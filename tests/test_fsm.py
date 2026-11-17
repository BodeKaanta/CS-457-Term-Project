"""PenteServer FSM tests with fake sockets (FSM spec §3, concurrency design §3-§6)."""

import itertools
import json
import logging
import threading
import time
import unittest

from pente import messages as m
from pente.fsm import PAUSED_RECONNECT, PLAYER_TURN, WAITING_FOR_PLAYERS, Connection, PenteServer
from pente.framing import encode_frame

logging.disable(logging.CRITICAL)  # keep test output clean

_ids = itertools.count(1)


class FakeSocket:
    """Records sendall() bytes. Can be told to fail like a dead peer."""

    def __init__(self) -> None:
        self.sent = bytearray()
        self.is_shut_down = False
        self.fail_sends = False

    def sendall(self, data: bytes) -> None:
        if self.fail_sends:
            raise BrokenPipeError("peer gone")
        self.sent += data

    def shutdown(self, how) -> None:
        self.is_shut_down = True


def frame(msg: dict) -> bytes:
    return encode_frame(msg)[:-1]


def take(conn: Connection) -> list[dict]:
    """Return and clear the messages sent to conn."""
    lines = [json.loads(line) for line in bytes(conn.sock.sent).split(b"\n") if line]
    conn.sock.sent.clear()
    return lines


def types(msgs: list[dict]) -> list[str]:
    return [x["msg_type"] for x in msgs]


class FsmTestCase(unittest.TestCase):
    grace = 60
    # Most tests check other rules and open wherever is convenient, so the
    # opening rule is off here; TournamentRuleTests turns it on.
    tournament_rule = False

    def setUp(self):
        self.server = PenteServer(grace_period=self.grace,
                                  tournament_rule=self.tournament_rule)

    def tearDown(self):
        self.server.shutdown_all()

    def new_conn(self) -> Connection:
        conn = Connection(next(_ids), FakeSocket(), ("127.0.0.1", 40000))
        self.assertTrue(self.server.register_connection(conn))
        return conn

    def connect(self, alias: str) -> Connection:
        conn = self.new_conn()
        self.server.on_message(conn, frame(m.make_connect(alias)))
        return conn

    def start_game(self):
        p1 = self.connect("Alice")
        p2 = self.connect("Bob")
        start1 = take(p1)
        start2 = take(p2)
        self.tokens = {
            "P1": next(x for x in start1 if x["msg_type"] == "GAME_START")["payload"]["session_token"],
            "P2": next(x for x in start2 if x["msg_type"] == "GAME_START")["payload"]["session_token"],
        }
        return p1, p2

    def move(self, conn, pid, row, col):
        self.server.on_message(conn, frame(m.make_move(pid, row, col)))

    def last_error(self, conn) -> dict:
        errors = [x for x in take(conn) if x["msg_type"] == "ERROR"]
        self.assertTrue(errors, "expected an ERROR")
        return errors[-1]["payload"]


class TournamentRuleTests(FsmTestCase):
    """G12: P1's first stone on the center, P1's second on or outside the box."""
    tournament_rule = True

    def assert_rejected(self, conn, pid, row, col, turn):
        self.move(conn, pid, row, col)
        err = self.last_error(conn)
        self.assertEqual((err["code"], err["fatal"]), ("TOURNAMENT_RULE", False))
        self.assertFalse(conn.close_requested)
        self.assertEqual(self.server.game.turn_number, turn)       # same player keeps the turn
        self.assertEqual(self.server.game.board.cells[row][col], ".")
        return err

    def assert_accepted(self, conn, pid, row, col, turn_after):
        self.move(conn, pid, row, col)
        self.assertEqual(self.server.game.turn_number, turn_after)
        take(conn)

    def test_first_stone_must_be_center(self):
        p1, p2 = self.start_game()
        err = self.assert_rejected(p1, "P1", 9, 10, turn=0)
        self.assertIn("J10", err["detail"])
        self.assert_rejected(p1, "P1", 0, 0, turn=0)
        self.assert_accepted(p1, "P1", 9, 9, turn_after=1)

    def test_second_stone_on_or_outside_box(self):
        p1, p2 = self.start_game()
        self.assert_accepted(p1, "P1", 9, 9, turn_after=1)
        self.assert_accepted(p2, "P2", 9, 10, turn_after=2)    # P2 may play anywhere
        for row, col in ((8, 8), (11, 7), (7, 11), (9, 7)):    # 1-2 steps from the center
            self.assert_rejected(p1, "P1", row, col, turn=2)
        self.assert_accepted(p1, "P1", 6, 6, turn_after=3)     # G13, a corner of the box
        take(p2)

    def test_box_edge_and_beyond_are_allowed(self):
        for row, col in ((12, 9), (9, 12), (0, 0), (18, 9)):   # J7, M10, A19, J1
            with self.subTest(point=(row, col)):
                self.server.shutdown_all()
                self.server = PenteServer(tournament_rule=True)   # a fresh game each time
                p1, p2 = self.start_game()
                self.assert_accepted(p1, "P1", 9, 9, turn_after=1)
                self.assert_accepted(p2, "P2", 0, 18, turn_after=2)
                self.assert_accepted(p1, "P1", row, col, turn_after=3)

    def test_rule_ends_after_p1_second_stone(self):
        p1, p2 = self.start_game()
        for conn, pid, r, c, after in ((p1, "P1", 9, 9, 1), (p2, "P2", 9, 10, 2),
                                       (p1, "P1", 6, 6, 3), (p2, "P2", 10, 9, 4)):
            self.assert_accepted(conn, pid, r, c, after)
        self.assert_accepted(p1, "P1", 8, 8, turn_after=5)     # inside the box is fine now


class LobbyTests(FsmTestCase):
    def test_first_player_waits_with_standings(self):
        p1 = self.connect("Alice")
        msgs = take(p1)
        self.assertEqual(types(msgs), ["LOBBY_WAIT", "SCORE_UPDATE"])
        self.assertEqual(msgs[1]["payload"]["reason"], "STANDINGS")

    def test_second_player_starts_game(self):
        p1 = self.connect("Alice")
        take(p1)
        p2 = self.connect("Bob")
        for conn, me, opp in ((p1, "P1", "Bob"), (p2, "P2", "Alice")):
            msgs = take(conn)
            self.assertEqual(types(msgs), ["GAME_START", "SCORE_UPDATE", "STATE_UPDATE"])
            start = msgs[0]["payload"]
            self.assertEqual(start["your_player_id"], me)
            self.assertEqual(start["opponent_alias"], opp)
            self.assertEqual(len(start["session_token"]), 16)
            int(start["session_token"], 16)
            state = msgs[2]["payload"]
            self.assertEqual((state["turn_number"], state["active_player"], state["game_status"]),
                             (0, "P1", "ACTIVE"))
        self.assertEqual(self.server.game.state, PLAYER_TURN)

    def test_third_client_room_full(self):
        self.start_game()
        third = self.connect("Carol")
        err = self.last_error(third)
        self.assertEqual((err["code"], err["fatal"]), ("ROOM_FULL", True))
        self.assertTrue(third.close_requested)

    def test_alias_taken_invalid_alias_and_version(self):
        self.connect("Alice")
        dup = self.connect("Alice")
        self.assertEqual(self.last_error(dup)["code"], "ALIAS_TAKEN")
        bad = self.connect("no spaces!")
        self.assertEqual(self.last_error(bad)["code"], "INVALID_ALIAS")
        old = self.new_conn()
        msg = m.make_connect("Zed")
        msg["payload"]["protocol_version"] = 2
        self.server.on_message(old, frame(msg))
        self.assertEqual(self.last_error(old)["code"], "VERSION_MISMATCH")
        self.assertEqual(self.server.game.state, WAITING_FOR_PLAYERS)

    def test_p1_leaves_lobby(self):
        p1 = self.connect("Alice")
        self.server.on_connection_lost(p1, "EOF")  # L6
        self.assertEqual(self.server.game.seats, {})
        newcomer = self.connect("Bob")
        self.assertEqual(types(take(newcomer))[0], "LOBBY_WAIT")  # Bob is the new P1

    def test_move_in_lobby_and_unseated_move(self):
        p1 = self.connect("Alice")
        take(p1)
        self.move(p1, "P1", 9, 9)
        self.assertEqual(self.last_error(p1)["code"], "GAME_NOT_ACTIVE")  # L5
        stranger = self.new_conn()
        self.move(stranger, None, 9, 9)
        self.assertEqual(self.last_error(stranger)["code"], "GAME_NOT_ACTIVE")  # X2

    def test_too_many_connections(self):
        server = PenteServer(max_connections=2)
        conns = [Connection(next(_ids), FakeSocket(), ("h", 1)) for _ in range(3)]
        self.assertEqual([server.register_connection(c) for c in conns], [True, True, False])


class GameplayTests(FsmTestCase):
    def test_out_of_turn_move_rejected(self):
        p1, p2 = self.start_game()
        self.move(p2, "P2", 9, 9)
        err = self.last_error(p2)
        self.assertEqual((err["code"], err["fatal"]), ("NOT_YOUR_TURN", False))
        self.assertEqual(take(p1), [])  # nothing broadcast
        self.assertEqual(self.server.game.turn_number, 0)
        self.assertTrue(self.server.game.board.is_empty(9, 9))

    def test_validation_order_and_errors(self):
        p1, p2 = self.start_game()
        self.move(p1, "P1", 9, 9)
        take(p1), take(p2)
        self.move(p2, "P2", 9, 9)
        self.assertEqual(self.last_error(p2)["code"], "CELL_OCCUPIED")
        self.move(p2, "P2", 19, 0)
        self.assertEqual(self.last_error(p2)["code"], "OUT_OF_BOUNDS")
        self.move(p2, "P2", -1, 0)
        self.assertEqual(self.last_error(p2)["code"], "OUT_OF_BOUNDS")
        bad = m.make_move("P2", 1, 1)
        bad["payload"]["row"] = True
        self.server.on_message(p2, frame(bad))
        self.assertEqual(self.last_error(p2)["code"], "INVALID_PAYLOAD")
        bad["payload"] = {"row": 1}
        self.server.on_message(p2, frame(bad))
        self.assertEqual(self.last_error(p2)["code"], "MISSING_FIELD")
        # NOT_YOUR_TURN is checked before the payload (Blueprint §3.4 step 2 before 3).
        p1_bad = m.make_move("P1", 1, 1)
        p1_bad["payload"] = {"row": 1}
        self.server.on_message(p1, frame(p1_bad))
        self.assertEqual(self.last_error(p1)["code"], "NOT_YOUR_TURN")
        # Nothing changed, and P2 still has the turn.
        self.assertEqual(self.server.game.turn_number, 1)
        self.move(p2, "P2", 8, 8)
        self.assertEqual(types(take(p2)), ["STATE_UPDATE"])

    def test_player_id_mismatch_and_bad_frames(self):
        p1, p2 = self.start_game()
        self.move(p2, "P1", 9, 9)  # P2's socket pretends to be P1
        self.assertEqual(self.last_error(p2)["code"], "PLAYER_ID_MISMATCH")
        self.server.on_message(p1, b"{oops")
        self.assertEqual(self.last_error(p1)["code"], "MALFORMED_JSON")
        self.server.on_message(p1, frame(m.envelope("TELEPORT", "P1", {})))
        self.assertEqual(self.last_error(p1)["code"], "UNKNOWN_MSG_TYPE")
        self.assertFalse(p1.close_requested)  # non-fatal
        self.assertEqual(self.server.game.turn_number, 0)

    def test_broadcast_sends_identical_bytes(self):
        p1, p2 = self.start_game()
        self.move(p1, "P1", 9, 9)
        self.assertEqual(bytes(p1.sock.sent), bytes(p2.sock.sent))
        state = take(p1)[0]["payload"]
        self.assertEqual(state["active_player"], "P2")
        self.assertEqual(state["board"][9][9], "1")
        self.assertEqual(state["last_move"], {"player": "P1", "row": 9, "col": 9, "captured": []})

    def test_capture_updates_counts(self):
        p1, p2 = self.start_game()
        for conn, pid, r, c in ((p1, "P1", 9, 9), (p2, "P2", 9, 10), (p1, "P1", 0, 0),
                                (p2, "P2", 9, 11), (p1, "P1", 9, 12)):
            self.move(conn, pid, r, c)
        state = take(p1)[-1]["payload"]
        self.assertEqual(state["captures"], {"P1": 1, "P2": 0})
        self.assertEqual(state["last_move"]["captured"], [[9, 11], [9, 10]])
        self.assertEqual(state["board"][9][10:12], "..")

    def test_full_game_five_in_a_row_and_scoring(self):
        p1, p2 = self.start_game()
        for i in range(4):
            self.move(p1, "P1", 9, i)
            self.move(p2, "P2", 0, i)  # P2 builds a four (worth 1 point)
        take(p1), take(p2)
        self.move(p1, "P1", 9, 4)
        for conn in (p1, p2):
            msgs = take(conn)
            self.assertEqual(types(msgs), ["STATE_UPDATE", "GAME_OVER", "SCORE_UPDATE"])
            self.assertEqual(msgs[0]["payload"]["game_status"], "FINISHED")
            self.assertIsNone(msgs[0]["payload"]["active_player"])
            over = msgs[1]["payload"]
            self.assertEqual((over["result"], over["winner"], over["winner_alias"],
                              over["win_reason"], over["total_turns"]),
                             ("WIN", "P1", "Alice", "FIVE_IN_A_ROW", 9))
            score = msgs[2]["payload"]
            self.assertEqual(score["reason"], "ROUND_COMPLETE")
            self.assertEqual(score["round"]["game_id"], 1)
            self.assertEqual(score["round"]["points"]["P1"]["total"], 5)
            self.assertEqual(score["round"]["points"]["P2"]["four_points"], 1)
            self.assertEqual([(e["rank"], e["alias"], e["points"]) for e in score["leaderboard"]],
                             [(1, "Alice", 5), (2, "Bob", 1)])
            self.assertTrue(conn.sock.is_shut_down)  # CLEANUP
        self.assertEqual(self.server.game.state, WAITING_FOR_PLAYERS)
        self.assertEqual(self.server.game.seats, {})

    def test_five_captures_win(self):
        p1, p2 = self.start_game()
        g = self.server.game
        g.captures["P1"] = 4
        for r, c, s in ((9, 10, "2"), (9, 11, "2"), (9, 12, "1")):
            g.board.cells[r][c] = s
        self.move(p1, "P1", 9, 9)
        over = next(x for x in take(p2) if x["msg_type"] == "GAME_OVER")["payload"]
        self.assertEqual((over["winner"], over["win_reason"]), ("P1", "FIVE_CAPTURES"))
        self.assertEqual(over["final_captures"], {"P1": 5, "P2": 0})

    def test_board_full_tie_goes_to_p2(self):
        p1, p2 = self.start_game()
        g = self.server.game
        # Fill every cell except (18,18) in a pattern with no five and no captures.
        for r in range(19):
            for c in range(19):
                g.board.cells[r][c] = "1" if (c // 2 + r) % 2 == 0 else "2"
        g.board.cells[18][18] = "."
        g.captures = {"P1": 0, "P2": 0}
        self.move(p1, "P1", 18, 18)
        over = next(x for x in take(p1) if x["msg_type"] == "GAME_OVER")["payload"]
        self.assertEqual((over["winner"], over["win_reason"]), ("P2", "BOARD_FULL"))

    def test_leaderboard_survives_cleanup(self):
        for game_no in (1, 2):
            p1, p2 = self.start_game()
            for i in range(4):
                self.move(p1, "P1", 9, i)
                self.move(p2, "P2", 0, i * 2)
            self.move(p1, "P1", 9, 4)
            score = [x for x in take(p1) if x["msg_type"] == "SCORE_UPDATE"][-1]["payload"]
            self.assertEqual(score["round"]["game_id"], game_no)
            self.server.on_connection_lost(p1, "EOF")  # handlers wake after CLEANUP: X1
            self.server.on_connection_lost(p2, "EOF")
        alice = self.server.leaderboard["Alice"]
        self.assertEqual((alice.games_played, alice.wins, alice.points), (2, 2, 10))
        p1 = self.connect("Alice")
        standings = take(p1)[1]["payload"]
        self.assertEqual(standings["leaderboard"][0]["games_played"], 2)

    def test_simultaneous_moves_never_corrupt_turns(self):
        p1, p2 = self.start_game()
        # Cells chosen so nobody can capture or make five: same-color stones are
        # never adjacent, and the colors live on different rows.
        cells = {"P1": [(r, c) for r in (0, 4, 8, 12, 16) for c in range(0, 19, 2)],
                 "P2": [(r, c) for r in (2, 6, 10, 14, 18) for c in range(0, 19, 2)]}
        barrier = threading.Barrier(2)

        def spam(conn, pid):
            barrier.wait()
            for r, c in cells[pid]:
                for _ in range(200):  # retry until it is our turn
                    before = len(conn.sock.sent)
                    self.move(conn, pid, r, c)
                    if b"NOT_YOUR_TURN" not in bytes(conn.sock.sent[before:]):
                        break

        threads = [threading.Thread(target=spam, args=a) for a in ((p1, "P1"), (p2, "P2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        g = self.server.game
        stones = sum(cell != "." for row in g.board.cells for cell in row)
        self.assertEqual(stones, g.turn_number)
        p1_states = [x for x in take(p1) if x["msg_type"] == "STATE_UPDATE"]
        p2_states = [x for x in take(p2) if x["msg_type"] == "STATE_UPDATE"]
        self.assertEqual(p1_states, p2_states)  # same states, same order
        turns = [s["payload"]["turn_number"] for s in p1_states]
        self.assertEqual(turns, list(range(1, g.turn_number + 1)))
        players = [s["payload"]["last_move"]["player"] for s in p1_states]
        self.assertTrue(all(a != b for a, b in zip(players, players[1:])))  # strict alternation


class DisconnectTests(FsmTestCase):
    def test_disconnect_message_forfeits(self):
        p1, p2 = self.start_game()
        self.move(p1, "P1", 9, 9)
        take(p1), take(p2)
        self.server.on_message(p2, frame(m.make_disconnect("P2")))  # D1
        self.assertTrue(p2.close_requested)
        self.assertEqual(take(p2), [])  # the quitter is not sent the result
        msgs = take(p1)
        self.assertEqual(types(msgs), ["STATE_UPDATE", "GAME_OVER", "SCORE_UPDATE"])
        over = msgs[1]["payload"]
        self.assertEqual((over["result"], over["winner"], over["win_reason"]),
                         ("FORFEIT", "P1", "OPPONENT_QUIT"))
        self.assertEqual(self.server.leaderboard["Bob"].losses, 1)

    def test_drop_pause_and_reconnect(self):
        p1, p2 = self.start_game()
        self.move(p1, "P1", 9, 9)
        take(p1), take(p2)
        self.server.on_connection_lost(p2, "EOF")  # D2
        self.assertEqual(self.server.game.state, PAUSED_RECONNECT)
        msgs = take(p1)
        self.assertEqual(types(msgs), ["PLAYER_STATUS", "STATE_UPDATE"])
        self.assertEqual((msgs[0]["payload"]["status"], msgs[0]["payload"]["grace_seconds_remaining"]),
                         ("DISCONNECTED", 60))
        self.assertEqual(msgs[1]["payload"]["game_status"], "PAUSED_RECONNECT")
        self.assertIsNone(msgs[1]["payload"]["active_player"])

        self.move(p1, "P1", 0, 0)
        self.assertEqual(self.last_error(p1)["code"], "GAME_NOT_ACTIVE")  # D6

        p2b = self.new_conn()
        self.server.on_message(p2b, frame(m.make_reconnect("P2", "Bob", self.tokens["P2"])))  # D4
        rejoin = take(p2b)
        self.assertEqual(types(rejoin), ["GAME_START", "SCORE_UPDATE", "STATE_UPDATE"])
        self.assertTrue(rejoin[0]["payload"]["resumed"])
        self.assertEqual(rejoin[0]["payload"]["your_player_id"], "P2")
        msgs = take(p1)
        self.assertEqual(types(msgs), ["PLAYER_STATUS", "STATE_UPDATE"])
        self.assertEqual(msgs[0]["payload"]["status"], "RECONNECTED")
        self.assertEqual(msgs[1]["payload"]["active_player"], "P2")  # same as before the drop
        self.assertIsNone(self.server.game.grace_timer)
        self.move(p2b, "P2", 9, 10)
        self.assertEqual(types(take(p2b)), ["STATE_UPDATE"])

    def test_reconnect_with_bad_token(self):
        p1, p2 = self.start_game()
        self.server.on_connection_lost(p2, "EOF")
        intruder = self.new_conn()
        self.server.on_message(intruder, frame(m.make_reconnect("P2", "Bob", "0" * 16)))  # D5
        err = self.last_error(intruder)
        self.assertEqual((err["code"], err["fatal"]), ("INVALID_TOKEN", True))
        self.assertEqual(self.server.game.state, PAUSED_RECONNECT)  # still waiting

    def test_reconnect_when_nothing_paused(self):
        stranger = self.new_conn()
        self.server.on_message(stranger, frame(m.make_reconnect(None, "Bob", "0" * 16)))  # X3
        self.assertEqual(self.last_error(stranger)["code"], "INVALID_TOKEN")

    def test_both_players_lost_is_not_scored(self):
        p1, p2 = self.start_game()
        self.server.on_connection_lost(p2, "EOF")  # D2
        self.server.on_connection_lost(p1, "ABRUPT")  # D8
        self.assertEqual(self.server.game.state, WAITING_FOR_PLAYERS)
        self.assertEqual(self.server.leaderboard, {})

    def test_connection_lost_is_idempotent(self):
        p1, p2 = self.start_game()
        self.server.on_connection_lost(p2, "EOF")
        seq = self.server.game.pause_seq
        self.server.on_connection_lost(p2, "CLOSED")
        self.assertEqual(self.server.game.pause_seq, seq)

    def test_failed_send_marks_dead_but_finishes_broadcast(self):
        p1, p2 = self.start_game()
        p2.sock.fail_sends = True
        self.move(p1, "P1", 9, 9)
        self.assertTrue(p2.dead)
        self.assertTrue(p2.sock.is_shut_down)
        self.assertEqual(types(take(p1)), ["STATE_UPDATE"])
        self.server.on_connection_lost(p2, "EOF")  # its handler reports the drop later
        self.assertEqual(self.server.game.state, PAUSED_RECONNECT)

    def test_stale_timer_is_ignored(self):
        p1, p2 = self.start_game()
        self.server.on_connection_lost(p2, "EOF")
        old = (self.server.game_id, self.server.game.pause_seq)
        p2b = self.new_conn()
        self.server.on_message(p2b, frame(m.make_reconnect("P2", "Bob", self.tokens["P2"])))
        self.server.on_grace_timeout(*old)  # fires late: game already resumed
        self.assertEqual(self.server.game.state, PLAYER_TURN)
        self.server.on_connection_lost(p2b, "EOF")  # paused again, pause_seq + 1
        self.server.on_grace_timeout(*old)  # old timer still must not forfeit
        self.assertEqual(self.server.game.state, PAUSED_RECONNECT)


class GraceTimeoutTests(FsmTestCase):
    grace = 0.2

    def test_timeout_forfeits_to_remaining_player(self):
        p1, p2 = self.start_game()
        self.server.on_connection_lost(p2, "ABRUPT")
        take(p1)
        deadline = time.monotonic() + 3
        while self.server.game.state != WAITING_FOR_PLAYERS and time.monotonic() < deadline:
            time.sleep(0.02)
        msgs = take(p1)
        self.assertEqual(types(msgs), ["STATE_UPDATE", "GAME_OVER", "SCORE_UPDATE"])
        over = msgs[1]["payload"]
        self.assertEqual((over["result"], over["winner"], over["win_reason"]),
                         ("FORFEIT", "P1", "OPPONENT_TIMEOUT"))


if __name__ == "__main__":
    unittest.main()
