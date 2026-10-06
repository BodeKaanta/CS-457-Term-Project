# CS 457 Project Statement of Work (SOW) & Protocol Specification Template

**Student Name:** Bode Kaanta  
**Date:** 2026/09/21 (updated 2026/10/05 for Sprint 1)  
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
4. `MOVE` (Client -> Server): Place a stone. The player types a chess-style label like `K10` (columns A–S, rows 1–19, row 1 at the bottom); the client converts it to integer `row`, `col` (0-18) before sending.
5. `STATE_UPDATE` (Server -> Clients): Broadcast board, captured pairs, turn number, active player, and game status.
6. `ERROR` (Server -> Client): Invalid/out-of-turn move or malformed packet (15 defined error codes).
7. `DISCONNECT` (Client -> Server): Intentional quit, which forfeits immediately.
8. `GAME_OVER` (Server -> Clients): WIN / FORFEIT notification (winner, win reason) with final board and captures.
9. `RECONNECT` (Client -> Server): Rejoin an in-progress game within the 60-second grace period.
10. `PLAYER_STATUS` (Server -> Client): Opponent disconnected / reconnected notice.

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
- **Architecture Choice:** [Multi-Threading (`threading.Thread`) OR Non-blocking I/O multiplexing (`select.select` / `selectors`)]
- **Synchronization Logic:** Explain how shared game state and client list are thread-safe (e.g. `threading.Lock`) to prevent race conditions during turn processing.

### 3.2 State & Score Synchronization Across Clients
- **Turn Enforcement:** Detail how the server validates active player ID before processing moves and broadcasts updated turn notifications to all clients.
- **Score & Board Synchronization:** Describe how state broadcasts keep client screens synchronized in real time.

---

## 4. Coding & AI Implementation Plan (Sprint 3)

- **Permitted AI Tools:** [e.g., GitHub Copilot, ChatGPT, Claude]
- **AI Prompting & Constraint Strategy:** Explain how you will constrain AI models to generate code (in Python or your chosen language) that adheres strictly to the protocol blueprint and FSM designed in Sprints 1 & 2.
- **Implementation Risk Management:** Detail your plan to leverage past programming experience and manage time to ensure code completion on schedule.

---

## 5. CML Multi-Subnet Topology & Wireshark Deployment Plan (Sprint 4 & 5 Deliverable)

> For now you can use the topology below. We may update this when we get to defining subnets.

### 5.1 Subnet & Router Design
- **Subnet A (Client 1):** `192.168.10.0/24` (Interface `Gi0/1` on Router R1)
- **Subnet B (Client 2):** `192.168.11.0/24` (Interface `Gi0/2` on Router R1)
- **Subnet C (Game Server):** `192.168.20.0/24` (Interface `Gi0/1` on Router R2)
- **Router Backbone:** `10.0.0.0/30` (Interface `Gi0/0` on R1 <-> `Gi0/0` on R2)

### 5.2 DHCP Pools & DNS Configuration Plan
- **Router R1 DHCP Pool 1 (`CLIENT1_POOL`):** Leases `192.168.10.10` - `192.168.10.50`, gateway `192.168.10.1`, DNS `10.0.0.2`.
- **Router R1 DHCP Pool 2 (`CLIENT2_POOL`):** Leases `192.168.11.10` - `192.168.11.50`, gateway `192.168.11.1`, DNS `10.0.0.2`.
- **Router R2 Authoritative DNS:** Configured with `ip dns server` and static host mapping `server.[yourlastname].edu` -> `192.168.20.100`.

### 5.3 Deployment Strategy & Wireshark Trace Capture
- **CML Deployment Strategy:** Deploy `server.py` onto Subnet C node (`192.168.20.100`) behind Router R2, and `client.py` onto Subnet A and Subnet B nodes behind Router R1.
- **Cisco Infrastructure Configuration:** Router R1 DHCP pools (`CLIENT1_POOL`, `CLIENT2_POOL`) and Router R2 authoritative DNS (`ip host server.[lastname].edu 192.168.20.100`).
- **Wireshark Trace Capture Plan:** Capture DHCP DORA exchange (`dhcp_negotiation.pcap`) and DNS query/response resolution (`dns_lookup.pcap`).
