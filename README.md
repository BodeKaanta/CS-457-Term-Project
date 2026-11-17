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
python3 client.py -i server.kaanta.edu -p 5457 -a Alice   # in the CML lab (DNS from Router R1)
python3 client.py -i localhost -p 5457 -a Bob             # local testing on one machine
```

`-i` is a DNS hostname. The client resolves it with `socket.gethostbyname()` and never uses a hard-coded IP. If the name cannot be resolved, the client prints a clear error and exits with status 1. `-a` (player name) is optional; the client asks for it if it is left out.

The board is drawn with grid lines like a real Pente board, with a heavy cross on the center point (`J10`) and a heavier box 3 points out from the center (`G7` to `M13`), the box used by the tournament rule for P1's second stone. Two display options:

- `-s dots` draws the stones as `●` (P1) and `○` (P2) instead of `X` and `O`.
- `-b ascii` draws the same board with plain ASCII (`+`, `-`, `#` for the box, `*` for the center), for a console that shows boxes or `?` instead of lines.

**Playing:** the first player to connect is P1 (`X`) and moves first. When the screen shows `>>> YOUR TURN`, type a move chess-style: a column letter `A`-`S`, then a row number `1`-`19`, e.g. `J10` (the center). Columns run left to right and row 1 is at the bottom, so a move reads like an (x, y) point. Letters are printed above and below the board and numbers on both sides. **Tournament rule:** P1's first stone must go on the center point, `J10`, and P1's second stone must go on or outside the box (`G7` to `M13`). The server rejects anything else with `TOURNAMENT_RULE` and the player tries again; the prompt reminds P1 on those two turns. Other commands: `board` (redraw), `help`, `quit` (forfeit and leave). The last move is shown in brackets, like `[X]`.

**Rejoining:** if a client crashes, loses its connection, or is closed with Ctrl-C, the server holds that player's seat for 60 seconds. A client whose connection drops retries on its own every 5 s. A client that was closed can be started again with the same `-a` name within 60 s and it rejoins automatically (it saves a `.pente_session_<name>.json` file for this). Typing `quit` is a forfeit.

## Project layout

| Path | What it is |
|---|---|
| `server.py` | Server CLI, accept loop, and one handler thread per connection |
| `client.py` | Terminal client: CLI, DNS lookup, board display, input, auto-rejoin |
| `pente/framing.py` | Newline-delimited JSON framing (`FrameReader`, `encode_frame`) |
| `pente/messages.py` | The 11 message types, 16 error codes, validation, and message builders |
| `pente/game.py` | Pente rules (captures, five in a row) and Points Pente scoring |
| `pente/fsm.py` | `PenteServer`: the game state machine, guarded by one lock |
| `tests/` | Unit and end-to-end tests |
| `cml/` | Sprint 4 CML lab: router config, importable lab, lab guide |
| `docs/` | Protocol, FSM, concurrency design, sequence diagram, AI prompt log |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The suite covers framing (including TCP fragmentation and coalescing), message validation, every Pente rule, the FSM transition rows (lobby, gameplay, disconnect, reconnect, grace timeout, and concurrent moves from two threads), and full games over real sockets on localhost. `pytest` also runs these tests if it is installed.

## Sprint 4: CML network

The game runs on a Cisco Modeling Labs topology: one IOSv router, R1, connects the two client subnets and the server subnet. R1 leases client addresses by DHCP and answers DNS for `server.kaanta.edu`.

- [`cml/LAB_GUIDE.md`](cml/LAB_GUIDE.md): step-by-step build, verification, capture, and export
- [`cml/pente_lab_import.yaml`](cml/pente_lab_import.yaml): ready-to-import CML lab (regenerate with `python tools/build_cml_lab.py` after editing [`cml/R1.cfg`](cml/R1.cfg))
- [`sprint_4_network_report.md`](sprint_4_network_report.md): verification report
- `python tools/dns_pcap_report.py dns_raw.pcap --annotate sprint_4_dns_lookup.pcap`: checks the raw CML capture, prints an annotated summary for the report, and writes the deliverable capture with a Wireshark comment on every DNS packet

## Documentation

- [`sow_template.md`](sow_template.md): Statement of Work
- [`docs/protocol_blueprint.md`](docs/protocol_blueprint.md): wire protocol (PAP v1)
- [`docs/fsm_specification.md`](docs/fsm_specification.md): server game state machine
- [`docs/concurrency_design.md`](docs/concurrency_design.md): threading and locking
- [`docs/sequence_diagram.md`](docs/sequence_diagram.md): message flow diagram
- [`docs/ai_prompts.md`](docs/ai_prompts.md): AI usage strategy and prompt log
