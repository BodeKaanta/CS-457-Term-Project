"""PenteServer: the server's game state machine and shared state.

Implements fsm_specification.md (transition rows L1-L7, G1-G12, D1-D9, X1-X4)
with the threading rules of concurrency_design.md §3:

* One plain threading.Lock (state_lock) guards ALL shared state.
* Public entry points take the lock exactly once. Helpers whose names end
  in _locked assume the lock is held and never take it. Entry points only
  call _locked helpers, never each other.
* Every send to a registered socket happens under the lock (rule R2), so a
  broadcast is encoded once and reaches P1 then P2 with nothing in between.
* A failed send marks the connection dead and shutdown()s it. That
  connection's own handler thread then reports the drop (§5.3).
* Only a socket's handler thread close()s it; this module only shutdown()s.

Every state change is logged with the row ID from the FSM spec tables, so
the server log can be read side by side with the spec.
"""

import logging
import secrets
import socket
import threading
from dataclasses import dataclass, field

from pente import game, messages as m
from pente.framing import encode_frame

log = logging.getLogger("pente.fsm")

# Configuration constants (concurrency_design.md §1.2).
GRACE_PERIOD_SEC = 60
MAX_CONNECTIONS = 8
LEADERBOARD_MAX_ENTRIES = 10

# FSM state names (fsm_specification.md §2). WAITING_FOR_PLAYERS has the
# substates LOBBY_EMPTY and ONE_PLAYER, which are derived from the seats.
INIT = "INIT"
WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
GAME_START = "GAME_START"
PLAYER_TURN = "PLAYER_TURN"
EVALUATE_MOVE = "EVALUATE_MOVE"
CHECK_WIN = "CHECK_WIN"
PAUSED_RECONNECT = "PAUSED_RECONNECT"
GAME_OVER = "GAME_OVER"
CLEANUP = "CLEANUP"

# game_status values sent in STATE_UPDATE (Blueprint §3.5).
_WIRE_STATUS = {PLAYER_TURN: "ACTIVE", PAUSED_RECONNECT: "PAUSED_RECONNECT",
                GAME_OVER: "FINISHED"}


@dataclass(eq=False)
class Connection:
    """One accepted TCP connection. Flags are read and written under state_lock."""
    conn_id: int
    sock: socket.socket
    addr: tuple
    seated: bool = False            # bound to a seat by L1, L3 or D4
    close_requested: bool = False   # fatal ERROR sent or DISCONNECT processed
    dead: bool = False              # a send to this socket failed (§5.3)
    lost_handled: bool = False      # on_connection_lost already ran (idempotency)
    server_shutdown: bool = False   # the server itself shut this socket down

    def __str__(self) -> str:
        return f"conn-{self.conn_id} {self.addr[0]}:{self.addr[1]}"


@dataclass
class Seat:
    """FSM spec §5."""
    player_id: str
    alias: str
    conn_id: int | None
    session_token: str = ""
    connected: bool = True


@dataclass
class GameState:
    """Per-game state. CLEANUP replaces it with a fresh instance (FSM spec §5)."""
    state: str = INIT
    board: game.Board = field(default_factory=game.Board)
    turn_number: int = 0
    captures: dict = field(default_factory=lambda: {"P1": 0, "P2": 0})
    seats: dict = field(default_factory=dict)   # "P1"/"P2" -> Seat
    last_move: dict | None = None
    paused_player: str | None = None
    pause_seq: int = 0
    grace_timer: threading.Timer | None = None


@dataclass
class PlayerRecord:
    """Session leaderboard row, keyed by alias. Survives CLEANUP (§6.2)."""
    alias: str
    games_played: int = 0
    wins: int = 0
    losses: int = 0
    points: int = 0
    captures: int = 0
    fours: int = 0


class PenteServer:
    """The single game room. Thread-safe: every public method takes state_lock."""

    def __init__(self, grace_period: float = GRACE_PERIOD_SEC,
                 max_connections: int = MAX_CONNECTIONS,
                 tournament_rule: bool = True) -> None:
        self.state_lock = threading.Lock()
        self.grace_period = grace_period
        # The opening rule (G12). Always on in server.py; unit tests of other
        # rules turn it off so they can open anywhere on the board.
        self.tournament_rule = tournament_rule
        self.max_connections = max_connections
        self.game = GameState()
        self.game_id = 0
        self.leaderboard: dict[str, PlayerRecord] = {}
        self.connections: dict[int, Connection] = {}
        with self.state_lock:
            self._set_state_locked(WAITING_FOR_PLAYERS, "server listening")

    # ------------------------------------------------------------------
    # Public entry points (each takes state_lock exactly once)
    # ------------------------------------------------------------------

    def register_connection(self, conn: Connection) -> bool:
        """Add an accepted connection. Returns False if the server is at capacity."""
        with self.state_lock:
            if len(self.connections) >= self.max_connections:
                return False
            self.connections[conn.conn_id] = conn
            log.info("%s registered (%d open)", conn, len(self.connections))
            return True

    def unregister_connection(self, conn: Connection) -> None:
        """Remove a connection when its handler exits. Idempotent."""
        with self.state_lock:
            if self.connections.pop(conn.conn_id, None) is not None:
                log.info("%s closed (%d open)", conn, len(self.connections))

    def is_seated(self, conn: Connection) -> bool:
        """Used by the handler's handshake-timeout check."""
        with self.state_lock:
            return conn.seated

    def on_message(self, conn: Connection, frame: bytes) -> None:
        """Process one complete frame from `conn` (one critical section, rule R6)."""
        with self.state_lock:
            try:
                msg = m.decode(frame)
                m.validate_client_message(msg)
            except m.ProtocolError as e:
                self._send_error_locked(conn, e.code, e.detail, e.ref_msg_type)
                return

            msg_type = msg["msg_type"]
            seat_id = self._seat_of_locked(conn)
            # Identity comes from the socket, never from the message (Blueprint §2).
            # An unseated socket has no role yet, so CONNECT/RECONNECT are exempt.
            if seat_id is not None and msg["player_id"] != seat_id:
                self._send_error_locked(
                    conn, "PLAYER_ID_MISMATCH",
                    f"This connection is {seat_id}, but the message says "
                    f"{msg['player_id']!r}.", msg_type)
                return
            self._dispatch_locked(conn, seat_id, msg)

    def on_connection_lost(self, conn: Connection, cause: str) -> None:
        """The handler saw EOF, a reset, or a keepalive timeout. Idempotent."""
        with self.state_lock:
            self._connection_lost_locked(conn, cause)

    def on_fatal_protocol_error(self, conn: Connection, code: str) -> None:
        """FRAME_TOO_LARGE: tell the client, then treat it as a lost connection."""
        with self.state_lock:
            self._send_error_locked(conn, code, "No newline within 8192 bytes.", None)
            self._connection_lost_locked(conn, "PROTOCOL_VIOLATION")

    def on_grace_timeout(self, game_id: int, pause_seq: int) -> None:
        """Grace timer callback (D7). Runs on the timer thread."""
        with self.state_lock:
            g = self.game
            # Stale-timer guard (rule R7): the game may have resumed, ended, or
            # been paused again since this timer was started.
            if (g.state != PAUSED_RECONNECT or game_id != self.game_id
                    or pause_seq != g.pause_seq):
                log.info("Stale grace timer ignored (game %d, pause %d)", game_id, pause_seq)
                return
            winner = game.opponent(g.paused_player)
            log.info("%s did not rejoin within %ss", g.paused_player, self.grace_period)
            self._finish_game_locked(winner, "FORFEIT", "OPPONENT_TIMEOUT", "D7")

    def shutdown_all(self) -> None:
        """Server Ctrl-C: cancel the timer and wake every handler thread."""
        with self.state_lock:
            if self.game.grace_timer:
                self.game.grace_timer.cancel()
            for conn in self.connections.values():
                self._shutdown_socket(conn)

    def connection_count(self) -> int:
        with self.state_lock:
            return len(self.connections)

    # ------------------------------------------------------------------
    # Dispatch (fsm.dispatch in the FSM spec)
    # ------------------------------------------------------------------

    def _dispatch_locked(self, conn: Connection, seat_id: str | None, msg: dict) -> None:
        msg_type = msg["msg_type"]

        if seat_id is None:
            # Section 3.4: connections without a seat (X1-X4).
            if msg_type == m.CONNECT:
                self._handle_connect_locked(conn, msg)                       # X4
            elif msg_type == m.RECONNECT:
                self._handle_reconnect_locked(conn, msg)
            elif msg_type == m.MOVE:
                self._send_error_locked(conn, "GAME_NOT_ACTIVE",
                                        "You are not seated in a game.", m.MOVE)  # X2
            elif msg_type == m.DISCONNECT:
                log.info("%s sent DISCONNECT without a seat [X1]", conn)
                conn.lost_handled = True
                conn.close_requested = True                                  # X1
            return

        if msg_type in (m.CONNECT, m.RECONNECT):
            # Not in the transition tables: the handshake already happened.
            # Leave the state unchanged and answer with a non-fatal error.
            self._send_error_locked(
                conn, "UNKNOWN_MSG_TYPE",
                f"Already joined as {seat_id}; {msg_type} is only valid as the first message.",
                msg_type)
        elif msg_type == m.MOVE:
            self._handle_move_locked(conn, seat_id, msg)
        elif msg_type == m.DISCONNECT:
            self._handle_disconnect_locked(conn, seat_id, msg["payload"]["reason"])

    # ---- Lobby: CONNECT (L1-L4, L7) ---------------------------------

    def _handle_connect_locked(self, conn: Connection, msg: dict) -> None:
        payload = msg["payload"]
        alias = payload["alias"]

        if payload["protocol_version"] != m.PROTOCOL_VERSION:              # L2
            self._send_error_locked(conn, "VERSION_MISMATCH",
                                    f"Server speaks protocol version {m.PROTOCOL_VERSION}.",
                                    m.CONNECT)
            return
        if not m.is_valid_alias(alias):                                     # L2
            self._send_error_locked(conn, "INVALID_ALIAS",
                                    "Alias must be 1-16 characters: letters, digits, '_'.",
                                    m.CONNECT)
            return
        if self.game.state != WAITING_FOR_PLAYERS:                          # L7
            self._send_error_locked(conn, "ROOM_FULL",
                                    "A game is already in progress. Try again later.",
                                    m.CONNECT)
            return

        seats = self.game.seats
        if "P1" not in seats:                                               # L1
            old = self._state_label_locked()
            seats["P1"] = Seat("P1", alias, conn.conn_id)
            conn.seated = True
            log.info("FSM %s -> %s [L1 %s seated as P1 (%s)]",
                     old, self._state_label_locked(), alias, conn)
            self._send_locked(conn, m.make_lobby_wait())
            self._send_locked(conn, self._standings_msg_locked())
            return

        if seats["P1"].alias == alias:                                      # L4
            self._send_error_locked(conn, "ALIAS_TAKEN",
                                    f"Alias '{alias}' is already in the game.", m.CONNECT)
            return

        self._set_state_locked(GAME_START, f"L3 {alias} seated as P2 ({conn})")  # L3
        seats["P2"] = Seat("P2", alias, conn.conn_id)
        conn.seated = True
        self._start_game_locked()

    def _start_game_locked(self) -> None:
        """G1: assign tokens, send GAME_START to each, standings, initial board."""
        g = self.game
        self.game_id += 1
        p1, p2 = g.seats["P1"], g.seats["P2"]
        p1.session_token = secrets.token_hex(8)  # 16 hex chars (Blueprint §3.3)
        p2.session_token = secrets.token_hex(8)
        log.info("Game %d: %s (P1) vs %s (P2)", self.game_id, p1.alias, p2.alias)

        for me, other in ((p1, p2), (p2, p1)):
            self._send_to_seat_locked(me.player_id, m.make_game_start(
                me.player_id, me.alias, other.alias, me.session_token, resumed=False))
        self._broadcast_locked(self._standings_msg_locked())
        self._set_state_locked(PLAYER_TURN, "G1 P1 moves first")
        self._broadcast_locked(self._state_update_msg_locked())

    # ---- Gameplay: MOVE (G2-G12) -------------------------------------

    def _handle_move_locked(self, conn: Connection, seat_id: str, msg: dict) -> None:
        """Blueprint §3.4 validation order; the first failure wins."""
        g = self.game
        payload = msg["payload"]

        # 1. Game status (L5 in the lobby, D6 while paused).
        if g.state != PLAYER_TURN:
            status = self._status_text_locked()
            self._send_error_locked(conn, "GAME_NOT_ACTIVE",
                                    f"No move accepted while the game is {status}.", m.MOVE)
            return
        # 2. Turn order (G3).
        turn_owner = game.active_player(g.turn_number)
        if seat_id != turn_owner:
            self._send_error_locked(conn, "NOT_YOUR_TURN",
                                    f"It is {turn_owner}'s turn (turn {g.turn_number}).", m.MOVE)
            return
        # 3. Payload types (G4).
        for key in ("row", "col"):
            if key not in payload:
                self._send_error_locked(conn, "MISSING_FIELD",
                                        f"Payload field '{key}' is missing.", m.MOVE)
                return
            if not m.is_int(payload[key]):
                self._send_error_locked(conn, "INVALID_PAYLOAD",
                                        f"'{key}' must be an integer.", m.MOVE)
                return
        row, col = payload["row"], payload["col"]

        where = f"(row {row}, col {col})"
        if game.in_bounds(row, col):
            where = f"{game.to_notation(row, col)} {where}"
        self._set_state_locked(EVALUATE_MOVE, f"G2 {seat_id} MOVE {where}")
        # 4. Bounds (G5).
        if not game.in_bounds(row, col):
            self._send_error_locked(conn, "OUT_OF_BOUNDS",
                                    "That point is off the board. Columns are A-S and "
                                    "rows are 1-19.", m.MOVE)
            self._set_state_locked(PLAYER_TURN, f"G5 out of bounds, {seat_id} keeps the turn")
            return
        # 5. Occupancy (G6).
        if not g.board.is_empty(row, col):
            self._send_error_locked(conn, "CELL_OCCUPIED",
                                    f"{game.to_notation(row, col)} already has a stone.", m.MOVE)
            self._set_state_locked(PLAYER_TURN, f"G6 cell occupied, {seat_id} keeps the turn")
            return
        # 6. Tournament rule for P1's first two stones (G12).
        reason = (game.opening_rule_error(g.turn_number, row, col)
                  if self.tournament_rule else None)
        if reason:
            self._send_error_locked(conn, "TOURNAMENT_RULE", reason, m.MOVE)
            self._set_state_locked(PLAYER_TURN,
                                   f"G12 tournament rule, {seat_id} keeps the turn")
            return

        # G7: legal move. Place, resolve captures.
        captured = g.board.place(row, col, game.STONE[seat_id])
        g.captures[seat_id] += len(captured) // 2
        g.last_move = {"player": seat_id, "row": row, "col": col, "captured": captured}
        g.turn_number += 1  # moves applied so far (Blueprint §3.5)
        note = f"G7 stone placed, {len(captured) // 2} pair(s) captured"
        self._set_state_locked(CHECK_WIN, note)

        # Check order (FSM spec §4): captures resolved above, then five in a
        # row, then capture count, then the board-full safety net.
        if g.board.five_in_a_row(row, col):
            self._finish_game_locked(seat_id, "WIN", "FIVE_IN_A_ROW", "G8")
        elif g.captures[seat_id] >= m.CAPTURES_TO_WIN:
            self._finish_game_locked(seat_id, "WIN", "FIVE_CAPTURES", "G9")
        elif g.board.is_full():
            self._finish_game_locked(game.board_full_winner(g.captures), "WIN",
                                     "BOARD_FULL", "G10")
        else:                                                               # G11
            next_player = game.active_player(g.turn_number)
            self._set_state_locked(PLAYER_TURN, f"G11 no win, {next_player} to move")
            self._broadcast_locked(self._state_update_msg_locked())

    # ---- Disconnects and reconnects (L6, D1-D9) ----------------------

    def _handle_disconnect_locked(self, conn: Connection, seat_id: str, reason: str) -> None:
        """Intentional quit: final, no grace period (Blueprint §3.7)."""
        log.info("%s (%s) sent DISCONNECT %s", seat_id, conn, reason)
        conn.close_requested = True
        conn.lost_handled = True  # the EOF that follows is not a second event
        self._release_seat_locked(conn, seat_id)
        g = self.game

        if g.state == WAITING_FOR_PLAYERS:                                  # L6
            del g.seats[seat_id]
            log.info("FSM WAITING_FOR_PLAYERS/ONE_PLAYER -> WAITING_FOR_PLAYERS/LOBBY_EMPTY "
                     "[L6] P1 left the lobby")
        elif g.state == PLAYER_TURN:                                        # D1
            self._finish_game_locked(game.opponent(seat_id), "FORFEIT", "OPPONENT_QUIT", "D1")
        elif g.state == PAUSED_RECONNECT:                                   # D8
            self._cleanup_locked("D8 both players gone, game abandoned (not scored)")

    def _connection_lost_locked(self, conn: Connection, cause: str) -> None:
        """EOF, RST, keepalive timeout, or protocol violation. Idempotent."""
        if conn.lost_handled:
            return
        conn.lost_handled = True
        seat_id = self._seat_of_locked(conn)
        if seat_id is None:                                                 # X1
            if conn.server_shutdown:
                log.info("%s handler exiting; the server closed it [X1]", conn)
            else:
                log.info("%s lost (%s), no seat [X1]", conn, cause)
            return

        log.info("%s (%s) connection lost: %s", seat_id, conn, cause)
        self._release_seat_locked(conn, seat_id)
        g = self.game
        other = game.opponent(seat_id)

        if g.state == WAITING_FOR_PLAYERS:                                  # L6
            del g.seats[seat_id]
            log.info("FSM WAITING_FOR_PLAYERS/ONE_PLAYER -> WAITING_FOR_PLAYERS/LOBBY_EMPTY "
                     "[L6] P1 left the lobby")
        elif g.state == PLAYER_TURN:
            if self._seat_reachable_locked(other):                          # D2
                self._pause_locked(seat_id)
            else:                                                           # D3
                self._cleanup_locked("D3 both players lost")
        elif g.state == PAUSED_RECONNECT:                                   # D8
            self._cleanup_locked("D8 both players gone, game abandoned (not scored)")
        # D9: GAME_OVER / CLEANUP never last past one critical section, and
        # every other state is transient, so there is nothing else to do.

    def _pause_locked(self, seat_id: str) -> None:
        """D2: freeze the game and give the dropped player GRACE_PERIOD_SEC to rejoin."""
        g = self.game
        other = game.opponent(seat_id)
        g.paused_player = seat_id
        g.pause_seq += 1
        self._set_state_locked(PAUSED_RECONNECT,
                               f"D2 {seat_id} dropped, waiting {self.grace_period:g}s")
        alias = g.seats[seat_id].alias
        grace = int(self.grace_period)
        self._send_to_seat_locked(other, m.make_player_status(
            seat_id, "DISCONNECTED", grace,
            f"{alias} lost connection. Waiting up to {grace}s for them to rejoin..."))
        self._broadcast_locked(self._state_update_msg_locked())

        timer = threading.Timer(self.grace_period, self.on_grace_timeout,
                                args=(self.game_id, g.pause_seq))
        timer.daemon = True
        timer.name = f"grace-timer-{self.game_id}.{g.pause_seq}"
        g.grace_timer = timer
        timer.start()

    def _handle_reconnect_locked(self, conn: Connection, msg: dict) -> None:
        """D4 (success), D5 / X3 (rejected) for a RECONNECT on a new socket."""
        g = self.game
        payload = msg["payload"]
        if payload["protocol_version"] != m.PROTOCOL_VERSION:
            self._send_error_locked(conn, "VERSION_MISMATCH",
                                    f"Server speaks protocol version {m.PROTOCOL_VERSION}.",
                                    m.RECONNECT)
            return

        seat = g.seats.get(g.paused_player) if g.state == PAUSED_RECONNECT else None
        if (seat is None or seat.alias != payload["alias"]
                or not secrets.compare_digest(seat.session_token, payload["session_token"])):
            why = "bad alias or token [D5]" if seat else "no game is waiting for you [X3]"
            log.info("%s RECONNECT rejected: %s", conn, why)
            self._send_error_locked(conn, "INVALID_TOKEN",
                                    "No paused game matches that alias and session token.",
                                    m.RECONNECT)
            return

        if g.grace_timer:
            g.grace_timer.cancel()  # returns immediately (rule R4)
            g.grace_timer = None
        seat.conn_id = conn.conn_id
        seat.connected = True
        conn.seated = True
        pid = seat.player_id
        other = g.seats[game.opponent(pid)]
        g.paused_player = None
        self._set_state_locked(PLAYER_TURN, f"D4 {pid} ({seat.alias}) rejoined on {conn}")

        self._send_locked(conn, m.make_game_start(
            pid, seat.alias, other.alias, seat.session_token, resumed=True))
        self._send_locked(conn, self._standings_msg_locked())
        self._send_to_seat_locked(other.player_id, m.make_player_status(
            pid, "RECONNECTED", None, f"{seat.alias} is back. The game continues."))
        self._broadcast_locked(self._state_update_msg_locked())

    # ---- Game over, scoring, and cleanup ------------------------------

    def _finish_game_locked(self, winner: str, result: str, win_reason: str, row: str) -> None:
        """GAME_OVER entry actions (concurrency design §6.3), then CLEANUP."""
        g = self.game
        self._set_state_locked(GAME_OVER, f"{row} {winner} wins: {result} {win_reason}")
        if g.grace_timer:
            g.grace_timer.cancel()
            g.grace_timer = None

        aliases = {pid: seat.alias for pid, seat in g.seats.items()}
        scores = game.score_round(g.board, g.captures, aliases, win_reason, winner)
        for pid, score in scores.items():
            self._record_result_locked(score, won=(pid == winner))
        log.info("Round %d points: P1 %s=%d, P2 %s=%d", self.game_id,
                 scores["P1"].alias, scores["P1"].total, scores["P2"].alias, scores["P2"].total)

        board = g.board.to_wire()
        self._broadcast_locked(self._state_update_msg_locked())  # status FINISHED
        self._broadcast_locked(m.make_game_over(
            result, winner, aliases[winner], win_reason, g.captures, board, g.turn_number))
        round_result = {
            "game_id": self.game_id,
            "winner": winner,
            "win_reason": win_reason,
            "points": {pid: s.to_wire() for pid, s in scores.items()},
        }
        self._broadcast_locked(m.make_score_update(
            "ROUND_COMPLETE", round_result, self._ranked_leaderboard_locked()))
        self._cleanup_locked("results sent")

    def _record_result_locked(self, score: game.RoundScore, won: bool) -> None:
        record = self.leaderboard.setdefault(score.alias, PlayerRecord(score.alias))
        record.games_played += 1
        if won:
            record.wins += 1
        else:
            record.losses += 1
        record.points += score.total
        record.captures += score.capture_points
        record.fours += score.four_points

    def _cleanup_locked(self, reason: str) -> None:
        """CLEANUP: release every game resource and return to the lobby.

        The leaderboard is deliberately kept (FSM spec §2).
        """
        g = self.game
        self._set_state_locked(CLEANUP, reason)
        if g.grace_timer:
            g.grace_timer.cancel()
        for seat in g.seats.values():
            conn = self.connections.get(seat.conn_id) if seat.connected else None
            if conn is not None:
                conn.seated = False
                # The handler wakes, finds no seat (X1), closes its socket, exits.
                self._shutdown_socket(conn)
        self.game = GameState(state=CLEANUP)
        self._set_state_locked(WAITING_FOR_PLAYERS, "reset for the next game")

    # ------------------------------------------------------------------
    # Small helpers (all assume state_lock is held)
    # ------------------------------------------------------------------

    def _set_state_locked(self, new_state: str, note: str) -> None:
        old = self._state_label_locked()
        self.game.state = new_state
        log.info("FSM %s -> %s [%s]", old, self._state_label_locked(), note)

    def _state_label_locked(self) -> str:
        """State name, with the lobby substate (LOBBY_EMPTY / ONE_PLAYER) shown."""
        state = self.game.state
        if state == WAITING_FOR_PLAYERS:
            return state + ("/ONE_PLAYER" if self.game.seats else "/LOBBY_EMPTY")
        return state

    def _seat_of_locked(self, conn: Connection) -> str | None:
        for pid, seat in self.game.seats.items():
            if seat.connected and seat.conn_id == conn.conn_id:
                return pid
        return None

    def _seat_reachable_locked(self, pid: str) -> bool:
        seat = self.game.seats.get(pid)
        if seat is None or not seat.connected:
            return False
        conn = self.connections.get(seat.conn_id)
        return conn is not None and not conn.dead

    def _release_seat_locked(self, conn: Connection, seat_id: str) -> None:
        seat = self.game.seats[seat_id]
        seat.connected = False
        seat.conn_id = None
        conn.seated = False

    def _status_text_locked(self) -> str:
        """Short description of the current state for error details."""
        return {WAITING_FOR_PLAYERS: "waiting for a second player",
                PAUSED_RECONNECT: "paused for a reconnect"}.get(self.game.state, self.game.state)

    def _state_update_msg_locked(self) -> dict:
        g = self.game
        status = _WIRE_STATUS[g.state]
        active = game.active_player(g.turn_number) if status == "ACTIVE" else None
        return m.make_state_update(g.board.to_wire(), g.turn_number, active,
                                   g.captures, g.last_move, status)

    def _ranked_leaderboard_locked(self) -> list[dict]:
        """Sort by points, then wins (both high to low), then alias (A to Z)."""
        ranked = sorted(self.leaderboard.values(),
                        key=lambda r: (-r.points, -r.wins, r.alias))
        return [
            {"rank": i + 1, "alias": r.alias, "games_played": r.games_played,
             "wins": r.wins, "losses": r.losses, "points": r.points}
            for i, r in enumerate(ranked[:LEADERBOARD_MAX_ENTRIES])
        ]

    def _standings_msg_locked(self) -> dict:
        return m.make_score_update("STANDINGS", None, self._ranked_leaderboard_locked())

    def _send_error_locked(self, conn: Connection, code: str, detail: str,
                           ref_msg_type: str | None) -> None:
        msg = m.make_error(code, detail, ref_msg_type)
        level = logging.WARNING if msg["payload"]["fatal"] else logging.INFO
        log.log(level, "ERROR %s -> %s: %s", code, conn, detail)
        self._send_locked(conn, msg)
        if msg["payload"]["fatal"]:
            conn.close_requested = True

    # ---- Sending (rule R2: only ever called with state_lock held) -----

    def _send_locked(self, conn: Connection, msg: dict) -> None:
        log.debug("send %s -> %s", msg["msg_type"], conn)
        self._send_bytes_locked(conn, encode_frame(msg))

    def _send_to_seat_locked(self, pid: str, msg: dict) -> None:
        seat = self.game.seats.get(pid)
        if seat and seat.connected:
            conn = self.connections.get(seat.conn_id)
            if conn is not None:
                self._send_locked(conn, msg)

    def _broadcast_locked(self, msg: dict) -> None:
        """Encode once; send identical bytes to P1 then P2 (concurrency design §5.2)."""
        data = encode_frame(msg)
        log.debug("broadcast %s", msg["msg_type"])
        for pid in ("P1", "P2"):
            seat = self.game.seats.get(pid)
            if seat and seat.connected:
                conn = self.connections.get(seat.conn_id)
                if conn is not None:
                    self._send_bytes_locked(conn, data)

    def _send_bytes_locked(self, conn: Connection, data: bytes) -> None:
        if conn.dead:
            return
        try:
            conn.sock.sendall(data)  # bounded by the 5 s socket timeout (rule R3)
        except OSError as e:  # BrokenPipe, ConnectionReset/Aborted, Timeout are OSErrors
            log.warning("send to %s failed (%r); marking it dead", conn, e)
            conn.dead = True
            self._shutdown_socket(conn)

    @staticmethod
    def _shutdown_socket(conn: Connection) -> None:
        """Wake the owning handler without closing the socket (rule R5)."""
        conn.server_shutdown = True
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
