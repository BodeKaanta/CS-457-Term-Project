# AI Prompting & Constraint Strategy

**Project:** CS 457 Term Project: Networked Pente
**Author:** Bode Kaanta
**Sprint:** 1 (updated in Sprint 2)
**AI tools:** Claude (primary), GitHub Copilot (inline completion only)
**Related docs:** [`protocol_blueprint.md`](protocol_blueprint.md) · [`fsm_specification.md`](fsm_specification.md) · [`concurrency_design.md`](concurrency_design.md) · [`sequence_diagram.md`](sequence_diagram.md)

The goal is to make AI tools **implement my protocol**, not invent one. Generic "write me a socket game server" prompts produce boilerplate that ignores framing, uses ad-hoc message formats, and crashes on disconnects. That is exactly what this course says will fail evaluation. Every code-generation request therefore follows the rules below.

This document has two parts. §1–§4 is the strategy and the exact prompts I will use once I start writing code. §5 is a log of the prompts I have actually used so far, which I will keep adding to as the project goes on.

---

## 1. Constraint Strategy

1. **Spec-first context.** The full text of `protocol_blueprint.md`, `fsm_specification.md` and (from Sprint 2) `concurrency_design.md` is pasted into (or attached to) every coding session. The AI is told those documents are normative and override its own defaults.
2. **A fixed system prompt** (§2) is used for every session, so the rules never drift between conversations.
3. **One small unit per request.** Each prompt asks for a single module or function with a fixed signature (§3). Never "write the whole server."
4. **Closed vocabularies.** Prompts list the exact allowed `msg_type` values, error codes, state names, and field names. The AI is forbidden from adding new ones.
5. **Mandatory tests.** Every request also asks for `pytest` tests that encode the spec's examples (wire streams from blueprint §1.2, transitions from FSM §3).
6. **Human verification gate** (§4) before any AI output is committed.
7. **Prompt log** (§5): every prompt actually used is recorded, along with what was accepted or rejected.

---

## 2. System Prompt (used for every coding session)

```text
You are a code generator for a CS 457 networking project: a 2-player Pente game
(19x19 board) over TCP, written in Python 3.11 standard library only
(socket, threading, json, struct, logging, dataclasses, secrets, time). No third-party packages.
The game is TERMINAL ONLY: the board is printed as plain text and moves are typed
as "row col". Never use a GUI, pygame, tkinter, curses, or any 2D/3D graphics.

The attached documents protocol_blueprint.md, fsm_specification.md and
concurrency_design.md are the NORMATIVE SPECIFICATION. If anything in your training or habits conflicts with
them, the specification wins. Do not "improve" the protocol.

HARD RULES - violating any of these makes the output unusable:
1. FRAMING: newline-delimited JSON. Send = json.dumps(msg, separators=(",",":"))
   .encode("utf-8") + b"\n" via sock.sendall(). Receive = accumulate bytes in a
   per-connection bytearray buffer and split on b"\n". NEVER assume one recv()
   equals one message. Max frame 8192 bytes, otherwise ERROR FRAME_TOO_LARGE (fatal).
2. ENVELOPE: every message has exactly msg_type, player_id, payload, timestamp.
   Server messages use player_id "SERVER".
3. MESSAGE TYPES (closed set): CONNECT, LOBBY_WAIT, GAME_START, MOVE,
   STATE_UPDATE, ERROR, DISCONNECT, GAME_OVER, RECONNECT, PLAYER_STATUS,
   SCORE_UPDATE.
   Use the exact payload field names and types from the blueprint. Do not add,
   rename, or omit fields.
4. ERROR CODES (closed set): MALFORMED_JSON, MISSING_FIELD, UNKNOWN_MSG_TYPE,
   INVALID_PAYLOAD, PLAYER_ID_MISMATCH, NOT_YOUR_TURN, OUT_OF_BOUNDS,
   CELL_OCCUPIED, GAME_NOT_ACTIVE, INVALID_ALIAS, ALIAS_TAKEN, ROOM_FULL,
   VERSION_MISMATCH, INVALID_TOKEN, FRAME_TOO_LARGE.
5. FSM STATES (closed set): INIT, WAITING_FOR_PLAYERS (LOBBY_EMPTY, ONE_PLAYER),
   GAME_START, PLAYER_TURN, EVALUATE_MOVE, CHECK_WIN, PAUSED_RECONNECT,
   GAME_OVER, CLEANUP. Only implement transitions listed in the FSM transition
   table. All state mutation goes through PenteServer._dispatch_locked() guarded by a
   single threading.Lock (state_lock).
6. TERMINATION: every recv loop checks `if not data:` (EOF) and breaks. Catch
   ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError,
   OSError around recv AND around every send. A send failure to one client must
   never crash handling of the other. Always close sockets in `finally`.
   DISCONNECT message = immediate forfeit. EOF/RST/timeout = PAUSED_RECONNECT
   with a 60-second threading.Timer.
7. NON-FATAL ERRORS never change board, turn, or state, and never break the
   receive loop.
8. Identify players by socket/connection, never by the client-supplied player_id.
9. No print() for diagnostics; use the logging module. No global mutable state
   outside the PenteServer object.
10. If the spec is ambiguous or silent, STOP and ask me. Do not guess.
11. CONCURRENCY (concurrency_design.md §3): one handler thread per socket.
   NEVER hold state_lock during recv(). EVERY sendall() to a registered socket
   happens while holding state_lock, via _send_locked(); one broadcast = encode
   once, send to P1 then P2, in the same critical section. Helpers ending in
   _locked never acquire the lock. Only the owning handler thread close()s its
   socket; other threads may only shutdown() it. Every socket has
   settimeout(5); a recv() TimeoutError with errno None is idle, not an error.

Output format: one Python file per request, type hints, docstrings that cite the
spec section (e.g. "# Blueprint §3.4"), followed by a pytest test file.
```

---

## 3. Task Prompts (one per module)

Each prompt below is sent **after** the system prompt and the three spec documents.

### 3.1 Framing layer: `protocol/framing.py`

```text
Implement protocol/framing.py per Blueprint §1.1–1.3 ONLY.

Required API (exact names and signatures):
  class FrameTooLarge(Exception): ...
  class FrameReader:
      def __init__(self, max_frame: int = 8192) -> None
      def feed(self, data: bytes) -> list[bytes]   # returns complete frames WITHOUT '\n'
  def encode_frame(msg: dict) -> bytes             # compact JSON + b"\n"
  def send_msg(sock: socket.socket, msg: dict) -> None   # sendall; lets socket errors propagate

Do NOT parse JSON in FrameReader (parsing is the codec's job). Ignore empty lines.

Tests (pytest) must cover, using the literal wire examples in Blueprint §1.2:
  - two messages coalesced in one feed() -> 2 frames
  - one message fragmented across three feed() calls -> 1 frame, leftover kept
  - trailing partial message stays buffered
  - >8192 bytes with no newline -> FrameTooLarge
  - encode_frame output contains exactly one b"\n", at the end
```

### 3.2 Message codec & validation: `protocol/messages.py`

```text
Implement protocol/messages.py per Blueprint §2 and §3.

Required API:
  MSG_TYPES: frozenset[str]          # the 11 types, nothing else
  ERROR_CODES: frozenset[str]        # the 15 codes, nothing else
  class ProtocolError(Exception):    # carries .code (one of ERROR_CODES) and .detail
  def decode(frame: bytes) -> dict   # raises ProtocolError(MALFORMED_JSON) if not a JSON object
  def validate_client_message(msg: dict) -> None
      # checks envelope keys, msg_type is a CLIENT->SERVER type
      # (CONNECT, MOVE, DISCONNECT, RECONNECT), payload field presence and
      # exact Python types (bool is NOT an int for row/col). Raises ProtocolError.
      # Does NOT check game rules (turn, bounds, occupancy) - that is the FSM's job.
  Builder functions, one per server message, returning a full envelope dict:
  make_lobby_wait(), make_game_start(...), make_state_update(...),
  make_error(code, detail, fatal, ref_msg_type), make_game_over(...),
  make_player_status(...), make_score_update(...)
  Builder parameters must map 1:1 to the payload tables in Blueprint §3.

Tests: every JSON example in Blueprint §3 must round-trip through decode() and
validation (client types) or equal the builder output (server types, ignoring timestamp).
```

### 3.3 Game rules: `game/pente.py`

```text
Implement pure game logic per FSM Spec §4. No sockets, no threading.

Required API:
  class Board:  # 19x19, cells ".", "1", "2"
      def place(self, row: int, col: int, stone: str) -> list[tuple[int,int]]
          # places the stone, removes captured PAIRS (exactly two flanked opponent
          # stones, 8 directions), returns removed coordinates
      def five_in_a_row(self, row: int, col: int) -> bool
      def is_full(self) -> bool
      def to_wire(self) -> list[str]  # 19 strings of 19 chars (Blueprint §2)
  def active_player(turn_number: int) -> str   # "P1" if even else "P2"
  def count_fours(board, stone: str) -> int    # runs of EXACTLY 4 (FSM Spec §4.1)
  def score_round(board, captures, winner) -> dict  # {"P1": RoundScore, "P2": RoundScore}

Must NOT capture when a player places a stone INTO a flanked position.
Must NOT capture 1 or 3 stones. Tests for each of these plus all 4 line directions.
There are NO draws. Full board with no winner: more captured pairs wins; tied -> P2 wins.
Scoring: +5 bonus ONLY for a FIVE_IN_A_ROW win; +1 per captured pair and +1 per
four on the final board, for both players. A run of 5+ is not a four. Test: open four, blocked four, five (not counted),
six (not counted), two fours sharing no stones, all 4 directions.
```

### 3.4 Server FSM: `server/fsm.py`

```text
Implement PenteServer (the FSM object) per fsm_specification.md and
concurrency_design.md §3. Implement EXACTLY the transitions
in tables L1–L7, G1–G11, D1–D9. Any (state, event) pair not in the tables
must leave the state unchanged and, for client messages, send ERROR
GAME_NOT_ACTIVE or UNKNOWN_MSG_TYPE as appropriate.

Entry points (each acquires self.state_lock exactly once): on_message(conn, frame),
on_connection_lost(conn, cause), on_fatal_protocol_error(conn, code),
on_grace_timeout(game_id, pause_seq), register_connection(conn),
unregister_connection(conn). Entry points call only _locked helpers, never
each other. on_grace_timeout must ignore a stale timer (state, game_id or
pause_seq changed). Implement rows X1-X4 for connections without a seat.
Outbound messages are sent INSIDE the critical section through _send_locked /
_broadcast_locked (concurrency_design.md §5), so both players get identical
bytes in the same order. A failed send marks the connection dead and calls
shutdown(); it never re-enters the lock. On GAME_OVER: score the round, update
the alias-keyed leaderboard (kept across CLEANUP), then send STATE_UPDATE,
GAME_OVER, SCORE_UPDATE in that order.
The 60 s threading.Timer calls on_grace_timeout(); cancel it in D4 and CLEANUP.
CLEANUP must always return to WAITING_FOR_PLAYERS/LOBBY_EMPTY.

Tests: drive the FSM with fake conn_ids (no real sockets) through: full game
to FIVE_IN_A_ROW, out-of-turn MOVE, CELL_OCCUPIED, DISCONNECT forfeit,
CONN_LOST -> RECONNECT success, CONN_LOST -> timeout forfeit, bad token,
both players lost, third client ROOM_FULL, P1 leaving the lobby, two threads
calling on_message at once (no lost or doubled turn), stale grace timer after a
RECONNECT, leaderboard surviving CLEANUP, SCORE_UPDATE contents after a win.
```

### 3.5 Network shell: `server/server.py` and `client/client.py`

```text
Implement the socket layer per Blueprint §5 and concurrency_design.md §2.
Server: one accept thread + one handler thread per connection (daemon=True,
MAX_CONNECTIONS=8, HANDSHAKE_TIMEOUT_SEC=10) using FrameReader; enable SO_KEEPALIVE and
TCP_KEEPIDLE=10 / TCP_KEEPINTVL=5 / TCP_KEEPCNT=3 when hasattr(socket, ...).
Use the exact client_handler structure in concurrency_design.md §2.5 (EOF check,
idle-timeout check, exception list, finally: unregister + close). Wrap each send
in broadcast() individually.

Client: terminal-only text UI (print()/input() only) that renders STATE_UPDATE
boards as text (row/col labels 0-18, "." empty, "1"/"2" stones),
reads "row col" input, sends MOVE, sends DISCONNECT on "quit". Saves the
session_token to .pente_session.json on GAME_START and, on EOF/RST without
GAME_OVER, retries RECONNECT every 5 s for up to 60 s (Blueprint §5.5).
Prints SCORE_UPDATE leaderboards as a text table; after GAME_OVER it waits for
SCORE_UPDATE (or EOF) before exiting.
Host and port come from argv (default server.kaanta.edu 5457).
```

---

## 4. Verification Gate (before any AI-generated code is committed)

- [ ] Every `msg_type`, field name, error code, and state name matches the spec character-for-character (`grep` for anything not in the closed sets).
- [ ] No `recv()` result is passed straight to `json.loads()` without going through `FrameReader`.
- [ ] Every receive loop has `if not data: break`.
- [ ] Every `send`/`sendall` is inside a `try` that catches `BrokenPipeError` / `ConnectionResetError` / `OSError`.
- [ ] No `recv()` happens while holding `state_lock`; every `sendall()` to a registered socket happens while holding it (concurrency design R1, R2).
- [ ] Only the owning handler thread calls `close()` on its socket; no `_locked` helper acquires the lock.
- [ ] All generated `pytest` tests pass, and I have read each test to confirm it checks the spec (not just the AI's own code).
- [ ] Manual test: two clients, a valid game, an out-of-turn move, `kill -9` on one client then a rejoin within 60 s, and a second `kill -9` left past 60 s.
- [ ] Manual test: play two games in a row with the same aliases and confirm the leaderboard totals carry over.
- [ ] Rejected AI suggestions and the reason are recorded in the log below.

---

## 5. Prompt Log

| Date | Tool | Prompt used | Target | Outcome / corrections |
|---|---|---|---|---|
| 2026-10-05 | Claude | Uploaded the Sprint 1 assignment page and my repo link. Told it my plan: JSON for serialization, the given message types, Mermaid for the diagram, and everything submitted as Markdown through a pull request | `docs/*.md` | Claude asked about framing, disconnect handling, the post-game flow, and how to deliver the files |
| 2026-10-05 | Claude | My design answers: newline-delimited JSON; on a disconnect, a 30–60 s grace period so the player can rejoin (I chose this because rejoining is a project goal); return to the lobby after each game | `docs/*.md` | Claude drafted the blueprint, FSM, and this file from those decisions and compiled the Mermaid diagram with the Mermaid CLI to check for syntax errors. Grace period set to 60 s; `RECONNECT` / `PLAYER_STATUS` added for the rejoin |
| 2026-10-05 | Claude | Reviewed the drafts and sent corrections: (1) the game must be **terminal only**, not 2D/3D; (2) **Pente has no draws**, so remove `DRAW` and rename `CHECK_WIN_DRAW`; (3) asked why there is no `LOSE` result; (4) asked how port 5457 was chosen and how the server endpoint is established; (5) the diagram was hard to read; (6) the `r, c, dr, dc` notation in the capture rule was unclear; (7) don't list future sprints in this file | all docs | Accepted all fixes: terminal-only rule added to the system prompt and docs; `DRAW` removed (full-board safety rule: more captures wins, tie → P2); `LOSE` explained as implied by `winner`; port rationale and endpoint setup added (blueprint §1.4); diagram simplified to short labels keyed to the transition tables; capture rule rewritten in plain words with an example |
| 2026-10-19 | Claude | Uploaded the Sprint 2 assignment page and repo link. Chose **multi-threading** (Option A) because it is common in real-world servers. Asked for help with the Mermaid diagram and asked whether scores are stored per game instance and whether the broadcast happens after the active player's move | `docs/concurrency_design.md`, `docs/sequence_diagram.md` | Claude explained the thread layout and corrected one point: the server listens to **both** clients at all times (one thread each), and turn enforcement happens after receiving. It also pointed out that `CLEANUP` wipes `GameState`, so "round scores / leaderboard" would not survive a game. Gave options for score scope, score message, broadcast strategy, and diagram detail |
| 2026-10-19 | Claude | My decisions: session leaderboard kept in server memory (keyed by alias); a new `SCORE_UPDATE` message; send broadcasts **while holding the lock**; diagram shows the handler threads and the lock. Scoring follows normal Pente points: 5 for a win, 1 per captured pair, 1 per four-in-a-row | all docs | Claude wrote both Sprint 2 docs, added `SCORE_UPDATE` (§3.11) to the blueprint and round scoring (§4.1) to the FSM, and compiled the diagram with the Mermaid CLI (light and dark) to check for syntax errors. Sending under the lock **replaces** the Sprint 1 rule "no socket I/O while holding the lock" in §3.4 and §4 of this file; the 5 s socket timeout bounds how long a send can hold the lock |
| 2026-10-19 | Claude | Claude pointed out that the official rules give the 5 points only for five in a row. I checked the Pente rulebook (bonus for five in a row; 1 per capture; 1 per four still on the board) and asked to match it. Also asked why the Sprint 1 "no socket I/O under the lock" rule was reversed | scoring in all docs | Changed: 5-point bonus only for `FIVE_IN_A_ROW` (renamed `win_bonus` to `five_bonus`); no bonus for wins by captures, a full board, or forfeits. Claude explained the trade-off: the Sprint 1 rule keeps the lock short, but needs extra machinery to keep broadcasts in order and stop two threads writing to one socket at once |
