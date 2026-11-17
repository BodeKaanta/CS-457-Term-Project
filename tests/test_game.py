"""Pente rule and scoring tests (FSM spec §4, §4.1)."""

import unittest

from pente.game import (Board, active_player, board_full_winner, count_fours,
                        score_round)


def board_with(stones: dict) -> Board:
    """stones: {(row, col): "1" or "2"}"""
    b = Board()
    for (r, c), s in stones.items():
        b.cells[r][c] = s
    return b


class CaptureTests(unittest.TestCase):
    def test_capture_in_every_direction(self):
        for dr, dc in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]:
            r, c = 9, 9
            b = board_with({(r + dr, c + dc): "2", (r + 2 * dr, c + 2 * dc): "2",
                            (r + 3 * dr, c + 3 * dc): "1"})
            captured = b.place(r, c, "1")
            self.assertEqual(sorted(map(tuple, captured)),
                             sorted([(r + dr, c + dc), (r + 2 * dr, c + 2 * dc)]),
                             f"direction {(dr, dc)}")
            self.assertTrue(b.is_empty(r + dr, c + dc))
            self.assertTrue(b.is_empty(r + 2 * dr, c + 2 * dc))

    def test_blueprint_example(self):
        # FSM spec §4: P1 at (9,10) captures P2 at (9,11),(9,12) against P1 at (9,13).
        b = board_with({(9, 11): "2", (9, 12): "2", (9, 13): "1"})
        self.assertEqual(b.place(9, 10, "1"), [[9, 11], [9, 12]])

    def test_moving_into_flanked_position_is_safe(self):
        # X _ O X: P2 plays into the gap, making X O O X itself. No capture.
        b = board_with({(9, 9): "1", (9, 11): "2", (9, 12): "1"})
        self.assertEqual(b.place(9, 10, "2"), [])
        self.assertEqual(b.get(9, 10), "2")
        self.assertEqual(b.get(9, 11), "2")

    def test_single_stone_not_captured(self):
        b = board_with({(9, 10): "2", (9, 11): "1"})
        self.assertEqual(b.place(9, 9, "1"), [])

    def test_three_stones_not_captured(self):
        b = board_with({(9, 10): "2", (9, 11): "2", (9, 12): "2", (9, 13): "1"})
        self.assertEqual(b.place(9, 9, "1"), [])

    def test_double_capture(self):
        b = board_with({(9, 10): "2", (9, 11): "2", (9, 12): "1",
                        (10, 9): "2", (11, 9): "2", (12, 9): "1"})
        self.assertEqual(len(b.place(9, 9, "1")), 4)

    def test_capture_near_edge_does_not_wrap(self):
        b = board_with({(0, 17): "2", (0, 18): "2"})
        self.assertEqual(b.place(0, 16, "1"), [])


class WinTests(unittest.TestCase):
    def test_five_in_each_line_direction(self):
        for dr, dc in [(0, 1), (1, 0), (1, 1), (1, -1)]:
            b = board_with({(5 + i * dr, 9 + i * dc): "1" for i in range(5)})
            for i in range(5):
                self.assertTrue(b.five_in_a_row(5 + i * dr, 9 + i * dc), (dr, dc, i))

    def test_four_is_not_a_win(self):
        b = board_with({(9, c): "1" for c in range(4)})
        self.assertFalse(b.five_in_a_row(9, 0))

    def test_six_is_a_win(self):
        b = board_with({(9, c): "1" for c in range(6)})
        self.assertTrue(b.five_in_a_row(9, 3))

    def test_mixed_colors_break_the_line(self):
        b = board_with({(9, 0): "1", (9, 1): "1", (9, 2): "2", (9, 3): "1", (9, 4): "1"})
        self.assertFalse(b.five_in_a_row(9, 0))

    def test_turn_order_and_board_full_rule(self):
        self.assertEqual([active_player(t) for t in range(4)], ["P1", "P2", "P1", "P2"])
        self.assertEqual(board_full_winner({"P1": 3, "P2": 2}), "P1")
        self.assertEqual(board_full_winner({"P1": 2, "P2": 2}), "P2")  # tie -> P2


class ScoringTests(unittest.TestCase):
    def test_count_fours(self):
        cases = {
            "open four": ({(9, c): "1" for c in range(5, 9)}, 1),
            "blocked four": ({**{(9, c): "1" for c in range(0, 4)}, (9, 4): "2"}, 1),
            "five is not a four": ({(9, c): "1" for c in range(5)}, 0),
            "six is not a four": ({(9, c): "1" for c in range(6)}, 0),
            "three is not a four": ({(9, c): "1" for c in range(3)}, 0),
            "vertical": ({(r, 3): "1" for r in range(4)}, 1),
            "diagonal": ({(i, i): "1" for i in range(4)}, 1),
            "anti-diagonal": ({(i, 10 - i): "1" for i in range(4)}, 1),
            "two separate fours": ({**{(0, c): "1" for c in range(4)},
                                    **{(5, c): "1" for c in range(4)}}, 2),
        }
        for name, (stones, expected) in cases.items():
            self.assertEqual(count_fours(board_with(stones), "1"), expected, name)

    def test_fours_of_other_color_ignored(self):
        self.assertEqual(count_fours(board_with({(9, c): "2" for c in range(4)}), "1"), 0)

    def test_score_round_five_in_a_row(self):
        # Concurrency design §6.1 example 1 shape: winner gets the bonus + captures + fours.
        stones = {**{(9, c): "1" for c in range(5)},       # winning five
                  **{(2, c): "1" for c in range(4)},       # a P1 four
                  **{(15, c): "2" for c in range(4)}}      # a P2 four
        scores = score_round(board_with(stones), {"P1": 2, "P2": 1},
                             {"P1": "Alice", "P2": "Bob"}, "FIVE_IN_A_ROW", "P1")
        self.assertEqual(scores["P1"].to_wire(), {"alias": "Alice", "five_bonus": 5,
                                                  "capture_points": 2, "four_points": 1,
                                                  "total": 8})
        self.assertEqual(scores["P2"].total, 2)

    def test_no_bonus_for_capture_win_or_forfeit(self):
        for reason in ("FIVE_CAPTURES", "OPPONENT_QUIT", "OPPONENT_TIMEOUT", "BOARD_FULL"):
            scores = score_round(Board(), {"P1": 5, "P2": 0}, {"P1": "A", "P2": "B"},
                                 reason, "P1")
            self.assertEqual(scores["P1"].five_bonus, 0, reason)
            self.assertEqual(scores["P1"].total, 5, reason)


class NotationTests(unittest.TestCase):
    def test_round_trip_every_point(self):
        from pente.game import SIZE, from_notation, to_notation
        for r in range(SIZE):
            for c in range(SIZE):
                self.assertEqual(from_notation(to_notation(r, c)), (r, c))

    def test_examples(self):
        from pente.game import to_notation
        self.assertEqual(to_notation(9, 9), "J10")     # center
        self.assertEqual(to_notation(0, 0), "A19")     # top-left
        self.assertEqual(to_notation(18, 0), "A1")     # bottom-left, like chess a1
        self.assertEqual(to_notation(18, 18), "S1")
        self.assertEqual(to_notation(19, 5), "(row 19, col 5)")  # off the board


class OpeningRuleTests(unittest.TestCase):
    def test_first_move_center_only(self):
        from pente.game import opening_rule_error
        self.assertIsNone(opening_rule_error(0, 9, 9))
        self.assertIn("J10", opening_rule_error(0, 9, 10))

    def test_second_p1_move_on_or_outside_box(self):
        from pente.game import opening_rule_error
        self.assertIn("G7 to M13", opening_rule_error(2, 8, 8))
        self.assertIsNotNone(opening_rule_error(2, 11, 11))    # 2 steps: inside
        self.assertIsNone(opening_rule_error(2, 12, 12))       # 3 steps: on the box
        self.assertIsNone(opening_rule_error(2, 6, 9))         # J13, box edge
        self.assertIsNone(opening_rule_error(2, 0, 0))         # far outside

    def test_other_turns_unrestricted(self):
        from pente.game import opening_rule_error
        for turn in (1, 3, 4, 10):
            self.assertIsNone(opening_rule_error(turn, 9, 10))


if __name__ == "__main__":
    unittest.main()
