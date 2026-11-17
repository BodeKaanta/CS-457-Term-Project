#!/usr/bin/env python3
"""Networked Pente terminal client (CS 457 term project).

Usage:
    python3 client.py -h
    python3 client.py -i server.kaanta.edu -p 5457 [-a Alice] [-s dots]

Threads:
    main      reads commands from the input queue and sends MOVE / DISCONNECT
    receiver  reads NDJSON frames from the server and prints the board, scores,
              and status; rejoins the game if the connection drops (Blueprint §5.5)
    stdin     blocks on input() and pushes each typed line onto a queue, so the
              main thread never blocks and network updates keep printing

The client never applies moves itself. It renders only what the server sends.
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time

from pente import game
from pente import messages as m
from pente.framing import FrameReader, FrameTooLarge, encode_frame

DEFAULT_PORT = 5457
CONNECT_TIMEOUT_SEC = 10
RECONNECT_INTERVAL_SEC = 5   # Blueprint §5.5: retry every 5 s ...
RECONNECT_WINDOW_SEC = 60    # ... for up to 60 s (the server's grace period)
SESSION_FILE_PREFIX = ".pente_session_"
RECV_SIZE = 4096

# Stone symbols, chosen with -s. "xo" is plain ASCII, which every console can
# show (including the CML lab nodes). "dots" uses filled and hollow circles.
STONE_STYLES = {"xo": ("X", "O"), "dots": ("●", "○")}  # dots: P1 filled, P2 hollow
SYMBOL = {"1": "X", "2": "O", ".": "."}       # wire cell -> printed symbol
PLAYER_SYMBOL = {"P1": "X", "P2": "O"}
WIN_REASON_TEXT = {
    "FIVE_IN_A_ROW": "five in a row",
    "FIVE_CAPTURES": "five captured pairs",
    "BOARD_FULL": "full board (more captures)",
    "OPPONENT_QUIT": "opponent quit",
    "OPPONENT_TIMEOUT": "opponent did not reconnect in time",
}
HELP_TEXT = """Commands:
  J10       place a stone: column letter A-S, then row number 1-19
            (row 1 is at the bottom, like chess); J10 is the center
  board     redraw the board
  help      show this help
  quit      forfeit and leave the game"""


# ---------------------------------------------------------------------------
# Command line and DNS (Sprint 3 §2.1, §2.2)
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="client.py",
        description="Terminal client for networked Pente. Connects to the game server, "
                    "shows the board, and sends your moves.",
        epilog=f"Example: python3 client.py -i server.kaanta.edu -p {DEFAULT_PORT} -a Alice",
    )
    parser.add_argument("-i", "--host", dest="hostname", required=True,
                        help="server DNS hostname, e.g. server.kaanta.edu (resolved with DNS)")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help=f"server TCP port (the project uses {DEFAULT_PORT})")
    parser.add_argument("-a", "--alias",
                        help="your player name: 1-16 letters, digits or '_' (asked if omitted)")
    parser.add_argument("-s", "--stones", choices=sorted(STONE_STYLES), default="xo",
                        help="stone symbols: xo = X and O (default), dots = filled and "
                             "hollow circles")
    return parser.parse_args(argv)


def set_stone_style(style: str) -> None:
    """Switch the printed stone symbols ("xo" or "dots") for this process."""
    p1, p2 = STONE_STYLES[style]
    SYMBOL.update({"1": p1, "2": p2})
    PLAYER_SYMBOL.update({"P1": p1, "P2": p2})
    try:
        (p1 + p2).encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        # This output can't show the circles (e.g. piped output on Windows).
        # Switch it to UTF-8 rather than crash with UnicodeEncodeError.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def port_number(text: str) -> int:
    """argparse type: an integer port in 1-65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} is not in the range 1-65535")
    return port


def resolve_server(hostname: str) -> str | None:
    """Resolve the server name through DNS. Returns None (never raises) on failure.

    socket.gaierror covers an unknown name and an unreachable DNS server.
    UnicodeError covers names that cannot be encoded (e.g. an empty label).
    """
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, socket.herror, UnicodeError) as e:
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration ('ip dns server' / "
              "'ip host') or verify domain spelling.")
        print(f"        Resolver detail: {e}")
        return None


def ask_alias() -> str | None:
    """Prompt until the user enters a valid alias. None if input is closed."""
    while True:
        try:
            alias = input("Enter your player name (1-16 letters, digits or '_'): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if m.is_valid_alias(alias):
            return alias
        print("  That name is not valid. Use 1-16 characters: letters, digits, '_'.")


# ---------------------------------------------------------------------------
# Session file: lets a crashed or Ctrl-C'd client rejoin (Blueprint §3.3, §5.5).
# One file per alias (.pente_session_<alias>.json), so two clients started
# from the same folder (common when testing on one machine) never write the
# same file. The server decides whether a saved token is still valid.
# ---------------------------------------------------------------------------

def session_path(alias: str) -> str:
    return f"{SESSION_FILE_PREFIX}{alias}.json"


def load_session(alias: str, hostname: str, port: int) -> dict | None:
    """Return the saved session for this alias and server, if there is one."""
    try:
        with open(session_path(alias), encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return None
    if (not isinstance(entry, dict) or entry.get("hostname") != hostname
            or entry.get("port") != port or not isinstance(entry.get("session_token"), str)):
        return None
    return entry


def save_session(alias: str, hostname: str, port: int, player_id: str, token: str) -> None:
    entry = {"hostname": hostname, "port": port, "player_id": player_id,
             "session_token": token, "saved_at": int(time.time())}
    try:
        with open(session_path(alias), "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
    except OSError:
        pass  # rejoining after a crash is a convenience, never a reason to crash


def delete_session(alias: str) -> None:
    try:
        os.remove(session_path(alias))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Text rendering (Sprint 3 §2.4)
# ---------------------------------------------------------------------------

def parse_move(text: str) -> tuple[int, int] | None:
    """Parse a chess-style move like 'J10' into wire (row, col).

    Bounds are checked by the server, so 'T5' or 'A20' still parse.
    """
    return game.from_notation(text)


def render_board(board: list[str], last_move: dict | None) -> str:
    """Draw the board with column letters and row numbers. The last move is in [ ]."""
    last = (last_move["row"], last_move["col"]) if last_move else None
    size = len(board)
    letters = "    " + "".join(f" {game.COLUMNS[c]} " for c in range(len(board[0])))
    lines = [letters]
    for r, row in enumerate(board):
        cells = []
        for c, cell in enumerate(row):
            sym = SYMBOL.get(cell, "?")
            cells.append(f"[{sym}]" if (r, c) == last else f" {sym} ")
        number = size - r          # row 1 at the bottom, like chess
        lines.append(f"{number:>3} " + "".join(cells) + f" {number}")
    lines.append(letters)
    return "\n".join(lines)


def render_leaderboard(leaderboard: list[dict]) -> str:
    if not leaderboard:
        return "Session leaderboard: no finished games yet."
    lines = ["Session leaderboard:",
             "  Rank  Player            Games   W   L  Points"]
    for e in leaderboard:
        lines.append(f"  {e['rank']:>4}  {e['alias']:<16}  {e['games_played']:>5}"
                     f"  {e['wins']:>2}  {e['losses']:>2}  {e['points']:>6}")
    return "\n".join(lines)


def render_round(round_result: dict) -> str:
    lines = [f"Round {round_result['game_id']} points:",
             "  Player                Five  Captures  Fours  Total"]
    for pid in ("P1", "P2"):
        p = round_result["points"][pid]
        name = f"{p['alias']} ({PLAYER_SYMBOL[pid]})"
        lines.append(f"  {name:<20}  {p['five_bonus']:>4}  {p['capture_points']:>8}"
                     f"  {p['four_points']:>5}  {p['total']:>5}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class PenteClient:
    """Holds the connection and the latest game view. Shared fields are guarded by self.lock."""

    def __init__(self, hostname: str, server_ip: str, port: int, alias: str) -> None:
        self.hostname = hostname
        self.server_ip = server_ip
        self.port = port
        self.alias = alias

        self.lock = threading.Lock()        # guards the game-view fields below
        self.send_lock = threading.Lock()   # one writer at a time on the socket
        self.print_lock = threading.Lock()  # keeps multi-line output together
        self.sock: socket.socket | None = None
        self.connected = False

        self.player_id: str | None = None
        self.opponent_alias: str | None = None
        self.session_token: str | None = None
        self.state: dict | None = None      # payload of the latest STATE_UPDATE
        self.move_pending = False
        self.game_over = False
        self.resuming_from_file = False
        self.reconnect_deadline: float | None = None

        self.quitting = threading.Event()   # user chose to leave
        self.done = threading.Event()       # receiver finished; main loop exits
        self.lines: queue.Queue = queue.Queue()
        self.exit_code = 0

    # ---- output -------------------------------------------------------

    def say(self, text: str = "") -> None:
        with self.print_lock:
            print(text, flush=True)

    # ---- socket helpers -----------------------------------------------

    def open_socket(self) -> socket.socket:
        """Active open to the resolved server address. Raises OSError on failure."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SEC)
        try:
            sock.connect((self.server_ip, self.port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(None)  # the receiver thread blocks in recv() between messages
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return sock

    def send(self, msg: dict) -> bool:
        """Send one message. Returns False (never raises) if the connection is gone."""
        with self.send_lock:
            sock = self.sock
            if sock is None:
                return False
            try:
                sock.sendall(encode_frame(msg))
                return True
            except OSError:
                return False

    def close_socket(self) -> None:
        """shutdown() wakes the receiver from recv(); it then closes the socket."""
        with self.send_lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _use_socket(self, sock: socket.socket) -> None:
        with self.send_lock:
            self.sock = sock
        with self.lock:
            self.connected = True

    # ---- startup ------------------------------------------------------

    def start(self) -> bool:
        """Connect and send CONNECT, or RECONNECT if a saved session exists."""
        try:
            sock = self.open_socket()
        except OSError as e:
            print(f"[ERROR] Could not connect to {self.hostname} ({self.server_ip}) "
                  f"port {self.port}: {e}")
            print("        Is the server running, and is the port correct?")
            return False
        self._use_socket(sock)
        print(f"[INFO] Connected to {self.hostname} ({self.server_ip}) port {self.port}.")

        saved = load_session(self.alias, self.hostname, self.port)
        if saved:
            self.resuming_from_file = True
            print(f"[INFO] Found a saved game for {self.alias}; trying to rejoin it...")
            self.send(m.make_reconnect(saved.get("player_id"), self.alias,
                                       saved["session_token"]))
        else:
            self.send(m.make_connect(self.alias))
        return True

    # ---- receiver thread --------------------------------------------

    def receiver(self) -> None:
        try:
            while True:
                outcome = self._read_messages(self.sock)
                with self.send_lock:
                    old, self.sock = self.sock, None
                with self.lock:
                    self.connected = False
                if old is not None:
                    old.close()  # the receiver is the only thread that close()s

                if outcome == "FINISHED" or self.quitting.is_set():
                    return
                if outcome == "RETRY_AS_NEW":
                    self.say("[INFO] That saved game has ended. Joining as a new player.")
                    delete_session(self.alias)
                    self.resuming_from_file = False
                    if self._reopen(m.make_connect(self.alias)):
                        continue
                    self.say("[ERROR] Could not connect to the server again.")
                    self.exit_code = 1
                    return
                if outcome == "FATAL":
                    self.exit_code = 1
                    return
                # The connection dropped without a GAME_OVER.
                if self.session_token and not self.game_over:
                    if self._try_rejoin():
                        continue
                    self.say("[ERROR] Could not rejoin the game within "
                             f"{RECONNECT_WINDOW_SEC} s. The game was forfeited.")
                    delete_session(self.alias)
                else:
                    self.say("[ERROR] Connection to the server was lost.")
                self.exit_code = 1
                return
        finally:
            self.done.set()

    def _read_messages(self, sock: socket.socket) -> str:
        """Read frames until the connection ends or a handler says stop."""
        reader = FrameReader()
        while True:
            try:
                data = sock.recv(RECV_SIZE)
            except OSError:
                return "LOST"
            if not data:  # EOF (Blueprint §5.1)
                return "LOST"
            try:
                frames = reader.feed(data)
            except FrameTooLarge:
                self.say("[ERROR] The server sent an oversized message.")
                return "LOST"
            for frame in frames:
                try:
                    msg = json.loads(frame.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self.say("[WARN] Ignored a malformed message from the server.")
                    continue
                if not isinstance(msg, dict):
                    continue
                outcome = self.handle(msg)
                if outcome:
                    return outcome

    def _reopen(self, first_msg: dict) -> bool:
        try:
            sock = self.open_socket()
        except OSError:
            return False
        self._use_socket(sock)
        return self.send(first_msg)

    def _try_rejoin(self) -> bool:
        """Retry RECONNECT every 5 s until the 60 s window closes (Blueprint §5.5)."""
        if self.reconnect_deadline is None:
            self.reconnect_deadline = time.monotonic() + RECONNECT_WINDOW_SEC
            self.say(f"\n[WARN] Connection lost. Trying to rejoin every "
                     f"{RECONNECT_INTERVAL_SEC} s for up to {RECONNECT_WINDOW_SEC} s "
                     "(type 'quit' to give up)...")
        while True:
            remaining = self.reconnect_deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.quitting.wait(min(RECONNECT_INTERVAL_SEC, remaining)):
                return False
            if self._reopen(m.make_reconnect(self.player_id, self.alias, self.session_token)):
                self.say("[INFO] Reached the server again; asking to rejoin...")
                return True
            left = max(0, int(self.reconnect_deadline - time.monotonic()))
            self.say(f"[INFO] Server not reachable yet; {left} s left to rejoin.")

    # ---- message handlers (run on the receiver thread) ---------------

    def handle(self, msg: dict) -> str | None:
        """Handle one server message. Returns an outcome to stop reading, else None."""
        msg_type = msg.get("msg_type")
        payload = msg.get("payload") or {}
        handler = {
            m.LOBBY_WAIT: self.on_lobby_wait,
            m.GAME_START: self.on_game_start,
            m.STATE_UPDATE: self.on_state_update,
            m.ERROR: self.on_error,
            m.GAME_OVER: self.on_game_over,
            m.PLAYER_STATUS: self.on_player_status,
            m.SCORE_UPDATE: self.on_score_update,
        }.get(msg_type)
        if handler is None:
            self.say(f"[WARN] Ignored unexpected message type {msg_type!r}.")
            return None
        try:
            return handler(payload)
        except (KeyError, TypeError, ValueError) as e:
            self.say(f"[WARN] Ignored a {msg_type} message with a bad payload ({e!r}).")
            return None

    def on_lobby_wait(self, p: dict) -> None:
        self.say(f"[LOBBY] {p['message']} ({p['players_connected']}/{p['players_needed']} "
                 "players). You will be Player 1 (X) and move first.")

    def on_game_start(self, p: dict) -> None:
        with self.lock:
            self.player_id = p["your_player_id"]
            self.opponent_alias = p["opponent_alias"]
            self.session_token = p["session_token"]
            self.reconnect_deadline = None
            self.resuming_from_file = False
        save_session(self.alias, self.hostname, self.port, self.player_id, self.session_token)
        me = PLAYER_SYMBOL[self.player_id]
        if p["resumed"]:
            self.say(f"\n[GAME] Rejoined the game as {self.player_id} ({me}) "
                     f"against {self.opponent_alias}.")
        else:
            them = PLAYER_SYMBOL["P2" if self.player_id == "P1" else "P1"]
            self.say("\n" + "=" * 62)
            self.say(f"  GAME START: {p['your_alias']} ({me}, {self.player_id}) vs "
                     f"{self.opponent_alias} ({them})")
            self.say(f"  {p['first_player']} moves first. Win with {p['stones_to_win']} in a row "
                     f"or {p['captures_to_win']} captured pairs.")
            self.say("=" * 62)

    def on_state_update(self, p: dict) -> None:
        with self.lock:
            self.state = p
            self.move_pending = False
        self.say("\n" + self.render_state())

    def on_error(self, p: dict) -> str | None:
        code, detail = p["code"], p["detail"]
        with self.lock:
            self.move_pending = False
        if not p["fatal"]:
            self.say(f"[SERVER] Rejected - {code}: {detail}")
            if self.my_turn():
                self.say(self.turn_prompt())
            return None
        if code == "INVALID_TOKEN":
            if self.resuming_from_file:
                return "RETRY_AS_NEW"
            if self.reconnect_deadline is not None:
                return "LOST"  # the server may not have noticed the drop yet; keep trying
        self.say(f"[ERROR] The server closed the connection - {code}: {detail}")
        return "FATAL"

    def on_player_status(self, p: dict) -> None:
        self.say(f"[STATUS] {p['message']}")

    def on_game_over(self, p: dict) -> None:
        with self.lock:
            self.game_over = True
        delete_session(self.alias)
        won = p["winner"] == self.player_id
        headline = "YOU WIN!" if won else "You lose."
        reason = WIN_REASON_TEXT.get(p["win_reason"], p["win_reason"])
        caps = p["final_captures"]
        self.say("\n" + "=" * 62)
        self.say(f"  GAME OVER - {headline}")
        self.say(f"  Winner: {p['winner_alias']} ({p['winner']}) by {reason} [{p['result']}]")
        self.say(f"  Total moves: {p['total_turns']}   Captured pairs: "
                 f"P1 {caps['P1']}, P2 {caps['P2']}")
        self.say("=" * 62)

    def on_score_update(self, p: dict) -> str | None:
        parts = []
        if p["reason"] == "ROUND_COMPLETE" and p["round"]:
            parts.append(render_round(p["round"]))
        parts.append(render_leaderboard(p["leaderboard"]))
        self.say("\n" + "\n\n".join(parts))
        if p["reason"] == "ROUND_COMPLETE" and self.game_over:
            return "FINISHED"
        return None

    # ---- view helpers ----------------------------------------------

    def my_turn(self) -> bool:
        with self.lock:
            s = self.state
            return (self.connected and not self.game_over and s is not None
                    and s["game_status"] == "ACTIVE" and s["active_player"] == self.player_id)

    def turn_prompt(self) -> str:
        return ">>> YOUR TURN. Enter your move as letter + number (example: J10), or 'quit'."

    def render_state(self) -> str:
        with self.lock:
            s = self.state
            me, opp_alias = self.player_id, self.opponent_alias
        if s is None:
            return "(no board yet)"
        opp = "P2" if me == "P1" else "P1"
        lines = [render_board(s["board"], s["last_move"])]
        lines.append(f"Turn {s['turn_number']} | You: {self.alias} ({PLAYER_SYMBOL[me]}) "
                     f"captures {s['captures'][me]}/5 | {opp_alias} ({PLAYER_SYMBOL[opp]}) "
                     f"captures {s['captures'][opp]}/5")
        lm = s["last_move"]
        if lm:
            who = self.alias if lm["player"] == me else opp_alias
            where = game.to_notation(lm["row"], lm["col"])
            text = f"Last move: {who} ({PLAYER_SYMBOL[lm['player']]}) at {where}"
            if lm["captured"]:
                stones = ", ".join(game.to_notation(r, c) for r, c in lm["captured"])
                text += f" and captured {len(lm['captured']) // 2} pair ({stones})"
            lines.append(text)
        status = s["game_status"]
        if status == "ACTIVE":
            if s["active_player"] == me:
                lines.append(self.turn_prompt())
            else:
                lines.append(f"Waiting for {opp_alias} ({s['active_player']}) to move...")
        elif status == "PAUSED_RECONNECT":
            lines.append("Game paused while a player reconnects.")
        return "\n".join(lines)

    # ---- main thread ------------------------------------------------

    def stdin_reader(self) -> None:
        """Daemon thread: forward typed lines to the main thread. None means EOF."""
        while True:
            try:
                line = input()
            except (EOFError, OSError, ValueError):
                self.lines.put(None)
                return
            self.lines.put(line)

    def handle_command(self, line: str) -> None:
        cmd = line.strip()
        if not cmd:
            return
        word = cmd.lower()
        if word in ("quit", "exit", "q"):
            self.quit("USER_QUIT")
            return
        if word in ("help", "?"):
            self.say(HELP_TEXT)
            return
        if word == "board":
            self.say(self.render_state())
            return

        move = parse_move(cmd)
        if move is None:
            self.say("[INPUT] Type a move as a column letter A-S and a row number 1-19 "
                     "(e.g. J10), or 'help'.")
            if self.my_turn():
                self.say(self.turn_prompt())
            return
        if self.game_over:
            return
        with self.lock:
            pending = self.move_pending
            paused = self.state is not None and self.state["game_status"] == "PAUSED_RECONNECT"
            connected = self.connected
        if not self.my_turn():
            if not connected:
                self.say("[INPUT] Not connected right now; trying to rejoin the game...")
            elif paused:
                self.say("[INPUT] The game is paused until the other player reconnects.")
            elif self.state is None:
                self.say("[INPUT] The game has not started yet. Waiting for a second player...")
            else:
                self.say("[INPUT] It is not your turn yet. Waiting for the other player...")
            return
        if pending:
            self.say("[INPUT] Your move was sent; waiting for the server to reply.")
            return
        with self.lock:
            self.move_pending = True
        if not self.send(m.make_move(self.player_id, *move)):
            with self.lock:
                self.move_pending = False
            self.say("[ERROR] Could not send the move; the connection is down.")

    def quit(self, reason: str) -> None:
        """Intentional quit: DISCONNECT then close (Blueprint §3.7). Forfeits a game."""
        self.quitting.set()
        if not self.game_over:
            in_game = self.state is not None
            self.send(m.make_disconnect(self.player_id, reason))
            self.say("[INFO] You left the game" + (" (forfeit)." if in_game else "."))
        delete_session(self.alias)
        self.close_socket()

    def leave_without_forfeit(self) -> None:
        """Ctrl-C: close the transport without DISCONNECT (Blueprint §5 table).

        The server treats this as an unexpected drop and holds the seat for
        60 s, so running the client again with the same alias rejoins.
        """
        self.quitting.set()
        if self.session_token and not self.game_over:
            self.say("\n[INFO] Closed without forfeiting. Run the client again with the "
                     f"same name within {RECONNECT_WINDOW_SEC} s to rejoin.")
        self.close_socket()

    def run(self) -> int:
        if not self.start():
            return 1
        threading.Thread(target=self.receiver, name="receiver", daemon=True).start()
        threading.Thread(target=self.stdin_reader, name="stdin", daemon=True).start()
        self.say("Type 'help' for commands.")
        try:
            while not self.done.is_set():
                try:
                    line = self.lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    # Input closed. Give a Ctrl-C a moment to arrive first, since on
                    # Windows Ctrl-C can also close input() in the stdin thread.
                    time.sleep(0.5)
                    self.quit("CLIENT_SHUTDOWN")
                    break
                self.handle_command(line)
                if self.quitting.is_set():
                    break
        except KeyboardInterrupt:
            self.leave_without_forfeit()
        self.done.wait(timeout=3)
        return self.exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    set_stone_style(args.stones)
    hostname = args.hostname.strip()
    if not hostname:
        print("[ERROR] The server hostname (-i) cannot be empty.")
        return 1

    server_ip = resolve_server(hostname)
    if server_ip is None:
        return 1
    print(f"[INFO] Resolved {hostname} -> {server_ip}")

    alias = args.alias
    if alias is not None and not m.is_valid_alias(alias):
        print(f"[ERROR] '{alias}' is not a valid name. Use 1-16 letters, digits or '_'.")
        return 1
    if alias is None:
        alias = ask_alias()
        if alias is None:
            return 1

    client = PenteClient(hostname, server_ip, args.port, alias)
    try:
        return client.run()
    except KeyboardInterrupt:
        client.leave_without_forfeit()
        return 0


if __name__ == "__main__":
    exit_code = main()
    # The stdin thread may still be blocked in input(). A normal interpreter
    # exit would try to take stdin's lock while that thread holds it, which
    # aborts Python ("could not acquire lock ... at interpreter shutdown").
    # Flush our output and leave immediately instead.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
