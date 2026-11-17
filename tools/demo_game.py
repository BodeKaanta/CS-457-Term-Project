#!/usr/bin/env python3
"""Record a scripted demo game for the Sprint 3 execution log.

Starts the real server.py and two real client.py processes, types each
player's moves into the client when it prints its turn prompt, saves each
console's output under docs/execution_log/, and builds
docs/sprint3_execution_log.md from them. Nothing is simulated: the
clients resolve the hostname through DNS and talk to the server over TCP
exactly as they do when a person is typing.

Usage (from the repository root):
    python tools/demo_game.py [-i localhost] [-p 5457]

The script also records the client's DNS-failure message for a name that
does not resolve on this machine (server.kaanta.edu until Sprint 4's DNS).
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The board uses box-drawing characters, so the children write UTF-8 and the
# script reads UTF-8, whatever the Windows code page is.
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
OUT_DIR = ROOT / "docs" / "execution_log"
PROMPT = ">>> YOUR TURN"

# Each entry is typed when that client shows ">>> YOUR TURN". Rejected or
# unparseable entries re-print the prompt, so the next entry follows them.
# The game: Alice opens on the center (J10). Her second stone H12 is inside
# the tournament-rule box and is rejected, so she plays G13 on the box. Bob's
# pair at K10-L10 is captured by Alice's M10, then Alice completes the
# diagonal F14-J10 for five in a row. Bob's J10 (occupied) and T5 (column T is
# off the board) are rejected by the server.
SCRIPT = {
    "Alice": ["J10", "H12", "G13", "M10", "hello", "H12", "I11", "F14"],
    "Bob": ["K10", "L10", "J10", "A19", "T5", "B19", "C19"],
}


class Console:
    """One child process whose output is captured line by line."""

    def __init__(self, name: str, args: list[str], timeline: list, lock: threading.Lock,
                 moves: list[str] | None = None) -> None:
        self.name = name
        self.lines: list[str] = []
        self.moves = list(moves or [])
        self.timeline = timeline
        self.lock = lock
        self.proc = subprocess.Popen(
            [sys.executable, "-u", *args], cwd=ROOT, text=True, encoding="utf-8", env=CHILD_ENV,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _record(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            self.timeline.append(f"{time.strftime('%H:%M:%S')} {self.name:<6}| {line}")

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            self._record(line)
            if line.startswith(PROMPT) and self.moves:
                time.sleep(0.4)  # a human-ish pause; keeps the logs readable
                move = self.moves.pop(0)
                self._record(f"> {move}")  # piped input is not echoed; show what was typed
                self.proc.stdin.write(move + "\n")
                self.proc.stdin.flush()

    def wait_for(self, text: str, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if any(text in line for line in self.lines):
                    return
            time.sleep(0.05)
        raise TimeoutError(f"{self.name} never printed {text!r}")


def run_command(args: list[str]) -> str:
    """Run a short command and return its console output with the command line."""
    result = subprocess.run([sys.executable, *args], cwd=ROOT, text=True, encoding="utf-8",
                            env=CHILD_ENV, capture_output=True, timeout=60)
    shown = "python3 " + " ".join(args)
    return f"$ {shown}\n{result.stdout}{result.stderr}(exit status {result.returncode})\n"


def excerpt(lines: list[str], start: str, stop: str, occurrence: int = 1) -> list[str]:
    """Lines from the first line containing `start` through the Nth containing `stop`."""
    begin = next(i for i, line in enumerate(lines) if start in line)
    seen = 0
    for i in range(begin, len(lines)):
        if stop in lines[i]:
            seen += 1
            if seen == occurrence:
                return lines[begin:i + 1]
    return lines[begin:]


def block(lines: list[str]) -> str:
    return "```text\n" + "\n".join(lines) + "\n```"


def write_markdown(cli: str, server: list[str], alice: list[str], bob: list[str],
                   port: str, host: str) -> None:
    """Assemble docs/sprint3_execution_log.md from the captured consoles."""
    alice_start = excerpt(alice, "[INFO] Resolved", "Waiting for Bob (P2) to move...", 2)
    alice_capture = excerpt(alice, "> M10", "Waiting for Bob (P2) to move...")
    alice_end = alice[next(i for i, line in enumerate(alice) if line == "> F14"):]
    bob_errors = excerpt(bob, "> J10", PROMPT)
    bob_oob = excerpt(bob, "> T5", PROMPT)
    text = f"""# Sprint 3 Execution Log: Two Clients, One Full Game

**Project:** CS 457 Term Project: Networked Pente
**Author:** Bode Kaanta
**Generated by:** `python tools/demo_game.py -i {host} -p {port}` on {time.strftime('%Y-%m-%d')}

This log was recorded with the real `server.py` and two real `client.py` processes on one machine. `tools/demo_game.py` starts them and types each player's move when that client prints `>>> YOUR TURN`. Lines starting with `> ` are the typed input (piped input is not echoed, so the script adds them). The clients reach the server through DNS name resolution (`-i {host}`) and TCP, the same way they do when a person types. The raw console files are in [`execution_log/`](execution_log/).

## What this log shows

| Sprint 3 acceptance item | Where to look |
|---|---|
| Client parses `-h`, `-i`, `-p`; server parses `-h`, `-p` and binds `0.0.0.0` | Section 1 (help output) and the first server log line (`listening on 0.0.0.0:{port}`) |
| Client resolves the server by DNS hostname, no hard-coded IP | `[INFO] Resolved {host} -> ...` at the top of each client console |
| Client catches `socket.gaierror` and fails gracefully | Section 1: `server.kaanta.edu` does not resolve on this machine yet (Router R2's DNS comes in Sprint 4). The client prints the error and exits with status 1, with no traceback |
| Server accepts 2 simultaneous clients and runs FSM transitions cleanly | Section 2: `conn-1` and `conn-2` each get a handler thread. Every transition is logged with its row ID from `fsm_specification.md` (L1, L3, G1, G2, G5-G8, G11, G12) |
| At least 3 turns of gameplay | 11 moves were played: P1 opened on the center and her second stone was rejected inside the tournament-rule box (she kept the turn), a capture at move 5, an occupied point and an off-board move rejected (P2 kept the turn), then P1 won with five in a row on a diagonal |
| CLI board, turn status, and score summaries | Section 3: the board with column letters A-S and row numbers 1-19, `>>> YOUR TURN` / `Waiting for Bob (P2) to move...`, live captures on every turn, then round points and the session leaderboard |

## 1. Command-line help and DNS failure

{block(cli.rstrip().splitlines())}

## 2. Server console

Thread names are in brackets: `MainThread` accepts connections; `conn-1` (Alice) and `conn-2` (Bob) are the per-client handler threads. The thread that receives a move applies it and sends the update to both players.

{block(server)}

## 3. Client consoles

### Alice (P1, `X`): connecting, waiting, and the first three turns

Alice's first stone must go on the center point (the prompt reminds her). Her second stone, `H12`, is only 2 points from the center, inside the box, so the server rejects it with `TOURNAMENT_RULE` and she keeps the turn. `G13`, a corner of the box, is accepted.

{block(alice_start)}

### Alice: a capture (move 5)

Bob's stones at `K10` and `L10` sit between Alice's `J10` and her new stone at `M10`, so the server removes them and Alice's capture count goes to 1.

{block(alice_capture)}

### Bob (P2, `O`): rejected moves keep the turn

The server rejects the move with a non-fatal `ERROR` sent only to Bob. Nothing changes and Bob is prompted again.

{block(bob_errors)}

{block(bob_oob)}

### Alice: the winning move, result, and scores

{block(alice_end)}

<details>
<summary>Full console: Alice</summary>

{block(alice)}

</details>

<details>
<summary>Full console: Bob</summary>

{block(bob)}

</details>

## How to reproduce by hand

Three terminals in the repository folder:

```bash
python3 server.py -p {port}
python3 client.py -i {host} -p {port} -a Alice
python3 client.py -i {host} -p {port} -a Bob
```
"""
    (ROOT / "docs" / "sprint3_execution_log.md").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-i", "--host", default="localhost")
    parser.add_argument("-p", "--port", default="5457")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. CLI help and graceful DNS failure.
    cli = [run_command(["server.py", "-h"]), run_command(["client.py", "-h"]),
           run_command(["client.py", "-i", "server.kaanta.edu", "-p", args.port, "-a", "Alice"])]
    (OUT_DIR / "cli_and_dns_failure.txt").write_text("\n".join(cli), encoding="utf-8")

    # 2. A full game between two clients.
    timeline: list[str] = []
    lock = threading.Lock()
    server = Console("server", ["server.py", "-p", args.port], timeline, lock)
    server.wait_for("listening")
    alice = Console("Alice", ["client.py", "-i", args.host, "-p", args.port, "-a", "Alice"],
                    timeline, lock, SCRIPT["Alice"])
    alice.wait_for("[LOBBY]")
    bob = Console("Bob", ["client.py", "-i", args.host, "-p", args.port, "-a", "Bob"],
                  timeline, lock, SCRIPT["Bob"])

    status = 0
    for c in (alice, bob):
        try:
            c.proc.wait(timeout=60)
            c.thread.join(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"{c.name} did not finish; killing it", file=sys.stderr)
            c.proc.kill()
            status = 1
    time.sleep(0.5)
    server.proc.terminate()
    server.proc.wait(timeout=10)
    server.thread.join(timeout=5)

    for c in (server, alice, bob):
        (OUT_DIR / f"{c.name.lower()}.txt").write_text("\n".join(c.lines) + "\n", encoding="utf-8")
    (OUT_DIR / "timeline.txt").write_text("\n".join(timeline) + "\n", encoding="utf-8")
    if status == 0:
        write_markdown("\n".join(cli), server.lines, alice.lines, bob.lines, args.port, args.host)
    print(f"Logs written to {OUT_DIR.relative_to(ROOT)}; client exit codes: "
          f"Alice {alice.proc.returncode}, Bob {bob.proc.returncode}")
    return status or alice.proc.returncode or bob.proc.returncode


if __name__ == "__main__":
    sys.exit(main())
