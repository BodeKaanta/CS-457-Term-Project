"""End-to-end tests over real TCP sockets on localhost, plus client CLI checks."""

import json
import logging
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import client
import server
from pente import messages as m
from pente.framing import FrameReader, encode_frame
from pente.fsm import PenteServer

logging.disable(logging.CRITICAL)
ROOT = Path(__file__).resolve().parent.parent


class RawClient:
    """A bare protocol client used to drive the real server."""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.reader = FrameReader()
        self.pending: list[dict] = []

    def send(self, msg: dict) -> None:
        self.sock.sendall(encode_frame(msg))

    def recv_msg(self) -> dict:
        while not self.pending:
            data = self.sock.recv(4096)
            if not data:
                raise EOFError("server closed the connection")
            self.pending.extend(json.loads(f) for f in self.reader.feed(data))
        return self.pending.pop(0)

    def expect(self, *msg_types: str) -> list[dict]:
        msgs = [self.recv_msg() for _ in msg_types]
        assert [x["msg_type"] for x in msgs] == list(msg_types), [x["msg_type"] for x in msgs]
        return msgs

    def at_eof(self) -> bool:
        try:
            while True:
                if not self.sock.recv(4096):
                    return True
        except (ConnectionError, OSError):
            return True

    def close(self) -> None:
        self.sock.close()


class LiveServerTests(unittest.TestCase):
    def setUp(self):
        self.listen = server.create_listen_socket(0)  # ephemeral port
        self.port = self.listen.getsockname()[1]
        self.pente = PenteServer()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=server.serve_forever,
                                       args=(self.pente, self.listen, self.stop), daemon=True)
        self.thread.start()
        self.clients: list[RawClient] = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.stop.set()
        self.thread.join(timeout=3)
        self.listen.close()
        self.pente.shutdown_all()

    def join(self, alias: str) -> RawClient:
        c = RawClient(self.port)
        self.clients.append(c)
        c.send(m.make_connect(alias))
        return c

    def start_game(self):
        alice = self.join("Alice")
        alice.expect("LOBBY_WAIT", "SCORE_UPDATE")
        bob = self.join("Bob")
        alice.expect("GAME_START", "SCORE_UPDATE", "STATE_UPDATE")
        bob.expect("GAME_START", "SCORE_UPDATE", "STATE_UPDATE")
        return alice, bob

    def test_two_clients_play_three_turns(self):
        alice, bob = self.start_game()
        for who, pid, row, col in ((alice, "P1", 9, 9), (bob, "P2", 9, 10), (alice, "P1", 6, 6)):
            who.send(m.make_move(pid, row, col))
            a = alice.expect("STATE_UPDATE")[0]["payload"]
            b = bob.expect("STATE_UPDATE")[0]["payload"]
            self.assertEqual(a, b)
            self.assertEqual(a["last_move"]["player"], pid)
        self.assertEqual(a["turn_number"], 3)
        self.assertEqual(a["active_player"], "P2")

    def test_fragmented_and_coalesced_frames(self):
        alice, bob = self.start_game()
        data = encode_frame(m.make_move("P1", 9, 9))
        for piece in (data[:7], data[7:30], data[30:]):  # Blueprint §1.2 example C
            alice.sock.sendall(piece)
            time.sleep(0.05)
        alice.expect("STATE_UPDATE")
        bob.expect("STATE_UPDATE")
        # Example B: two frames in one send. The second is out of turn.
        alice.sock.sendall(encode_frame(m.make_move("P1", 1, 1)) + encode_frame(m.make_move("P1", 2, 2)))
        err = alice.expect("ERROR")[0]["payload"]
        self.assertEqual(err["code"], "NOT_YOUR_TURN")

    def test_third_client_rejected_without_disturbing_game(self):
        alice, bob = self.start_game()
        carol = self.join("Carol")
        err = carol.expect("ERROR")[0]["payload"]
        self.assertEqual((err["code"], err["fatal"]), ("ROOM_FULL", True))
        self.assertTrue(carol.at_eof())
        alice.send(m.make_move("P1", 9, 9))
        bob.expect("STATE_UPDATE")

    def test_oversized_frame_is_fatal(self):
        c = RawClient(self.port)
        self.clients.append(c)
        c.sock.sendall(b"x" * 9000)
        err = c.expect("ERROR")[0]["payload"]
        self.assertEqual(err["code"], "FRAME_TOO_LARGE")
        self.assertTrue(c.at_eof())

    def test_game_to_completion_then_lobby_reopens(self):
        alice, bob = self.start_game()
        # Alice builds J10-N10. Her first stone is the center and her second
        # (M10) is on the box, as the tournament rule requires.
        for i, col in enumerate((9, 12, 10, 11)):
            alice.send(m.make_move("P1", 9, col))
            alice.expect("STATE_UPDATE"), bob.expect("STATE_UPDATE")
            bob.send(m.make_move("P2", 0, i * 2))
            alice.expect("STATE_UPDATE"), bob.expect("STATE_UPDATE")
        alice.send(m.make_move("P1", 9, 13))
        for c in (alice, bob):
            msgs = c.expect("STATE_UPDATE", "GAME_OVER", "SCORE_UPDATE")
            self.assertEqual(msgs[1]["payload"]["winner"], "P1")
            self.assertTrue(c.at_eof())  # CLEANUP closes both connections
        newcomer = self.join("Alice")
        standings = newcomer.expect("LOBBY_WAIT", "SCORE_UPDATE")[1]["payload"]
        self.assertEqual(standings["leaderboard"][0]["alias"], "Alice")

    def test_abrupt_drop_then_rejoin(self):
        alice, bob = self.start_game()
        token = None
        # Bob's GAME_START was consumed by start_game; fetch the token from the server.
        with self.pente.state_lock:
            token = self.pente.game.seats["P2"].session_token
        bob.sock.close()  # EOF without DISCONNECT
        self.clients.remove(bob)
        status = alice.expect("PLAYER_STATUS", "STATE_UPDATE")
        self.assertEqual(status[0]["payload"]["status"], "DISCONNECTED")
        bob2 = RawClient(self.port)
        self.clients.append(bob2)
        bob2.send(m.make_reconnect("P2", "Bob", token))
        start = bob2.expect("GAME_START", "SCORE_UPDATE", "STATE_UPDATE")[0]["payload"]
        self.assertTrue(start["resumed"])
        alice.expect("PLAYER_STATUS", "STATE_UPDATE")


class ClientCliTests(unittest.TestCase):
    def run_client(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "client.py", *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)

    def test_dns_failure_is_graceful(self):
        # .invalid is reserved (RFC 2606), so it never resolves.
        result = self.run_client("-i", "server.nonexistent.invalid", "-p", "5457", "-a", "Alice")
        self.assertEqual(result.returncode, 1)
        self.assertIn("[ERROR] Unable to resolve DNS hostname", result.stdout)
        self.assertIn("ip dns server", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_required_arguments(self):
        result = self.run_client("-p", "5457")
        self.assertEqual(result.returncode, 2)
        self.assertIn("-i", result.stderr)
        self.assertEqual(self.run_client("-h").returncode, 0)

    def test_connection_refused_is_graceful(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]  # nothing listens here once closed
        result = self.run_client("-i", "localhost", "-p", str(free_port), "-a", "Alice")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not connect", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_server_rejects_bad_port(self):
        result = subprocess.run([sys.executable, "server.py", "-p", "70000"], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("65535", result.stderr)


class ClientHelperTests(unittest.TestCase):
    def test_parse_move(self):
        # Column letter (x) then row number (y), row 1 at the bottom.
        self.assertEqual(client.parse_move("J10"), (9, 9))      # center
        self.assertEqual(client.parse_move(" j 10 "), (9, 9))
        self.assertEqual(client.parse_move("A1"), (18, 0))      # bottom-left
        self.assertEqual(client.parse_move("A19"), (0, 0))      # top-left
        self.assertEqual(client.parse_move("S1"), (18, 18))     # bottom-right
        # Off the board still parses; the server rejects it with OUT_OF_BOUNDS.
        self.assertEqual(client.parse_move("T5"), (14, 19))
        self.assertEqual(client.parse_move("A20"), (-1, 0))
        for bad in ("", "K", "10", "9 9", "KK10", "K1O", "K100", "10K"):
            self.assertIsNone(client.parse_move(bad), bad)

    @staticmethod
    def point(text, notation):
        """The character drawn at a point, e.g. point(text, "J10")."""
        r, c = client.game.from_notation(notation)
        return text.splitlines()[1 + r][4 + 3 * c + 1]

    def test_render_board_marks_last_move(self):
        board = ["." * 19 for _ in range(19)]
        board[9] = "." * 9 + "1" + "2" + "." * 8      # X at J10, O at K10
        text = client.render_board(board, {"player": "P2", "row": 9, "col": 10, "captured": []})
        lines = text.splitlines()
        self.assertEqual(len(lines), 21)  # letters + 19 rows + letters
        self.assertEqual(lines[0].split(), list("ABCDEFGHIJKLMNOPQRS"))
        self.assertTrue(lines[1].startswith(" 19 ") and lines[19].startswith("  1 "))
        self.assertTrue(lines[10].startswith(" 10 "))
        self.assertIn("─X─[O]─", lines[10])   # stones sit on the line
        self.assertEqual(lines[10].index("X"), lines[0].index("J"))  # X sits under J
        self.assertEqual(self.point(text, "J10"), "X")
        # Every row is the same width, so the vertical lines stay straight.
        self.assertTrue(all(line[4 + 3 * 19] == " " for line in lines[1:20]))

    def test_lines_board_glyphs(self):
        text = client.render_board(["." * 19 for _ in range(19)], None)
        expected = {
            "A19": "┌", "S19": "┐", "A1": "└", "S1": "┘",  # corners
            "A10": "├", "S10": "┤", "J19": "┬", "J1": "┴",  # edges
            "C3": "┼",                                               # plain point
            "J10": "╋",                                              # center
            "G13": "╆", "M13": "╅", "G7": "╄", "M7": "╃",  # box corners
            "J13": "┿", "J7": "┿", "G10": "╂", "M10": "╂",  # box sides
        }
        for notation, glyph in expected.items():
            self.assertEqual(self.point(text, notation), glyph, notation)
        row13 = text.splitlines()[1 + 6]
        self.assertIn("━━", row13)          # heavy line along the top of the box
        self.assertNotIn("━", text.splitlines()[1 + 5])

    def test_ascii_board(self):
        board = ["." * 19 for _ in range(19)]
        board[9] = "." * 9 + "1" + "2" + "." * 8
        try:
            client.set_display("xo", "ascii")
            text = client.render_board(board, None)
        finally:
            client.set_display()
        self.assertTrue(all(ord(ch) < 128 for ch in text))  # for consoles without box glyphs
        self.assertEqual(self.point(text, "J10"), "X")
        self.assertEqual(self.point(text, "C3"), "+")
        self.assertEqual(self.point(text, "G13"), "#")
        self.assertIn("#==#", text.splitlines()[1 + 6])
        empty = client.render_board(["." * 19 for _ in range(19)], None)
        self.assertEqual(self.point(empty, "J10"), "╋")   # back to lines after reset

    def test_dots_stone_style(self):
        board = ["." * 19 for _ in range(19)]
        board[9] = "." * 9 + "12" + "." * 8
        try:
            client.set_display("dots")
            text = client.render_board(board, None)
            self.assertIn("●──○", text.splitlines()[10])
            self.assertEqual(client.PLAYER_SYMBOL["P1"], "●")
        finally:
            client.set_display()
        self.assertEqual(client.PLAYER_SYMBOL, {"P1": "X", "P2": "O"})

if __name__ == "__main__":
    unittest.main()
