# Pente Server Concurrency & Game Behavior Architecture

**Project:** CS 457 Term Project: Networked Pente
**Author:** Bode Kaanta
**Sprint:** 2 (Game Behavior & Server Concurrency Architecture Design)
**Related docs:** [`sequence_diagram.md`](sequence_diagram.md) (Mermaid message flow) · [`protocol_blueprint.md`](protocol_blueprint.md) (message schemas) · [`fsm_specification.md`](fsm_specification.md) (game states and transitions) · [`ai_prompts.md`](ai_prompts.md)

This document defines **how the server runs several client connections at the same time without blocking**, how it keeps the shared game state safe from race conditions, how turns are enforced, and how the board, scores, and leaderboard reach both players. The protocol and FSM from Sprint 1 say *what* happens. This document says *which thread does it, and under which lock*.

## Summary

The server uses **multi-threading**. The main thread only accepts connections, and every client gets its own handler thread. A handler waiting on `recv()` blocks only itself, so one slow or silent player never stalls the server. All shared data (the board, turn, captures, seats, connection list, and leaderboard) is protected by **one `threading.Lock`**, so only one thread can change the game at a time. That rules out two moves being applied at once and makes deadlock from lock ordering impossible. When a move arrives, the receiving thread takes the lock and checks that it is the sender's turn and the move is legal. It updates the board and **sends the same `STATE_UPDATE` to both players before releasing the lock**, so both screens always show the same sequence of states. When a game ends, the server scores the round with Points Pente rules, adds the points to a leaderboard kept in memory for as long as the server runs, and sends `GAME_OVER` and `SCORE_UPDATE` to both players.

| If you want... | Read |
|---|---|
| Which threads exist and why | §1 |
| A connection from `accept()` to close | §2 |
| The lock, its rules, and the race conditions it prevents | §3, §4 |
| How broadcasts stay in sync, and turn enforcement | §5 |
| How scores are stored and sent | §6 |
| The message flow as a diagram | [`sequence_diagram.md`](sequence_diagram.md) |

---

## 1. Concurrency Model: Multi-Threading (Option A)

| Decision | Choice |
|---|---|
| Model | **One worker thread per client connection** (`threading.Thread`), plus the main thread that accepts connections |
| Why not `socketserver.ThreadingTCPServer` | It hides the accept loop and the per-connection setup. Writing the `accept()` loop by hand makes every step of the connection lifecycle (§2) visible and lets me add the connection cap, keepalive, and timeouts in one place |
| Shared-state protection | **One `threading.Lock`** (`state_lock`) guards every shared structure (§4) |
| Broadcast strategy | The thread that changes the state **sends to both clients while still holding the lock** (§5) |
| Language / libraries | Python 3.11 standard library only (`socket`, `threading`, `json`, `logging`, `dataclasses`, `secrets`, `time`) |

**Why threading fits this game:** each player spends most of the game *thinking*, so each handler thread spends most of its life blocked in `recv()`. A blocked `recv()` only parks **that one thread**. The OS wakes it when bytes arrive. The main thread, the other player's thread, and the grace timer all keep running. That is what lets one server process serve two players (and turn away extra clients) without any single slow or silent client stalling everyone else. Threading is also the model used by many real-world servers (for example, thread-per-request web servers and database connection handlers), which is why I chose it over `selectors`.

### 1.1 Thread Inventory

| Thread | How many | Created by | Job | Ever holds `state_lock`? |
|---|---|---|---|---|
| **Main / acceptor** | 1 | Python at startup | `bind()`, `listen()`, loops on `accept()`, configures each new socket, starts its handler thread | Briefly, to register a connection |
| **Connection handler** (`conn-N`) | 1 per open TCP connection (max `MAX_CONNECTIONS`) | Main thread | Owns one socket: `recv()` loop → `FrameReader` → `server.on_message()` / `server.on_connection_lost()` | Yes, for every message it dispatches |
| **Grace timer** | 0 or 1 | `threading.Timer(60, ...)` started on an unexpected drop (FSM D2) | After 60 s with no `RECONNECT`, calls `server.on_grace_timeout(game_id, pause_seq)` | Yes, when it fires |

A full game with one drop and rejoin uses at most **1 main + 3 handlers + 1 timer = 5 threads** (P1, the dropped P2 thread that is exiting, and the new P2 thread). All handler threads are created with `daemon=True` so that Ctrl-C on the server never hangs waiting on a blocked `recv()`.

### 1.2 Configuration Constants

| Constant | Value | Purpose |
|---|---|---|
| `PORT` | `5457` | Listening port (blueprint §1.4) |
| `LISTEN_BACKLOG` | `5` | Kernel queue of not-yet-accepted connections |
| `MAX_CONNECTIONS` | `8` | Cap on open client sockets (= handler threads). 2 seats + a rejoining player + spare room for clients that will be turned away with `ROOM_FULL` |
| `SOCKET_TIMEOUT_SEC` | `5` | `sock.settimeout()` value. Bounds how long any `sendall()` can block (and therefore how long the lock can be held), and makes `recv()` wake up periodically (§3.2) |
| `HANDSHAKE_TIMEOUT_SEC` | `10` | A new connection must send `CONNECT` or `RECONNECT` within 10 s or it is closed. Stops idle sockets from holding threads forever |
| `GRACE_PERIOD_SEC` | `60` | Reconnect window (FSM D2/D7) |
| `LEADERBOARD_MAX_ENTRIES` | `10` | Rows sent in `SCORE_UPDATE` (keeps the frame far below 8192 bytes) |

---

## 2. Connection Lifecycle

Every connection goes through the same six phases, from the listening socket to teardown.

```text
 LISTEN ──► ACCEPT ──► REGISTER ──► HANDSHAKE ──► ACTIVE SERVICING ──► TEARDOWN
 (main)     (main)     (main,lock)  (handler)     (handler, lock per msg)  (handler)
```

### 2.1 Listen (main thread, once at startup)

1. `listen_sock = socket.socket(AF_INET, SOCK_STREAM)`
2. `setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)` so a restarted server can re-bind immediately.
3. `bind(("0.0.0.0", 5457))`, then `listen(LISTEN_BACKLOG)`.
4. The FSM enters `WAITING_FOR_PLAYERS / LOBBY_EMPTY` (FSM `INIT` → `WAITING_FOR_PLAYERS`).

### 2.2 Accept (main thread, forever)

```python
listen_sock.settimeout(1.0)                    # so Ctrl-C is noticed on Windows too
while not shutting_down:
    try:
        sock, addr = listen_sock.accept()      # blocks only the main thread
    except TimeoutError:
        continue                               # no new client this second; check shutting_down
    configure_socket(sock)                     # keepalive + settimeout (below), BEFORE any send
    if server.connection_count() >= MAX_CONNECTIONS:   # reads the count under state_lock
        try:
            send_msg(sock, make_error("ROOM_FULL", "Server is at capacity.", fatal=True))
        except OSError:
            pass                               # a failed reject must never stop the accept loop
        sock.close()                           # never registered; no other thread knows this socket
        continue
    conn = Connection(conn_id=next(conn_ids), sock=sock, addr=addr)
    server.register_connection(conn)           # takes state_lock (§2.3)
    threading.Thread(target=client_handler, args=(conn,),
                     name=f"conn-{conn.conn_id}", daemon=True).start()
```

`configure_socket()` enables TCP keepalive (`SO_KEEPALIVE`, `TCP_KEEPIDLE=10`, `TCP_KEEPINTVL=5`, `TCP_KEEPCNT=3`), sets `TCP_USER_TIMEOUT=25000` ms, and calls `sock.settimeout(SOCKET_TIMEOUT_SEC)`. Options the OS lacks are skipped with `hasattr()` (blueprint §5.3). Keepalive only probes an **idle** connection. `TCP_USER_TIMEOUT` covers the other case, where the server has sent data (for example a `STATE_UPDATE`) that the peer never acknowledges because the link was cut. Without it, Linux would keep retransmitting for about 15 minutes before giving up.

The over-capacity `ROOM_FULL` is the **only** send that happens without `state_lock`. It is safe because that socket was never registered, so no other thread can reach it. `listen_sock.settimeout(1.0)` makes `accept()` return once a second, so the loop can notice `shutting_down`. On Windows, a blocking `accept()` would otherwise ignore Ctrl-C until a client connects.

### 2.3 Register (main thread, under `state_lock`)

`register_connection()` adds the `Connection` to `server.connections` (a `dict[int, Connection]`). Registration is **not** a seat: the connection has no `P1`/`P2` role yet. The connection count used for the cap is read under the same lock.

### 2.4 Handshake (handler thread)

The handler starts its receive loop. The first message must be `CONNECT` or `RECONNECT`, and it must arrive within `HANDSHAKE_TIMEOUT_SEC`. The FSM then decides, under the lock:

- `CONNECT` → seat as `P1` (L1), seat as `P2` (L3), or reject (`ROOM_FULL`, `ALIAS_TAKEN`, `INVALID_ALIAS`, `VERSION_MISMATCH`, all fatal).
- `RECONNECT` → re-bind to the paused seat (D4) or reject (`INVALID_TOKEN`, fatal).
- Anything else before a seat is assigned → the matching non-fatal `ERROR` (L5). The handshake clock keeps running.

When a seat is assigned, `seat.conn_id = conn.conn_id`. From then on the server identifies the player **by which connection a message arrived on**, never by the `player_id` field inside it (blueprint §2).

### 2.5 Active Servicing (handler thread)

```python
def client_handler(conn: Connection) -> None:
    reader = FrameReader()                     # thread-confined: only this thread touches it
    opened_at = time.monotonic()
    try:
        while True:
            # Checked every pass, whether or not data arrived, so a client that keeps
            # sending junk instead of CONNECT/RECONNECT can't hold this thread forever.
            if not conn.seated and time.monotonic() - opened_at > HANDSHAKE_TIMEOUT_SEC:
                break
            try:
                data = conn.sock.recv(4096)    # lock is NOT held here
            except TimeoutError as e:
                if e.errno is not None:        # ETIMEDOUT from keepalive / TCP_USER_TIMEOUT: peer is gone
                    raise
                continue                       # our own 5 s settimeout(): the player is just thinking
            if not data:                       # EOF (peer sent FIN)
                server.on_connection_lost(conn, cause="EOF")
                break
            try:
                frames = reader.feed(data)
            except FrameTooLarge:
                server.on_fatal_protocol_error(conn, "FRAME_TOO_LARGE")
                break
            for frame in frames:
                server.on_message(conn, frame) # acquires state_lock inside
            if conn.close_requested:           # fatal ERROR or DISCONNECT was processed
                break
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
            TimeoutError, OSError) as e:
        server.on_connection_lost(conn, cause="ABRUPT")
    finally:
        server.unregister_connection(conn)     # takes state_lock; idempotent
        conn.sock.close()                      # the owning thread is the only one that close()s
```

This is the Sprint 1 handler (blueprint §5.1) with three refinements: the `settimeout()` wake-up (§3.2), the handshake deadline, and socket ownership (R5). `on_connection_lost()` is idempotent. It checks `conn.lost_handled` under the lock, so calling it from both the EOF path and the exception path is harmless. `conn.seated` is set by the FSM, under the lock, when L1, L3 or D4 binds the connection to a seat.

### 2.6 Teardown

A connection ends in one of the ways below. Every one ends in the handler's `finally` block, so there is exactly **one** place where a *registered* socket is closed and unregistered. The only other `close()` is the over-capacity reject in §2.2, on a socket that was never registered.

| Trigger | Who notices | What happens |
|---|---|---|
| Client sends `DISCONNECT` | Its handler, inside `on_message` | FSM D1 (forfeit) or L6 (lobby); sets `conn.close_requested` → loop exits |
| Client closes / crashes (FIN or RST) | Its handler: `recv()` returns `b""` or raises | `on_connection_lost` → FSM D2 (pause) or L6 / D8 |
| Silent link loss (cut CML link) | Its handler: keepalive (idle link) or `TCP_USER_TIMEOUT` (unacknowledged data) raises `TimeoutError` (errno `ETIMEDOUT`) | Same as above, after about 25 s |
| Client never completes the handshake | Its handler: `HANDSHAKE_TIMEOUT_SEC` check | Loop exits; the connection was never seated, so the FSM is not involved |
| Server sends a fatal `ERROR` (`ROOM_FULL`, `ALIAS_TAKEN`, `INVALID_ALIAS`, `VERSION_MISMATCH`, `INVALID_TOKEN`, `FRAME_TOO_LARGE`) | Its handler, inside `on_message` / `on_fatal_protocol_error` | Sets `conn.close_requested` → loop exits |
| A **send** to this client fails (in another thread's broadcast) | The sending thread, under the lock | Marks `conn.dead = True` and calls `sock.shutdown(SHUT_RDWR)`. That wakes this connection's own handler with EOF/error, and the handler then runs `on_connection_lost` normally (§5.3) |
| Server finishes a game (FSM `CLEANUP`) | The thread running `CLEANUP` | `shutdown(SHUT_RDWR)` on both player sockets. Their handlers wake, see they no longer hold a seat (FSM X1, ignored), close, and exit |

**Server shutdown (Ctrl-C):** the main thread catches `KeyboardInterrupt`, sets `shutting_down`, closes `listen_sock`, and under the lock calls `shutdown(SHUT_RDWR)` on every registered connection. Handler threads are daemons, so the process exits even if one is mid-`recv()`.

---

## 3. Thread Safety Strategy

The server uses three techniques together. Each shared thing is protected by exactly one of them.

| Technique | Applies to | Idea |
|---|---|---|
| **Mutual exclusion** (`state_lock`) | Everything in §4 table "Shared" | Only one thread at a time may read-modify-write shared state |
| **Thread confinement** | Each connection's `FrameReader` buffer, its `recv()` calls, its handshake clock | Data that only one thread ever touches needs no lock |
| **Single owner for socket close** | Each client socket | Only the owning handler thread calls `close()`. Other threads may only `sendall()` (under the lock) or `shutdown()` |

### 3.1 The One Lock

```python
class PenteServer:
    def __init__(self) -> None:
        self.state_lock = threading.Lock()   # guards EVERYTHING listed in §4 "Shared"
        self.game = GameState()              # per game, reset by CLEANUP
        self.leaderboard: dict[str, PlayerRecord] = {}   # per server process, survives CLEANUP
        self.connections: dict[int, Connection] = {}
        self.game_id = 0                     # +1 every time a game starts
        # self.game.pause_seq: +1 on every pause (D2), checked by the stale-timer guard
```

**Why one lock instead of several smaller ones** (for example, a board lock, a seats lock, and a leaderboard lock):

- Almost every event touches several structures at once. A legal move changes the board, captures, `turn_number`, and `last_move`. A game ending changes the game state, the leaderboard, the seats, and the connection list. With separate locks, a thread would need several locks at once, and two threads taking them in different orders can **deadlock**. With one lock, deadlock from lock ordering is impossible.
- The work done under the lock is tiny: a capture check looks at 8 directions, a win check at 4 lines, and scoring the fours scans 361 cells × 4 directions once per game. Each critical section finishes in microseconds plus two small `sendall()` calls (under 1 KB each for moves). With two players there is nothing to gain from finer locking.
- The FSM is the only code that mutates state, and every FSM entry point takes the same lock. So **every state transition is atomic**: no thread can ever see a half-applied move.

The lock is a plain `threading.Lock` (not an `RLock`). To make re-entry impossible, the code follows a naming rule: public entry points (`on_message`, `on_connection_lost`, `on_grace_timeout`, `register_connection`, `unregister_connection`, `on_fatal_protocol_error`) acquire the lock with `with self.state_lock:`. Every private helper whose name ends in `_locked` (`_dispatch_locked`, `_connection_lost_locked`, `_send_locked`, `_broadcast_locked`, `_score_round_locked`, `_cleanup_locked`) **assumes the lock is already held and never acquires it**. Entry points only ever call `_locked` helpers, never other entry points. For example, `on_connection_lost()` and `on_fatal_protocol_error()` both call `_connection_lost_locked()`, so neither tries to take the lock twice.

`PenteServer` *is* the FSM object from `fsm_specification.md`: `fsm.dispatch(event)` there is `_dispatch_locked()` here.

### 3.2 Rules That Keep the Lock Safe

| # | Rule | What it prevents |
|---|---|---|
| R1 | **Never hold the lock during `recv()`.** Each handler reads bytes and splits frames with no lock held, and only takes the lock to dispatch a complete message | One thinking player would freeze the whole server |
| R2 | **Every `sendall()` to a registered socket happens while holding the lock**, through `_send_locked()` | Two threads writing to the same socket at once could interleave bytes and corrupt NDJSON frames. It also gives one global order of messages (§5) |
| R3 | **Every socket has a 5 s timeout**, so a `sendall()` under the lock is bounded | A client that stops reading could otherwise block a send, and the lock, forever |
| R4 | **Never `join()` a thread or wait on another lock while holding `state_lock`.** `Timer.cancel()` is allowed because it returns immediately | Deadlock |
| R5 | **Only the owning handler `close()`s a socket.** Other threads call `shutdown(SHUT_RDWR)` | Closing a socket another thread is blocked on is undefined across platforms, and the file descriptor number could be reused by a new connection |
| R6 | **Everything an event changes is done in one critical section** (validate → mutate → build messages → send) | Other threads seeing a half-updated state |
| R7 | **Timer callbacks re-check the state before acting** (stale-timer guard: `state == PAUSED_RECONNECT`, same `game_id`, same `pause_seq`; §3.4) | A timer that already fired forfeiting a game that was just resumed, or a later pause of the same game |

R3 needs one trick because `settimeout()` also applies to `recv()`. A `recv()` that times out is not an error. It just means the player hasn't typed anything for 5 s. The handler tells the two kinds of `TimeoutError` apart by `errno`: Python's own socket timeout has `errno = None`, while a dead-peer keepalive timeout has `errno = ETIMEDOUT`. The periodic wake-up is also what enforces `HANDSHAKE_TIMEOUT_SEC`.

### 3.3 Critical Sections (lock acquire → release)

Each row is one `with self.state_lock:` block. Nothing in the "Under the lock" column ever runs without the lock.

| Entry point (thread) | Acquire when | Under the lock | Release after |
|---|---|---|---|
| `register_connection` (main) | A socket was accepted | Cap check, add to `connections` | Added |
| `on_message` (handler) | One complete frame is ready | Decode, validate envelope, check `player_id` against the socket's seat, FSM dispatch (L/G/D tables), build all resulting messages, `_send_locked` / `_broadcast_locked` them, and on game end: score the round, update the leaderboard, `CLEANUP` | Last message sent |
| `on_connection_lost` (handler) | EOF / RST / keepalive timeout | `_connection_lost_locked()`: idempotency check, mark seat disconnected, FSM D2 / D3 / D8 / L6 (or X1 if the connection has no seat), `pause_seq += 1` and start the grace `Timer` on D2, send `PLAYER_STATUS` + `STATE_UPDATE` to the remaining player | Messages sent |
| `on_grace_timeout(game_id, pause_seq)` (timer) | The 60 s timer fires | Stale-timer guard, FSM D7: forfeit, score the round, update the leaderboard, broadcast `STATE_UPDATE` / `GAME_OVER` / `SCORE_UPDATE`, `CLEANUP` | Cleanup done |
| `on_fatal_protocol_error` (handler) | `FrameTooLarge` | Send `ERROR FRAME_TOO_LARGE` (fatal), then call `_connection_lost_locked(conn, cause="PROTOCOL_VIOLATION")` in the same critical section (the helper, not the entry point, so the lock isn't taken twice) | Done |
| `unregister_connection` (handler) | Handler is exiting | Remove from `connections` (if present) | Removed |

### 3.4 Race Conditions Considered

| Race | What could go wrong without protection | How the design handles it |
|---|---|---|
| **Both players send `MOVE` at the same moment** | Both threads read `active_player = "P1"`, both place stones, and the turn counter jumps by 2 or the board is corrupted | Both handlers call `on_message`, and the lock admits one at a time. If P2's thread gets the lock first: `NOT_YOUR_TURN`, nothing changes. Then P1's move is applied. If P1's thread gets it first, P1's move is applied and the turn flips to P2. P2's message is then processed as a normal move on P2's turn. That is correct, because the server's order of arrival *is* the order of play. The client UI only accepts input when the last `STATE_UPDATE` says it is your turn, so this case only comes up with a buggy or modified client |
| **Two new clients send `CONNECT` at the same time while P1 waits** | Both are seated as `P2` | The lock serializes them: the first gets `P2` (L3), the second sees a full room and gets `ROOM_FULL` (L7) |
| **A move is being applied while the opponent's link drops** | The drop handler pauses the game in the middle of the move | The drop handler waits on the lock. The move finishes and is broadcast (the send to the dead player fails and marks it `dead`). Then the drop is processed from `PLAYER_TURN` → `PAUSED_RECONNECT` (D2) |
| **`RECONNECT` arrives at 59.99 s while the timer fires at 60 s** | The timer forfeits a game that was just resumed | `RECONNECT` gets the lock first, calls `timer.cancel()`, and resumes the game. `cancel()` cannot stop a callback that has already started, so the timer thread may now be waiting on the lock. When it gets the lock, the **stale-timer guard** sees `state != PAUSED_RECONNECT` (or a different `game_id` or `pause_seq`) and returns without doing anything. `pause_seq` matters if Bob drops *again* before the old timer gets the lock: the game is paused again with the same `game_id` and the same paused player, but `pause_seq` has gone up, so the old timer still knows it is stale and only the new 60 s timer counts. If instead the timer gets the lock first, it forfeits the game and runs `CLEANUP`. The late `RECONNECT` then finds no paused seat and gets `INVALID_TOKEN` |
| **Two `RECONNECT`s with the same token** | Two sockets bound to one seat | The first wins (D4) and the seat becomes connected. The second finds no paused seat → `INVALID_TOKEN` (fatal) |
| **A send fails in the middle of a broadcast** | An exception escapes and the other client never gets the message, or the thread re-enters the lock to handle the drop and deadlocks | `_send_locked` catches the error per recipient, marks that connection `dead`, calls `shutdown()`, and continues with the next recipient. The drop is handled later by that connection's own handler (§5.3), so the lock is never re-entered |
| **The leaderboard is read while a game ends** | A `SCORE_UPDATE` mixes old and new totals | Scoring, leaderboard updates, and building `SCORE_UPDATE` all happen in the same critical section as `GAME_OVER` |
| **A handler is still processing after `CLEANUP` reset the game** | An old connection affects the new game | `CLEANUP` clears all seats. A drop from a connection that holds no seat is ignored, and a `MOVE` from one gets `GAME_NOT_ACTIVE` (FSM X1), even if a new game has already started. `game_id` tells old timers apart from new games |

---

## 4. Shared Resources

| Resource | Type | Lifetime | Written by | Read by | Protection |
|---|---|---|---|---|---|
| `game.state` | FSM state name | Per game | All FSM entry points | All FSM entry points | `state_lock` |
| `game.board` | 19×19 `list[list[str]]` | Per game | `on_message` (G7) | `on_message`, message builders | `state_lock` |
| `game.turn_number` → `active_player` | `int` | Per game | `on_message` (G11) | Turn check (G2/G3), builders | `state_lock` |
| `game.captures` | `{"P1": int, "P2": int}` | Per game | `on_message` (G7) | Win check (G9), `STATE_UPDATE`, scoring | `state_lock` |
| `game.last_move` | `dict` or `None` | Per game | `on_message` (G7) | `STATE_UPDATE` builder | `state_lock` |
| `game.seats` | `{"P1": Seat, "P2": Seat}` (alias, `conn_id`, token, connected) | Per game | L1, L3, D1–D8, `CLEANUP` | Every dispatch (identity), broadcasts | `state_lock` |
| `game.paused_player`, `game.pause_seq`, `game.grace_timer` | `str`, `int`, `threading.Timer` | Per game | D2, D4, D7, `CLEANUP` | Timer guard, D4 | `state_lock` |
| `game_id` | `int` | Server process | Game start (G1) | Timer guard | `state_lock` |
| `leaderboard` | `dict[str, PlayerRecord]` | **Server process** (survives `CLEANUP`) | Round scoring (§6) | `SCORE_UPDATE` builder | `state_lock` |
| `connections` | `dict[int, Connection]` | Server process | Register / unregister | Cap check, shutdown | `state_lock` |
| Each client socket (send side) | `socket.socket` | Per connection | `_send_locked` from any thread | — | `state_lock` (R2) |
| Each client socket (`recv` / `close`) | `socket.socket` | Per connection | Owning handler only | Owning handler only | Thread confinement + single owner (R5) |
| Each `FrameReader` buffer | `bytearray` | Per connection | Owning handler only | Owning handler only | Thread confinement |

---

## 5. Broadcast Design (Turn & State Synchronization)

### 5.1 Who Sends

Any thread can send on any socket. When P1's move is applied, **P1's handler thread** sends `STATE_UPDATE` to *both* P1 and P2. P2's handler is busy sitting in `recv()` and is not involved. In the same way, the grace timer thread sends the forfeit messages, and P2's thread sends the result when P2 quits.

### 5.2 How "Synchronous" Is Guaranteed

```python
def _broadcast_locked(self, msg: dict) -> None:
    data = encode_frame(msg)                  # built ONCE: both players get identical bytes
    for pid in ("P1", "P2"):
        seat = self.game.seats.get(pid)
        if seat and seat.connected:
            self._send_bytes_locked(self.connections[seat.conn_id], data)

def _send_bytes_locked(self, conn: Connection, data: bytes) -> None:
    if conn.dead:
        return
    try:
        conn.sock.sendall(data)               # bounded by SOCKET_TIMEOUT_SEC (R3)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
            TimeoutError, OSError):
        conn.dead = True                      # deferred: its own handler reports the drop
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
```

Because the whole event (validate → mutate → encode → send to P1 → send to P2) runs inside **one** critical section:

1. **Same content:** both players receive the exact same encoded bytes for every broadcast.
2. **Same order:** no other state change can slip in between the send to P1 and the send to P2. Every broadcast from every thread is sent in the order its critical section ran. TCP keeps that order on each connection. So both clients see the **identical sequence** of states: turn 6, then turn 7, then turn 8, never 8 before 7.
3. **No gap:** the lock is released only after both sends finish. So the next event (for example, P2's reply move) can't be processed before P2 has been sent the board it is replying to.

The network can still deliver the two copies a few milliseconds apart (they are separate TCP connections), but neither client can ever be *ahead of* or *different from* the other.

**Trade-off accepted:** the lock is held during two `sendall()` calls. Each message is small (a `STATE_UPDATE` is about 600 bytes) and fits in the kernel's send buffer, so `sendall()` normally returns right away without waiting on the network. The worst case (a client that stopped reading) is capped at `SOCKET_TIMEOUT_SEC = 5` by R3, after which that client is treated as dropped. The other common design, a per-client outbound queue with a writer thread, would remove even that wait, but at the cost of a second thread per client. That is not worth it for two players.

### 5.3 Send Failures During a Broadcast

A failed send never interrupts the broadcast to the other player and never re-enters the lock. The failing connection is marked `dead` and `shutdown()`. Its own handler thread then wakes up from `recv()` (EOF or error) and calls `on_connection_lost()` like any other drop. That way every disconnect, whether noticed while receiving or while sending, goes through **one** code path and one FSM transition (D2 / D3 / D8).

### 5.4 Turn Enforcement

Turn enforcement happens **inside the critical section, before any mutation**. The check uses the connection the bytes arrived on, never the client's claim:

```python
seat_id = self._seat_of_locked(conn)                 # "P1" / "P2" / None, from conn_id
if msg["player_id"] != seat_id:      -> ERROR PLAYER_ID_MISMATCH
if self.game.state != "PLAYER_TURN": -> ERROR GAME_NOT_ACTIVE
if seat_id != active_player(self.game.turn_number):  -> ERROR NOT_YOUR_TURN
# then payload types, bounds, occupancy (blueprint §3.4)
```

Every rejection sends `ERROR` **only to the sender**, changes nothing, and leaves the same player in turn (FSM G3–G6). A move is only applied after every check passes, and `turn_number` only increments after the move, captures, and win checks are complete (G11). So an out-of-turn or invalid move can never corrupt the board or the turn order.

---

## 6. Score & Leaderboard Synchronization (Spec 2.3)

### 6.1 Scoring Rules (Points Pente)

Scoring follows the official Points Pente rules from the Pente rulebook. Winning and scoring are separate: you **win** with five in a row or five captured pairs, and **points** are awarded **once, when a game (round) ends**, to **both** players:

| Points | For |
|---|---|
| **5** (bonus) | Getting **five in a row** (`win_reason: FIVE_IN_A_ROW`). Only the five-in-a-row bonus is worth 5. A win by `FIVE_CAPTURES` gets no bonus, but it already earns at least 5 capture points. `BOARD_FULL` and forfeit wins (`OPPONENT_QUIT`, `OPPONENT_TIMEOUT`) get no bonus either |
| **1 each** | Every pair the player captured during the game (`game.captures[player]`) |
| **1 each** | Every **four-in-a-row** of the player's stones still on the **final** board |

**What counts as a four:** exactly 4 of the player's stones, consecutive, in one straight line (horizontal, vertical, or either diagonal). A run of 5 or more is the winning line and is **not** also counted as a four. A four that was broken up by a capture during the game doesn't count, because only the final board is scanned. Fours are counted for both players, so the loser can still earn points.

```python
DIRECTIONS = [(0, 1), (1, 0), (1, 1), (1, -1)]   # →, ↓, ↘, ↙ (each line checked once)

def count_fours(board, stone) -> int:
    fours = 0
    for r in range(19):
        for c in range(19):
            if board[r][c] != stone:
                continue
            for dr, dc in DIRECTIONS:
                pr, pc = r - dr, c - dc               # only start counting at the start of a run
                if 0 <= pr < 19 and 0 <= pc < 19 and board[pr][pc] == stone:
                    continue
                length = 0
                while 0 <= r + length*dr < 19 and 0 <= c + length*dc < 19 \
                        and board[r + length*dr][c + length*dc] == stone:
                    length += 1
                if length == 4:
                    fours += 1
    return fours
```

**Example 1:** Alice (P1) wins with five in a row, has captured 2 pairs, and has one other four on the board → 5 + 2 + 1 = **8 points**. Bob (P2) captured 1 pair and has one four → 0 + 1 + 1 = **2 points**.

**Example 2:** Bob wins by capturing his fifth pair and has no fours → 0 + 5 + 0 = **5 points**. The `wins` column on the leaderboard still counts it as a win.

**Games that are abandoned** (both players drop, FSM D3/D8) end without a `GAME_OVER` and are **not** scored.

### 6.2 Where Scores Live in Server Memory

Scores live at three levels, each with a different lifetime:

| Level | Structure | Lifetime | Contents | How clients see it |
|---|---|---|---|---|
| **Live game score** | `game.captures` inside `GameState` | One game; reset by `CLEANUP` | Captured pairs per seat | Every `STATE_UPDATE` (`captures` field), so it is synced after **every** move |
| **Round result** | `RoundScore` per seat, computed once at game end | Built in the `GAME_OVER` critical section and then discarded | `five_bonus`, `capture_points`, `four_points`, `total` | `SCORE_UPDATE.round` |
| **Session leaderboard** | `server.leaderboard: dict[str, PlayerRecord]`, keyed by **alias** | The whole server process. **Not** reset by `CLEANUP`, lost only on server restart | Totals across every game played on this server | `SCORE_UPDATE.leaderboard` |

```python
@dataclass
class RoundScore:
    alias: str
    five_bonus: int         # 5 only if win_reason == FIVE_IN_A_ROW, else 0
    capture_points: int     # = captured pairs
    four_points: int        # = fours on the final board
    total: int

@dataclass
class PlayerRecord:
    alias: str
    games_played: int = 0
    wins: int = 0
    losses: int = 0
    points: int = 0         # running total of RoundScore.total
    captures: int = 0       # running total of captured pairs
    fours: int = 0          # running total of scored fours
```

The leaderboard is **outside** `GameState` on purpose. `CLEANUP` resets the whole `GameState` between games (FSM §2), and the leaderboard has to outlive that reset. Players are matched to their record by **alias**: if Alice plays, the server returns to the lobby, and Alice connects again with the same alias, her new results add to her existing record. Aliases are not passwords, so anyone could connect as "Alice". That is acceptable for this project.

### 6.3 Updating Scores at Round Completion

The moment a game is decided (FSM G8–G10 or D1/D7), the thread holding the lock runs these steps in **one** critical section:

1. Set `game.state = "GAME_OVER"` and record the winner and `win_reason`.
2. `_score_round_locked()`: for each seat, compute `RoundScore` from `win_reason`, `game.captures`, and `count_fours(final_board)`.
3. Update `leaderboard[alias]` for both players (`games_played += 1`, `wins` or `losses += 1`, add points, captures, and fours). New aliases get a fresh `PlayerRecord`.
4. Build the ranked leaderboard: sort by `points` (high → low), then `wins` (high → low), then `alias` (A → Z), and keep the top `LEADERBOARD_MAX_ENTRIES`.
5. Broadcast, in this order, to every **still-connected** player:
   1. `STATE_UPDATE` (`game_status: "FINISHED"`, `active_player: null`)
   2. `GAME_OVER` (result, winner, reason, final board)
   3. `SCORE_UPDATE` (`reason: "ROUND_COMPLETE"`, this round's points, updated leaderboard)
6. `CLEANUP` (`_cleanup_locked`): cancel the timer, revoke tokens, clear seats, `shutdown()` both player sockets, and reset `GameState`. The leaderboard is kept. State → `WAITING_FOR_PLAYERS / LOBBY_EMPTY`.

Because steps 1–6 can't be interleaved with anything else, a player can never receive a `SCORE_UPDATE` that disagrees with the `GAME_OVER` just before it, and the next game can't start until the leaderboard is final.

### 6.4 When `SCORE_UPDATE` Is Sent

| Moment | Reason value | Recipients | Why |
|---|---|---|---|
| Round completion (§6.3) | `ROUND_COMPLETE` | Both players (only the one still connected after a forfeit) | Shows the points just earned and the new standings |
| P1 seated in the lobby (L1), right after `LOBBY_WAIT` | `STANDINGS` | P1 | Gives the waiting player something to read |
| New game starts (G1), after both `GAME_START`s, before the first `STATE_UPDATE` | `STANDINGS` | Both | Both players see current standings before the first move |
| Rejoin (D4), right after `GAME_START (resumed)` | `STANDINGS` | The rejoining player | Their screen was lost with the old connection |

The full `SCORE_UPDATE` schema is in [`protocol_blueprint.md` §3.11](protocol_blueprint.md#311-score_update-server--client). The client never computes scores. It prints exactly what the server sends, the same way it renders boards.

---

## 7. Capacity & Requirements Check

| Sprint 2 acceptance criterion | Where it is met |
|---|---|
| At least 2 simultaneous clients without blocking the server main loop | §1, §2.2: the main thread only accepts. Each client has its own handler thread, and `recv()` never runs under the lock (R1). Up to `MAX_CONNECTIONS = 8` sockets are serviced at once, and extra clients are turned away with `ROOM_FULL` |
| Thread safety / mutex mechanism explicitly documented | §3 (single `threading.Lock`, rules R1–R7, critical sections), §4 (shared resources) |
| Turn enforcement prevents out-of-turn moves from corrupting state | §5.4, §3.4 row 1 |
| Mermaid sequence diagram with move requests, state updates, scores, joining, waiting, game end, and broadcasts | [`sequence_diagram.md`](sequence_diagram.md) |
| Broadcast guarantees both clients receive updates synchronously | §5.2 |
| Scores stored in server memory and pushed to both clients on round completion | §6 |
