# Networked Pente (CS 457 Term Project)

My game is called Pente and the goal of the game is to either get 5 of your (marbles) in a row on a 19x19 grid, or capture 5 pairs. 
To capture a pair of your opponents marbles, you have to have one of your pieces next to a pair of your opponents marbles. Placing another one of your marbles on the other side of their pair all in the same line
will result in the capture of their two pieces.

EX Player 1 = X, Player 2 = O

XOO   -Player 2 is susseptiable to being captured

XOOX  -Player 1 plays to capture the pair

X--X  -Player 1 captures the pair and those two spaces on the board free up

This can also be done in a diagonal fasion as the game is played on a grid.

---

## Running the game

Requires Python 3.10 or newer (developed on 3.11). Standard library only, nothing to install.

**Server** (listens on `0.0.0.0`, all interfaces):

```bash
python3 server.py -h            # help
python3 server.py -p 5457       # start the server on port 5457
```

**Clients** (run two, one per player, in separate terminals):

```bash
python3 client.py -h
python3 client.py -i server.kaanta.edu -p 5457 -a Alice   # in the CML lab (DNS from Router R2)
python3 client.py -i localhost -p 5457 -a Bob             # local testing on one machine
```

`-i` is a DNS hostname. The client resolves it with `socket.gethostbyname()` and never uses a hard-coded IP. If the name cannot be resolved, the client prints a clear error and exits with status 1. `-a` (player name) is optional; the client asks for it if it is left out.

`-s dots` (optional) draws the stones as `●` (P1) and `○` (P2) instead of `X` and `O`. X/O is the default because it is plain ASCII and shows correctly on any console.

**Playing:** the first player to connect is P1 (`X`) and moves first. When the screen shows `>>> YOUR TURN`, type a move chess-style: a column letter `A`-`S`, then a row number `1`-`19`, e.g. `J10` (the center). Columns run left to right and row 1 is at the bottom, so a move reads like an (x, y) point. Letters are printed above and below the board and numbers on both sides. Other commands: `board` (redraw), `help`, `quit` (forfeit and leave). The last move is shown in brackets, like `[X]`.

**Rejoining:** if a client crashes, loses its connection, or is closed with Ctrl-C, the server holds that player's seat for 60 seconds. A client whose connection drops retries on its own every 5 s. A client that was closed can be started again with the same `-a` name within 60 s and it rejoins automatically (it saves a `.pente_session_<name>.json` file for this). Typing `quit` is a forfeit.

## Project layout

| Path | What it is |
|---|---|
| `server.py` | Server CLI, accept loop, and one handler thread per connection |
| `client.py` | Terminal client: CLI, DNS lookup, board display, input, auto-rejoin |
| `pente/framing.py` | Newline-delimited JSON framing (`FrameReader`, `encode_frame`) |
| `pente/messages.py` | The 11 message types, 15 error codes, validation, and message builders |
| `pente/game.py` | Pente rules (captures, five in a row) and Points Pente scoring |
| `pente/fsm.py` | `PenteServer`: the game state machine, guarded by one lock |
| `tests/` | Unit and end-to-end tests |
| `tools/demo_game.py` | Plays a scripted game with real server and client processes and saves the console logs |
| `docs/` | Protocol, FSM, concurrency design, sequence diagram, AI prompt log, Sprint 3 execution log |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The suite covers framing (including TCP fragmentation and coalescing), message validation, every Pente rule, the FSM transition rows (lobby, gameplay, disconnect, reconnect, grace timeout, and concurrent moves from two threads), and full games over real sockets on localhost. `pytest` also runs these tests if it is installed.

## Documentation

- [`sow_template.md`](sow_template.md): Statement of Work
- [`docs/protocol_blueprint.md`](docs/protocol_blueprint.md): wire protocol (PAP v1)
- [`docs/fsm_specification.md`](docs/fsm_specification.md): server game state machine
- [`docs/concurrency_design.md`](docs/concurrency_design.md): threading and locking
- [`docs/sequence_diagram.md`](docs/sequence_diagram.md): message flow diagram
- [`docs/ai_prompts.md`](docs/ai_prompts.md): AI usage strategy and prompt log
- [`docs/sprint3_execution_log.md`](docs/sprint3_execution_log.md): Sprint 3 gameplay log (two clients, full game)
