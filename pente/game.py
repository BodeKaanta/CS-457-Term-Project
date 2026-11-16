"""Pure Pente game rules: placement, captures, wins, and Points Pente scoring.

FSM spec §4 and §4.1. No sockets and no threading here, so every rule can be
unit tested on its own. The FSM calls these functions while holding its lock.

Board cells are "." (empty), "1" (P1 stone) or "2" (P2 stone).
Coordinates are (row, col), both 0-18, row 0 at the top.
"""

from dataclasses import asdict, dataclass

SIZE = 19
EMPTY = "."
STONE = {"P1": "1", "P2": "2"}

# All 8 directions, used for captures (FSM spec §4 "Capture").
ALL_DIRECTIONS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
# One direction per line, used for runs: right, down, down-right, down-left.
LINE_DIRECTIONS = [(0, 1), (1, 0), (1, 1), (1, -1)]


def active_player(turn_number: int) -> str:
    """P1 moves on even turn numbers, P2 on odd ones (FSM spec §4)."""
    return "P1" if turn_number % 2 == 0 else "P2"


def opponent(player_id: str) -> str:
    return "P2" if player_id == "P1" else "P1"


def in_bounds(row: int, col: int) -> bool:
    return 0 <= row < SIZE and 0 <= col < SIZE


class Board:
    """19x19 Pente board."""

    def __init__(self) -> None:
        self.cells = [[EMPTY] * SIZE for _ in range(SIZE)]

    @classmethod
    def from_wire(cls, rows: list[str]) -> "Board":
        """Build a board from the 19-string wire encoding (Blueprint §2)."""
        board = cls()
        board.cells = [list(row) for row in rows]
        return board

    def to_wire(self) -> list[str]:
        """19 strings of 19 characters (Blueprint §2 board encoding)."""
        return ["".join(row) for row in self.cells]

    def get(self, row: int, col: int) -> str:
        return self.cells[row][col]

    def is_empty(self, row: int, col: int) -> bool:
        return self.cells[row][col] == EMPTY

    def is_full(self) -> bool:
        return all(cell != EMPTY for row in self.cells for cell in row)

    def place(self, row: int, col: int, stone: str) -> list[list[int]]:
        """Place a stone and remove every pair it captures.

        A capture is the pattern X O O X along one of the 8 directions, where
        X is the stone just placed. Exactly two stones are taken, never one or
        three. Moving INTO the middle of X _ _ X is safe, because only the
        stone that closes the pattern captures.

        Returns the removed coordinates as [[row, col], ...] (wire format).
        The caller must have checked bounds and occupancy already.
        """
        assert self.is_empty(row, col), "place() on an occupied cell"
        self.cells[row][col] = stone
        enemy = "2" if stone == "1" else "1"
        captured = []
        for dr, dc in ALL_DIRECTIONS:
            r1, c1 = row + dr, col + dc
            r2, c2 = row + 2 * dr, col + 2 * dc
            r3, c3 = row + 3 * dr, col + 3 * dc
            if not in_bounds(r3, c3):
                continue
            if (self.cells[r1][c1] == enemy and self.cells[r2][c2] == enemy
                    and self.cells[r3][c3] == stone):
                self.cells[r1][c1] = EMPTY
                self.cells[r2][c2] = EMPTY
                captured.extend([[r1, c1], [r2, c2]])
        return captured

    def run_length(self, row: int, col: int, dr: int, dc: int) -> int:
        """Length of the line of same-colored stones through (row, col) along (dr, dc)."""
        stone = self.cells[row][col]
        length = 1
        for sign in (1, -1):
            r, c = row + sign * dr, col + sign * dc
            while in_bounds(r, c) and self.cells[r][c] == stone:
                length += 1
                r += sign * dr
                c += sign * dc
        return length

    def five_in_a_row(self, row: int, col: int) -> bool:
        """True if the stone at (row, col) is part of a line of 5 or more."""
        if self.is_empty(row, col):
            return False
        return any(self.run_length(row, col, dr, dc) >= 5 for dr, dc in LINE_DIRECTIONS)


def count_fours(board: Board, stone: str) -> int:
    """Count runs of EXACTLY four of `stone` in a straight line (FSM spec §4.1).

    Each run is counted once, from its first stone. Runs of five or more are
    the winning line and are not counted as fours.
    """
    fours = 0
    cells = board.cells
    for r in range(SIZE):
        for c in range(SIZE):
            if cells[r][c] != stone:
                continue
            for dr, dc in LINE_DIRECTIONS:
                pr, pc = r - dr, c - dc
                if in_bounds(pr, pc) and cells[pr][pc] == stone:
                    continue  # not the start of this run
                length = 0
                while in_bounds(r + length * dr, c + length * dc) \
                        and cells[r + length * dr][c + length * dc] == stone:
                    length += 1
                if length == 4:
                    fours += 1
    return fours


def board_full_winner(captures: dict) -> str:
    """G10 safety rule: more captured pairs wins; a tie goes to P2."""
    return "P1" if captures["P1"] > captures["P2"] else "P2"


@dataclass
class RoundScore:
    """One player's points for one finished game (concurrency design §6.2)."""
    alias: str
    five_bonus: int
    capture_points: int
    four_points: int
    total: int

    def to_wire(self) -> dict:
        return asdict(self)


def score_round(board: Board, captures: dict, aliases: dict, win_reason: str,
                winner: str) -> dict:
    """Score a finished game for both players with Points Pente rules.

    +5 bonus only for the FIVE_IN_A_ROW winner, +1 per captured pair, and
    +1 per four-in-a-row still on the final board.

    Returns {"P1": RoundScore, "P2": RoundScore}.
    """
    scores = {}
    for pid in ("P1", "P2"):
        five_bonus = 5 if (win_reason == "FIVE_IN_A_ROW" and pid == winner) else 0
        capture_points = captures[pid]
        four_points = count_fours(board, STONE[pid])
        scores[pid] = RoundScore(
            alias=aliases[pid],
            five_bonus=five_bonus,
            capture_points=capture_points,
            four_points=four_points,
            total=five_bonus + capture_points + four_points,
        )
    return scores
