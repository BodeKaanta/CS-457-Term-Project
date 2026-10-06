# Pente Application Protocol Blueprint (PAP v1)

**Project:** CS 457 Term Project: Networked Pente
**Author:** Bode Kaanta
**Sprint:** 1 (Application Protocol & FSM Design)
**Related docs:** [`fsm_specification.md`](fsm_specification.md) · [`ai_prompts.md`](ai_prompts.md) · [`../sow_template.md`](../sow_template.md)

This document is the single source of truth for every byte exchanged between `client.py` and `server.py`. Any implementation (human- or AI-written) that disagrees with this document is wrong.

---

## 1. Transport, Serialization & Framing

| Property | Decision |
|---|---|
| Transport | TCP (IPv4), single persistent connection per client |
| Server endpoint | `server.kaanta.edu` : **`5457`** (resolved via R2 DNS → `192.168.20.100`) |
| Serialization | JSON object, UTF-8 encoded |
| Framing | **Newline-delimited JSON (NDJSON)**: every message is exactly one JSON object followed by one `\n` (`0x0A`) |
| Max frame size | **8192 bytes** including the `\n` |
| Protocol version | `1` |
| Client interface | **Terminal only.** The board is printed as text and moves are typed as `row col`. No GUI, no 2D/3D graphics (CML lab nodes are console-only) |

### 1.1 Framing Rule (normative)

1. **Sender:** serialize the message with `json.dumps(msg, separators=(",", ":"))` (compact form, no pretty-printing, so the JSON itself never contains a raw newline), encode as UTF-8, append `b"\n"`, and send with `sock.sendall()`.
2. **Receiver:** keep a per-connection byte buffer. After each `recv()`, append the bytes to the buffer, then repeatedly split on the **first** `b"\n"`:
   - bytes before the `\n` = one complete frame → decode UTF-8 → `json.loads()` → dispatch.
   - bytes after the `\n` stay in the buffer (they are the start of the next message).
   - If no `\n` is present, stop and wait for the next `recv()`.
3. **Literal newlines inside strings are impossible on the wire.** JSON encodes a newline character inside a string as the two-character escape `\\n`, so the only raw `0x0A` byte in a frame is the terminator.
4. **Empty lines** (`\n` with nothing before it) are ignored.
5. **Oversized frames:** if the buffer grows past 8192 bytes without a `\n`, the receiver sends `ERROR` (`FRAME_TOO_LARGE`, `fatal: true`) and closes the connection. This stops a broken or malicious peer from exhausting memory.

This rule is deterministic regardless of how TCP slices the stream: it handles **coalescing** (many messages in one `recv()`) and **fragmentation** (one message across many `recv()`s) identically.

### 1.2 Wire Stream Examples

Raw bytes on the wire are shown as text; `\n` represents the single byte `0x0A`.

**Example A: Continuous stream (server → client, three back-to-back messages):**

```text
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"players_connected":1,"players_needed":2,"message":"Waiting for Player 2..."},"timestamp":1791266400}\n{"msg_type":"GAME_START","player_id":"SERVER","payload":{"your_player_id":"P1","your_alias":"Alice","opponent_alias":"Bob","session_token":"9f2c4a1be07d4c3a","board_size":19,"first_player":"P1","stones_to_win":5,"captures_to_win":5,"resumed":false},"timestamp":1791266412}\n{"msg_type":"STATE_UPDATE","player_id":"SERVER","payload":{...},"timestamp":1791266412}\n
```

**Example B: Coalescing.** The client sends two messages quickly and the server's **single** `recv(4096)` returns both:

```text
recv() #1 → {"msg_type":"MOVE","player_id":"P1","payload":{"row":9,"col":9},"timestamp":1791266420}\n{"msg_type":"DISCONNECT","player_id":"P1","payload":{"reason":"USER_QUIT"},"timestamp":1791266421}\n
```

Buffer processing: split at the first `\n` → dispatch `MOVE`; split at the second `\n` → dispatch `DISCONNECT`; buffer is now empty.

**Example C: Fragmentation.** One `MOVE` arrives across **three** `recv()` calls:

```text
recv() #1 → {"msg_type":"MO
recv() #2 → VE","player_id":"P2","payload":{"row":9,
recv() #3 → "col":10},"timestamp":1791266433}\n{"msg_type":"MO
```

| After | Buffer contents | Action |
|---|---|---|
| #1 | `{"msg_type":"MO` | no `\n` → wait |
| #2 | `{"msg_type":"MOVE","player_id":"P2","payload":{"row":9,` | no `\n` → wait |
| #3 | full MOVE + `\n` + `{"msg_type":"MO` | extract and dispatch MOVE; keep `{"msg_type":"MO` for the next message |

### 1.3 Reference Receiver Logic (Python)

```python
import json

MAX_FRAME = 8192

class FrameReader:
    """Accumulates TCP bytes and yields complete NDJSON messages."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        self.buffer.extend(data)
        messages = []
        while True:
            idx = self.buffer.find(b"\n")
            if idx == -1:
                if len(self.buffer) > MAX_FRAME:
                    raise FrameTooLarge()
                break
            line = bytes(self.buffer[:idx])
            del self.buffer[:idx + 1]          # drop the frame and its '\n'
            if not line.strip():
                continue                        # ignore empty lines
            messages.append(json.loads(line.decode("utf-8")))  # may raise -> MALFORMED_JSON
        return messages

def send_msg(sock, msg: dict) -> None:
    data = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
    sock.sendall(data)
```

`json.JSONDecodeError` / `UnicodeDecodeError` from one bad line produce an `ERROR` (`MALFORMED_JSON`) for that frame only. The rest of the buffer is still processed, because the `\n` boundary has already been found.

### 1.4 Server Endpoint & Port Selection

**Why port 5457?** It is an easy-to-remember tie to the course: **5** + **457** (CS 457). It sits in the *registered* port range (1024–49151), so the server does not need root/administrator rights, which ports below 1024 would require. It is not assigned to any common service, so it won't collide with anything on the lab nodes. Both the server and client read it from a constant (`PORT = 5457`) and accept a command-line override in case it is ever taken.

**How the endpoint is established:**

1. **Server (passive open):** `server.py` creates a TCP socket, sets `SO_REUSEADDR` (so a restart doesn't fail with "address already in use"), then calls `bind(("0.0.0.0", 5457))` to accept connections on every interface of the server node. It then calls `listen()` and loops on `accept()`. Each `accept()` returns a **new** socket dedicated to one client; the listening socket keeps waiting for others.
2. **Name resolution:** the client only knows the name `server.kaanta.edu`. It calls `socket.getaddrinfo("server.kaanta.edu", 5457)`, which asks the DNS server. In the CML topology (Sprint 4–5) that is Router R2, which answers `192.168.20.100`. During local development, `127.0.0.1` is passed on the command line instead.
3. **Client (active open):** the client calls `connect(("192.168.20.100", 5457))`. The OS picks a random ephemeral source port and performs the TCP 3-way handshake (SYN → SYN-ACK → ACK).
4. **Connection identity:** each connection is the unique 4-tuple *(client IP, client port, server IP, 5457)*. The server ties a player's seat to that connection. Its first application message must be `CONNECT` or `RECONNECT`.

---

## 2. Common Message Envelope

**Every** message, in both directions, is a JSON object with exactly these four top-level keys:

| Key | Type | Required | Description |
|---|---|---|---|
| `msg_type` | string (enum) | yes | One of the 10 types in §3. Case-sensitive, UPPER_SNAKE_CASE. |
| `player_id` | string \| null | yes | Sender identity. Server-originated messages use `"SERVER"`. Clients use their assigned role `"P1"` / `"P2"`, or `null` before a role has been assigned (`CONNECT`). |
| `payload` | object | yes | Type-specific body (may be `{}`), defined in §3. |
| `timestamp` | integer | yes | Unix epoch seconds at send time. Informational only; the server never trusts client clocks for game logic. |

**Validation rules (server side):**

- Unknown top-level keys are ignored (forward compatibility). Missing required keys → `ERROR MISSING_FIELD`.
- A client's `player_id` must match the role bound to **that socket**. The server identifies players by connection, never by the self-reported `player_id`. A mismatch → `ERROR PLAYER_ID_MISMATCH`.
- Board coordinates are 0-indexed: `row` 0–18 (top→bottom), `col` 0–18 (left→right). The center point is `(9, 9)`.

**Board encoding** (used in `STATE_UPDATE` and `GAME_OVER`): an array of **19 strings**, each **19 characters** long. `board[row][col]` is `"."` (empty), `"1"` (P1 stone) or `"2"` (P2 stone).

---

## 3. Message Type Specifications

| # | `msg_type` | Direction | Purpose |
|---|---|---|---|
| 1 | `CONNECT` | Client → Server | Join the game room with an alias |
| 2 | `LOBBY_WAIT` | Server → Client | Tell Player 1 the server is waiting for Player 2 |
| 3 | `GAME_START` | Server → Both clients | Game begins (or resumes); assigns roles P1/P2 and session tokens |
| 4 | `MOVE` | Client → Server | Active player places a stone |
| 5 | `STATE_UPDATE` | Server → Both clients | Authoritative board, captures, turn, and status broadcast |
| 6 | `ERROR` | Server → Client | Reject an invalid, out-of-turn, or malformed message |
| 7 | `DISCONNECT` | Client → Server | Intentional, graceful quit |
| 8 | `GAME_OVER` | Server → Both clients | Final result: who won, how (WIN / FORFEIT), and the final state |
| 9 | `RECONNECT` | Client → Server | Rejoin an in-progress game within the grace period |
| 10 | `PLAYER_STATUS` | Server → Client | Tell the remaining player that the opponent dropped or rejoined |

### 3.1 `CONNECT` (Client → Server)

Sent once, immediately after the TCP connection is established, to request a seat.

| Payload field | Type | Constraints |
|---|---|---|
| `alias` | string | 1–16 chars, `[A-Za-z0-9_]` only |
| `protocol_version` | integer | must equal `1` |

```json
{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Alice","protocol_version":1},"timestamp":1791266400}
```

Server responses: `LOBBY_WAIT` (first player), `GAME_START` to both (second player), or `ERROR` (`ROOM_FULL`, `INVALID_ALIAS`, `ALIAS_TAKEN`, `VERSION_MISMATCH`), each fatal, followed by a server close.

### 3.2 `LOBBY_WAIT` (Server → Client)

Sent to the first connected player while the room has one seat filled.

| Payload field | Type | Description |
|---|---|---|
| `players_connected` | integer | Always `1` when sent |
| `players_needed` | integer | Always `2` |
| `message` | string | Human-readable status for the console |

```json
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"players_connected":1,"players_needed":2,"message":"Waiting for Player 2..."},"timestamp":1791266400}
```

### 3.3 `GAME_START` (Server → Both clients)

Sent to **each** client individually (fields such as `your_player_id` differ per recipient). Role assignment: **the first player to `CONNECT` becomes `P1` and moves first**; the second becomes `P2`. Also re-sent to a player who successfully `RECONNECT`s, with `resumed: true`.

| Payload field | Type | Description |
|---|---|---|
| `your_player_id` | string | `"P1"` or `"P2"`, the role the client must use in all later `player_id` fields |
| `your_alias` | string | Echo of the client's alias |
| `opponent_alias` | string | Opponent's alias |
| `session_token` | string | 16-hex-char random token. Required to `RECONNECT`. The client saves it to `.pente_session.json` so it survives a client crash |
| `board_size` | integer | `19` |
| `first_player` | string | `"P1"` |
| `stones_to_win` | integer | `5` (five or more in a row) |
| `captures_to_win` | integer | `5` (captured pairs) |
| `resumed` | boolean | `false` on a new game, `true` after a reconnect |

```json
{"msg_type":"GAME_START","player_id":"SERVER","payload":{"your_player_id":"P2","your_alias":"Bob","opponent_alias":"Alice","session_token":"3b7e19c0a4f25d88","board_size":19,"first_player":"P1","stones_to_win":5,"captures_to_win":5,"resumed":false},"timestamp":1791266412}
```

Immediately after `GAME_START`, the server broadcasts an initial `STATE_UPDATE` (empty board, `turn_number: 0`, `active_player: "P1"`).

### 3.4 `MOVE` (Client → Server)

Place one stone. Only valid from the active player while the game status is `ACTIVE`.

| Payload field | Type | Constraints |
|---|---|---|
| `row` | integer | 0–18 |
| `col` | integer | 0–18 |

```json
{"msg_type":"MOVE","player_id":"P1","payload":{"row":9,"col":9},"timestamp":1791266420}
```

Server validation order (the first failure wins and produces an `ERROR`, non-fatal; the state is unchanged and the player stays in turn):

1. Game status is `ACTIVE` → else `GAME_NOT_ACTIVE`
2. Sender is the active player → else `NOT_YOUR_TURN`
3. `row` and `col` present and are integers (not bool/float/string) → else `INVALID_PAYLOAD`
4. `0 ≤ row, col ≤ 18` → else `OUT_OF_BOUNDS`
5. `board[row][col] == "."` → else `CELL_OCCUPIED`

A valid move is applied, captures are resolved, win conditions are checked, and the result is broadcast as `STATE_UPDATE` (and `GAME_OVER` if the game ended).

### 3.5 `STATE_UPDATE` (Server → Both clients)

The authoritative game snapshot. Clients **render only what the server sends**; they never apply moves locally.

| Payload field | Type | Description |
|---|---|---|
| `board` | array[19] of string(19) | Board encoding from §2 |
| `turn_number` | integer | Number of moves applied so far (starts at `0`) |
| `active_player` | string \| null | `"P1"`/`"P2"`; `null` while paused or after the game ends. Computed as `["P1","P2"][turn_number % 2]` |
| `captures` | object | `{"P1": int, "P2": int}`: captured **pairs** per player |
| `last_move` | object \| null | `{"player": str, "row": int, "col": int, "captured": [[r,c], ...]}`. `captured` lists the opponent stones removed by this move. `null` before the first move |
| `game_status` | string (enum) | `ACTIVE`, `PAUSED_RECONNECT`, or `FINISHED` |

```json
{"msg_type":"STATE_UPDATE","player_id":"SERVER","payload":{"board":["...................","...................","...................","...................","...................","...................","...................","........2..........",".........1.........",".........11..1.....","...................","...................","...................","...................","...................","...................","...................","...................","..................."],"turn_number":7,"active_player":"P2","captures":{"P1":1,"P2":0},"last_move":{"player":"P1","row":9,"col":10,"captured":[[9,11],[9,12]]},"game_status":"ACTIVE"},"timestamp":1791266455}
```

### 3.6 `ERROR` (Server → Client)

Sent **only** to the offending client. A non-fatal error never changes game state or turn order, and never crashes the server's receive loop.

| Payload field | Type | Description |
|---|---|---|
| `code` | string (enum) | See the table below |
| `detail` | string | Human-readable explanation |
| `fatal` | boolean | `true` → the server closes this connection after sending |
| `ref_msg_type` | string \| null | `msg_type` of the rejected message, if it could be parsed |

| `code` | Fatal | Trigger |
|---|---|---|
| `MALFORMED_JSON` | no | Frame is not valid UTF-8 JSON or is not a JSON object |
| `MISSING_FIELD` | no | A required envelope or payload key is absent |
| `UNKNOWN_MSG_TYPE` | no | `msg_type` is not one of the 10 defined types, or is a server-only type |
| `INVALID_PAYLOAD` | no | A field has the wrong data type |
| `PLAYER_ID_MISMATCH` | no | `player_id` differs from the role bound to this socket |
| `NOT_YOUR_TURN` | no | `MOVE` from the non-active player |
| `OUT_OF_BOUNDS` | no | `row`/`col` outside 0–18 |
| `CELL_OCCUPIED` | no | Target intersection is not empty |
| `GAME_NOT_ACTIVE` | no | `MOVE` while in lobby, paused, or finished |
| `INVALID_ALIAS` | yes | Alias fails the format rules |
| `ALIAS_TAKEN` | yes | Alias already used by the other connected player |
| `ROOM_FULL` | yes | Two players are already seated (and no slot is awaiting reconnect) |
| `VERSION_MISMATCH` | yes | `protocol_version` ≠ 1 |
| `INVALID_TOKEN` | yes | `RECONNECT` with an unknown or expired token |
| `FRAME_TOO_LARGE` | yes | More than 8192 bytes without a `\n` |

```json
{"msg_type":"ERROR","player_id":"SERVER","payload":{"code":"NOT_YOUR_TURN","detail":"It is P1's turn (turn 6).","fatal":false,"ref_msg_type":"MOVE"},"timestamp":1791266460}
```

### 3.7 `DISCONNECT` (Client → Server)

A graceful, intentional quit. After sending it, the client calls `sock.close()` (TCP FIN).

| Payload field | Type | Constraints |
|---|---|---|
| `reason` | string (enum) | `USER_QUIT` or `CLIENT_SHUTDOWN` |

```json
{"msg_type":"DISCONNECT","player_id":"P2","payload":{"reason":"USER_QUIT"},"timestamp":1791266470}
```

Server handling: an intentional quit is final. There is **no** grace period. If a game is in progress, the opponent wins immediately by forfeit (`GAME_OVER`, `win_reason: "OPPONENT_QUIT"`). If the player was in the lobby, their seat is freed. A `DISCONNECT` sent during the lobby is not an error.

### 3.8 `GAME_OVER` (Server → Both clients)

Final outcome. Sent after the final `STATE_UPDATE` (status `FINISHED`). After `GAME_OVER`, the server closes both connections and returns to the lobby (see FSM `CLEANUP`).

| Payload field | Type | Description |
|---|---|---|
| `result` | string (enum) | `WIN` (won by playing) or `FORFEIT` (won because the opponent quit or timed out) |
| `winner` | string | `"P1"` or `"P2"`; there is always a winner |
| `winner_alias` | string | Alias of the winner |
| `win_reason` | string (enum) | `FIVE_IN_A_ROW`, `FIVE_CAPTURES`, `BOARD_FULL`, `OPPONENT_QUIT`, `OPPONENT_TIMEOUT` |
| `final_captures` | object | `{"P1": int, "P2": int}` |
| `final_board` | array[19] of string(19) | Final board |
| `total_turns` | integer | Moves played |

```json
{"msg_type":"GAME_OVER","player_id":"SERVER","payload":{"result":"FORFEIT","winner":"P1","winner_alias":"Alice","win_reason":"OPPONENT_TIMEOUT","final_captures":{"P1":1,"P2":0},"final_board":["...................","...................","...................","...................","...................","...................","...................","........2..........",".........1.........",".........11..1.....","...................","...................","...................","...................","...................","...................","...................","...................","..................."],"total_turns":7},"timestamp":1791266550}
```

> **Why there is no `LOSE` or `DRAW` value:**
> - **No `LOSE`:** `GAME_OVER` is one broadcast describing the game, not one player's view of it. The `winner` field says who won, and each client compares it with its own `your_player_id` (from `GAME_START`) to print either "You win!" or "You lose." A loss is simply "`winner` is not me," so a separate value would be redundant.
> - **No `DRAW`:** Pente has no draws. In the theoretical case where all 361 intersections fill up with no five-in-a-row and nobody reaching 5 captures, the server still needs a defined ending. The player with more captured pairs wins (`win_reason: BOARD_FULL`). If captures are also tied, **P2 wins**, to offset P1's first-move advantage. This case should never come up in real play.

### 3.9 `RECONNECT` (Client → Server)

Sent as the **first** message on a **new** TCP connection by a player whose previous connection dropped, within the reconnect grace period (**60 seconds**).

| Payload field | Type | Constraints |
|---|---|---|
| `alias` | string | Must match the alias of the paused seat |
| `session_token` | string | Token from the original `GAME_START` |
| `protocol_version` | integer | must equal `1` |

```json
{"msg_type":"RECONNECT","player_id":"P2","payload":{"alias":"Bob","session_token":"3b7e19c0a4f25d88","protocol_version":1},"timestamp":1791266490}
```

On success the server binds the new socket to the old seat and sends that client `GAME_START` (`resumed: true`, same role and token). It sends the opponent `PLAYER_STATUS` (`RECONNECTED`), then broadcasts `STATE_UPDATE` (`ACTIVE`, same `active_player` as before the drop). On failure it sends `ERROR INVALID_TOKEN` (fatal).

### 3.10 `PLAYER_STATUS` (Server → Client)

Sent to the **remaining** player when the opponent's connection drops unexpectedly or comes back.

| Payload field | Type | Description |
|---|---|---|
| `subject_player` | string | `"P1"`/`"P2"`, the player whose status changed |
| `status` | string (enum) | `DISCONNECTED` or `RECONNECTED` |
| `grace_seconds_remaining` | integer \| null | Seconds left to reconnect (`null` on `RECONNECTED`) |
| `message` | string | Console text |

```json
{"msg_type":"PLAYER_STATUS","player_id":"SERVER","payload":{"subject_player":"P2","status":"DISCONNECTED","grace_seconds_remaining":60,"message":"Bob lost connection. Waiting up to 60s for them to rejoin..."},"timestamp":1791266475}
```

---

## 4. Typical Message Sequences

**Happy path:**

```text
Alice → S : CONNECT {alias:"Alice"}
S → Alice : LOBBY_WAIT
Bob   → S : CONNECT {alias:"Bob"}
S → Alice : GAME_START {your_player_id:"P1", ...}
S → Bob   : GAME_START {your_player_id:"P2", ...}
S → both  : STATE_UPDATE {turn_number:0, active_player:"P1"}
Alice → S : MOVE {9,9}         → S → both : STATE_UPDATE {active_player:"P2"}
Bob   → S : MOVE {9,9}         → S → Bob  : ERROR CELL_OCCUPIED (Bob still active)
Bob   → S : MOVE {8,8}         → S → both : STATE_UPDATE {active_player:"P1"}
...
Alice → S : MOVE (5th in row)  → S → both : STATE_UPDATE {FINISHED}, GAME_OVER {WIN, FIVE_IN_A_ROW}
S closes both sockets → back to lobby
```

**Abrupt drop and rejoin:**

```text
Bob's link drops (recv → ConnectionResetError / EOF without DISCONNECT)
S → Alice : PLAYER_STATUS {P2, DISCONNECTED, 60}
S → Alice : STATE_UPDATE {game_status:"PAUSED_RECONNECT", active_player:null}
Bob (new socket) → S : RECONNECT {alias:"Bob", session_token:"3b7e..."}
S → Bob   : GAME_START {resumed:true}
S → Alice : PLAYER_STATUS {P2, RECONNECTED}
S → both  : STATE_UPDATE {game_status:"ACTIVE", active_player:<same as before>}
(If 60 s elapse first → S → Alice : GAME_OVER {FORFEIT, OPPONENT_TIMEOUT})
```

---

## 5. Connection Termination & Socket Lifecycle

The server distinguishes **how** a connection ended, because the game reacts differently.

| Termination type | What is seen on the wire | How the server detects it | Game consequence |
|---|---|---|---|
| **Application-layer quit** | `DISCONNECT` message, then TCP FIN | `msg_type == "DISCONNECT"` is dispatched | Immediate forfeit (`OPPONENT_QUIT`); no grace period |
| **Clean transport close without `DISCONNECT`** (client exits / `sock.close()` / Ctrl-C) | TCP FIN (4-way teardown) | `recv()` returns `b""` (EOF) | Treated as an **unexpected drop** → `PAUSED_RECONNECT` (60 s grace) |
| **Abrupt termination** (`kill -9`, power loss, CML link cut) | TCP RST, or nothing at all | `ConnectionResetError` / `ConnectionAbortedError` on `recv()`, `BrokenPipeError` on `sendall()`, or keepalive timeout (`TimeoutError` / `OSError`) | Treated as an **unexpected drop** → `PAUSED_RECONNECT` (60 s grace) |

### 5.1 The TCP EOF (0-byte) Rule

When the peer closes cleanly, `recv()` **does not raise**. It returns `b""`. Every receive loop must check this, or it spins forever at 100 % CPU because `recv()` keeps returning `b""` immediately.

```python
def client_handler(sock, conn_id):
    reader = FrameReader()
    try:
        while True:
            data = sock.recv(4096)
            if not data:                                  # EOF: peer sent FIN
                fsm.handle_connection_lost(conn_id, cause="EOF")
                break
            try:
                for msg in reader.feed(data):
                    fsm.dispatch(conn_id, msg)            # DISCONNECT handled in here
            except json.JSONDecodeError:
                send_error(sock, "MALFORMED_JSON", fatal=False)   # loop continues
            except FrameTooLarge:
                send_error(sock, "FRAME_TOO_LARGE", fatal=True)
                fsm.handle_connection_lost(conn_id, cause="PROTOCOL_VIOLATION")
                break
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, OSError) as e:
        logger.warning(f"[{conn_id}] connection lost abruptly: {e!r}")
        fsm.handle_connection_lost(conn_id, cause="ABRUPT")
    finally:
        sock.close()                                      # always release the fd
```

### 5.2 Socket Exceptions & Where They Are Caught

| Exception | Meaning | Caught in |
|---|---|---|
| `ConnectionResetError` | Peer sent RST (crashed / killed) | receive loop |
| `ConnectionAbortedError` | Local stack aborted the connection (common on Windows) | receive loop |
| `BrokenPipeError` | `sendall()` to a peer that is already gone | **every** send (`send_msg` wrapper) |
| `TimeoutError` / `OSError` (`ETIMEDOUT`) | Keepalive probes unanswered (silent link loss) | receive loop |

Sends to one player must never crash the handling of the other. The `broadcast()` helper wraps each `send_msg` individually. A send failure marks that connection as lost and triggers `handle_connection_lost()` for it, then continues with the next recipient.

### 5.3 Detecting Silent Link Loss (cut CML link)

If a router link is severed, neither FIN nor RST may ever arrive, and an idle `recv()` would block forever. Every accepted socket therefore enables TCP keepalive with short timers, so a dead peer is detected in roughly 25 s:

```python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)   # start probing after 10 s idle
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)   # probe every 5 s
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)     # 3 missed probes → error
```

(Linux options. The CML nodes run Linux. On the Windows dev machine these constants may be missing and are skipped with `hasattr()`.)

### 5.4 Server-Side Cleanup Guarantees

Whatever the cause, `handle_connection_lost(conn_id, cause)` does the following:

1. Marks the seat as disconnected and closes the socket (idempotent: safe to call twice).
2. Feeds the matching event to the FSM (`DISCONNECT_MSG`, `CONN_LOST`), which decides between forfeit, pause, or lobby cleanup (see `fsm_specification.md`).
3. Notifies the remaining player (`PLAYER_STATUS` or `GAME_OVER`) if they are still connected.
4. When the game finishes, `CLEANUP` cancels the grace timer, discards session tokens, resets the board, closes all sockets, and returns to `WAITING_FOR_PLAYERS`. Nothing from the old game leaks into the next one.

### 5.5 Client-Side Behavior

- On user quit: send `DISCONNECT`, then `sock.close()`.
- On EOF / `ConnectionResetError` with no `GAME_OVER` received: print "Connection lost", then retry `RECONNECT` (using the saved `.pente_session.json`) every 5 s, for up to 60 s.
- On `GAME_OVER`: delete `.pente_session.json`, print the result, and close.
