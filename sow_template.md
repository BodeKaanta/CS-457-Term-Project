# CS 457 Project Statement of Work (SOW) & Protocol Specification Template

**Student Name:** Bode Kaanta  
**Date:** 2026/09/21 (updated 2026/10/05 for Sprint 1, 2026/10/19 for Sprint 2, 2026/11/15 for Sprint 3, 2026/11/16 for Sprint 4)  
**Course:** CS 457 - Computer Networks  
**Target Server Domain:** `server.kaanta.edu`  

---

## 1. Game Selection & Scope (Sprint 0)

> Planning is going to be an iterative process through the sprints so you don't have to have all the details now. Focus on big overview concepts. You will be updating the SOW as we plan.
> You have a lot of freedom to choose a game. There are a couple caveats.  

> - It must run in the console. The lab nodes won't be able to handle extensive graphics.
> - It has to be self-contained. You can use a internet-connector to download you code, but because the architecture must run 5 nodes you won't be able to run 
> - You are encouraged to use python, but I'm not going to make it a strict requirement. The instructor and TA's ability to help with C or Rust, etc will be diminished in other languages.

### 1.1 Game Overview
- **Chosen Game:** Pente
- **Player Capacity:** 2 Players (Simulated via 2 CML Client nodes)
- **Game Summary:** The goal is to either get 5 in a row or capture 5 pairs. A player can only place down one marker per tern. It is played on somthing very similar to a go board.

### 1.2 Core Game Rules & Win/Draw Conditions
- **Turn Mechanics:** The server keeps a counter `turn_number` (starts at 0, +1 after each legal move). It is P1's turn when `turn_number % 2 == 0` and P2's turn otherwise. P1 (first to connect) always moves first.
- **Victory Condition:** The player who wins is the person who either gets 5 of their peices in a row, or is able to catpure 5 pairs. 
- **Draw/Tie Condition:** There are no draws in Pente; one player always wins. As a safety rule for the theoretical case of a completely full board with no winner, the player with more captured pairs wins, and if those are tied, P2 wins.

---

## 2. Application-Layer Messaging Protocol Blueprint (Sprint 1 Deliverable)

> Full specifications: [`docs/protocol_blueprint.md`](docs/protocol_blueprint.md), [`docs/fsm_specification.md`](docs/fsm_specification.md), [`docs/ai_prompts.md`](docs/ai_prompts.md)

### 2.1 Message Transport & Serialization Format
- **Transport Protocol:** TCP (port `5457`)
- **Serialization Format:** JSON (UTF-8, compact)
- **Framing Mechanism:** Newline-delimited (`\n`) JSON payloads, one JSON object per line, max 8192 bytes. The receiver buffers bytes and splits on `\n` to handle TCP coalescing and fragmentation.

### 2.2 Message Schema Definitions

#### Message Types:
1. `CONNECT` (Client -> Server): Request to join the game room with an alias.
2. `LOBBY_WAIT` (Server -> Client): Notification that the server is waiting for Player 2.
3. `GAME_START` (Server -> Clients): Game initiated (or resumed); assigns roles P1/P2 and a reconnect session token.
4. `MOVE` (Client -> Server): Place a stone. The player types the move as `row col` (both 0-18, matching the numbers printed around the board, e.g. `9 9` for the center); the client sends them as integer `row`, `col`.
5. `STATE_UPDATE` (Server -> Clients): Broadcast board, captured pairs, turn number, active player, and game status.
6. `ERROR` (Server -> Client): Invalid/out-of-turn move or malformed packet (15 defined error codes).
7. `DISCONNECT` (Client -> Server): Intentional quit, which forfeits immediately.
8. `GAME_OVER` (Server -> Clients): WIN / FORFEIT notification (winner, win reason) with final board and captures.
9. `RECONNECT` (Client -> Server): Rejoin an in-progress game within the 60-second grace period.
10. `PLAYER_STATUS` (Server -> Client): Opponent disconnected / reconnected notice.
11. `SCORE_UPDATE` (Server -> Client(s)): Round points and the session leaderboard (added in Sprint 2).

#### Example JSON Protocol Schema:
```json
{
  "msg_type": "MOVE",
  "player_id": "P1",
  "payload": {
    "row": 9,
    "col": 9
  },
  "timestamp": 1791266420
}
```

---

### 2.3 Game State Machine (FSM) Design (Sprint 1 Deliverable)
- **State Transitions:** `INIT` -> `WAITING_FOR_PLAYERS` -> `GAME_START` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN` -> (`PLAYER_TURN` | `GAME_OVER`) -> `CLEANUP` -> `WAITING_FOR_PLAYERS`. An unexpected drop moves `PLAYER_TURN` -> `PAUSED_RECONNECT` (60 s grace) -> `PLAYER_TURN` on rejoin or `GAME_OVER` (forfeit) on timeout. See the Mermaid diagram in `docs/fsm_specification.md`.

---

## 3. Game Behavior & Server Concurrency Architecture (Sprint 2 Deliverable)

### 3.1 Server Concurrency Strategy
> Full specifications: [`docs/concurrency_design.md`](docs/concurrency_design.md), [`docs/sequence_diagram.md`](docs/sequence_diagram.md)

- **Architecture Choice:** Multi-threading (`threading.Thread`). The main thread runs the `accept()` loop and starts one daemon handler thread per client socket (cap of 8 connections). A `threading.Timer` thread runs the 60 s reconnect grace period. A blocked `recv()` only parks its own thread, so no client can stall the server.
- **Synchronization Logic:** One `threading.Lock` (`state_lock`) guards all shared state: the board, turn counter, captures, seats, connection list, `game_id`, and the session leaderboard. Handlers never hold the lock during `recv()`. Each complete message is processed in one critical section (validate → mutate → send), so moves are atomic. Every send to a client happens under the lock, so two threads never write to the same socket at once. A 5 s socket timeout bounds how long a send can hold the lock. Only a socket's own handler thread closes it, and the grace timer re-checks the state before acting (stale-timer guard).

### 3.2 State & Score Synchronization Across Clients
- **Turn Enforcement:** The server identifies the player by the socket a `MOVE` arrived on (not the self-reported `player_id`) and, under the lock, checks it against `active_player = ["P1","P2"][turn_number % 2]` before any change. Out-of-turn or illegal moves get an `ERROR` to the sender only and change nothing. After a legal move, `turn_number` increments and the new `active_player` is broadcast in `STATE_UPDATE`.
- **Score & Board Synchronization:** The thread that applied a change encodes `STATE_UPDATE` once and sends the same bytes to both players before releasing the lock, so both screens always show the same sequence of states. Live captures ride in every `STATE_UPDATE`. When a game ends, the server scores the round with Points Pente rules (5-point bonus for five in a row, 1 per captured pair, 1 per four-in-a-row still on the final board) and adds the points to a session leaderboard keyed by alias, which survives `CLEANUP`. It then sends `STATE_UPDATE (FINISHED)` → `GAME_OVER` → `SCORE_UPDATE (ROUND_COMPLETE)` to both players in one critical section. Standings are also sent with `SCORE_UPDATE (STANDINGS)` when a player joins.

---

## 4. Coding & AI Implementation Plan (Sprint 3)

> Code: [`server.py`](server.py), [`client.py`](client.py), and the shared [`pente/`](pente/) package. How to run it: [`README.md`](README.md). Gameplay log: [`docs/sprint3_execution_log.md`](docs/sprint3_execution_log.md).

- **Language:** Python 3.11, standard library only (`socket`, `threading`, `json`, `argparse`, `secrets`, `logging`, `dataclasses`). Nothing to install on the CML nodes.
- **Permitted AI Tools:** Claude (Claude Code), used to generate the modules and their tests from my specs; GitHub Copilot for inline completion only.
- **AI Prompting & Constraint Strategy:** The full strategy and the prompt log are in [`docs/ai_prompts.md`](docs/ai_prompts.md). In short:
  - The protocol blueprint, FSM spec, and concurrency design were given to the AI as the **normative spec**. Message types, error codes, field names, and state names are closed sets; the AI may not add or rename any.
  - The code was built **one layer at a time**, each with its own tests: framing → messages → game rules → FSM → server/client sockets.
  - Every FSM transition logs its row ID from the spec tables (for example `FSM PLAYER_TURN -> EVALUATE_MOVE [G2 ...]`), so the server log can be checked line by line against `fsm_specification.md`.
  - The verification gate in `ai_prompts.md` §4 was applied before committing: closed-set check, no `recv()` under the lock, every send under the lock, EOF checks, and all tests passing.
- **CLI (Sprint 3 §2.1):** `server.py -h | -p PORT` binds `0.0.0.0`. `client.py -h | -i HOSTNAME -p PORT [-a NAME]`. `-i` and `-p` are required. The client resolves `-i` with `socket.gethostbyname()` (no hard-coded IPs). On `socket.gaierror` it prints a message pointing at the router DNS configuration and exits with status 1, with no traceback (§2.2).
- **Implementation Risk Management:**

  | Risk | How it was handled |
  |---|---|
  | Running out of time with one big untested program | Built bottom-up in small modules, each with unit tests (81 tests). A scripted demo (`tools/demo_game.py`) replays a full game with real processes in seconds, so every change could be checked end to end |
  | Developing on Windows, deploying to Linux CML nodes | Linux-only socket options (`TCP_KEEPIDLE`, `TCP_USER_TIMEOUT`) are applied only when present (`hasattr`). Output is plain ASCII for the lab consoles |
  | Reading keyboard input while server updates keep arriving | `input()` runs in its own thread and feeds a queue, so the board refreshes as soon as the opponent moves. (`select()` cannot watch the keyboard on Windows) |
  | Bugs that only show up with two clients at once | FSM tests send moves from two threads at the same time and check that turns never double up and both players see identical states in the same order |
  | Disconnect and rejoin paths are hard to test by hand | Tested with a killed client relaunched within 60 s, a cut connection through a TCP proxy (client rejoins on its own), and a shortened grace timer in the unit tests |

---

## 5. CML Multi-Subnet Topology & Wireshark Deployment Plan (Sprint 4 & 5 Deliverable)

> **Updated for Sprint 4:** the course moved to a single-router topology. The earlier plan (two routers, R1 and R2, joined by a `10.0.0.0/30` backbone, with DNS on R2) is replaced by one router, R1, that is the gateway, DHCP server, and DNS server for all three subnets. Full details and verification: [`sprint_4_network_report.md`](sprint_4_network_report.md); router configuration: [`cml/R1.cfg`](cml/R1.cfg).

### 5.1 Subnet & Router Design
- **Router:** one Cisco IOSv router, R1 (CML node `bk-R1`). All three subnets are directly connected, so no static routes or routing protocol are needed.
- **Subnet A (Client 1, `bk-Client1`):** `192.168.10.0/24`, gateway R1 `Gi0/1` = `192.168.10.1`
- **Subnet B (Client 2, `bk-Client2`):** `192.168.11.0/24`, gateway R1 `Gi0/2` = `192.168.11.1`
- **Subnet C (Game Server, `bk-Server`):** `192.168.20.0/24`, gateway R1 `Gi0/3` = `192.168.20.1`. The server is static `192.168.20.100/24` with default route via `192.168.20.1`.

### 5.2 DHCP Pools & DNS Configuration Plan
- **R1 DHCP Pool 1 (`CLIENT1_POOL`):** network `192.168.10.0/24` (`.1` excluded), default gateway `192.168.10.1`, DNS `192.168.10.1`.
- **R1 DHCP Pool 2 (`CLIENT2_POOL`):** network `192.168.11.0/24` (`.1` excluded), default gateway `192.168.11.1`, DNS `192.168.11.1`.
- **R1 Authoritative DNS:** `ip domain lookup`, `ip dns server`, and the static host mapping `ip host server.kaanta.edu 192.168.20.100`.

### 5.3 Deployment Strategy & Wireshark Trace Capture
- **CML Deployment Strategy (Sprint 4):** build the lab from [`cml/pente_lab_import.yaml`](cml/pente_lab_import.yaml) (or by hand per [`cml/LAB_GUIDE.md`](cml/LAB_GUIDE.md)). Verify interfaces, routes, and DHCP bindings on R1; leases, ping, and `nslookup` on both clients; then export the lab as `sprint_4_topology.yaml`.
- **Game deployment (Sprint 5):** `server.py` runs on `bk-Server` (`192.168.20.100`) and `client.py` on `bk-Client1` and `bk-Client2`, connecting with `-i server.kaanta.edu -p 5457`.
- **Cisco Infrastructure Configuration:** R1 interfaces, DHCP pools (`CLIENT1_POOL`, `CLIENT2_POOL`), and authoritative DNS (`ip host server.kaanta.edu 192.168.20.100`); see [`cml/R1.cfg`](cml/R1.cfg).
- **Wireshark Trace Capture Plan:** the Sprint 4 requirement is the DNS query/response capture `sprint_4_dns_lookup.pcap`, taken on the R1-Client 1 link during `nslookup server.kaanta.edu`, and annotated in the report with `tools/dns_pcap_report.py`. (The DHCP capture was dropped from the Sprint 4 requirements.)
