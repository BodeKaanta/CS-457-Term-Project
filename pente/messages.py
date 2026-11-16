"""Pente Application Protocol (PAP v1) message vocabulary and codec.

Blueprint §2 defines the envelope shared by every message and §3 defines the
11 message types. This module holds the closed sets of names, decodes and
validates client messages, and builds every server message so field names
are written in exactly one place.

Game-rule checks (turn order, bounds, occupancy) are NOT done here. They
belong to the FSM, which must apply them in the order of Blueprint §3.4.
"""

import json
import re
import time

PROTOCOL_VERSION = 1
SERVER_ID = "SERVER"
BOARD_SIZE = 19
STONES_TO_WIN = 5
CAPTURES_TO_WIN = 5

# Blueprint §3: the 11 message types (closed set).
CONNECT = "CONNECT"
LOBBY_WAIT = "LOBBY_WAIT"
GAME_START = "GAME_START"
MOVE = "MOVE"
STATE_UPDATE = "STATE_UPDATE"
ERROR = "ERROR"
DISCONNECT = "DISCONNECT"
GAME_OVER = "GAME_OVER"
RECONNECT = "RECONNECT"
PLAYER_STATUS = "PLAYER_STATUS"
SCORE_UPDATE = "SCORE_UPDATE"

MSG_TYPES = frozenset({
    CONNECT, LOBBY_WAIT, GAME_START, MOVE, STATE_UPDATE, ERROR,
    DISCONNECT, GAME_OVER, RECONNECT, PLAYER_STATUS, SCORE_UPDATE,
})
CLIENT_MSG_TYPES = frozenset({CONNECT, MOVE, DISCONNECT, RECONNECT})

# Blueprint §3.6: the 15 error codes (closed set) and which ones are fatal.
ERROR_CODES = frozenset({
    "MALFORMED_JSON", "MISSING_FIELD", "UNKNOWN_MSG_TYPE", "INVALID_PAYLOAD",
    "PLAYER_ID_MISMATCH", "NOT_YOUR_TURN", "OUT_OF_BOUNDS", "CELL_OCCUPIED",
    "GAME_NOT_ACTIVE", "INVALID_ALIAS", "ALIAS_TAKEN", "ROOM_FULL",
    "VERSION_MISMATCH", "INVALID_TOKEN", "FRAME_TOO_LARGE",
})
FATAL_CODES = frozenset({
    "INVALID_ALIAS", "ALIAS_TAKEN", "ROOM_FULL", "VERSION_MISMATCH",
    "INVALID_TOKEN", "FRAME_TOO_LARGE",
})

DISCONNECT_REASONS = frozenset({"USER_QUIT", "CLIENT_SHUTDOWN"})

# Blueprint §3.1: alias is 1-16 characters from [A-Za-z0-9_].
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9_]{1,16}")

ENVELOPE_KEYS = ("msg_type", "player_id", "payload", "timestamp")

# Payload fields checked by validate_client_message: name -> required type.
# MOVE is absent on purpose: its row/col checks are step 3 of the FSM's
# validation order (Blueprint §3.4), after the game-status and turn checks.
_PAYLOAD_FIELDS = {
    CONNECT: {"alias": str, "protocol_version": int},
    RECONNECT: {"alias": str, "session_token": str, "protocol_version": int},
    DISCONNECT: {"reason": str},
    MOVE: {},
}


class ProtocolError(Exception):
    """A client message broke the protocol. Maps directly to an ERROR message."""

    def __init__(self, code: str, detail: str, ref_msg_type: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        assert code in ERROR_CODES, code
        self.code = code
        self.detail = detail
        self.ref_msg_type = ref_msg_type


def is_int(value: object) -> bool:
    """True for a real JSON integer. bool is a subclass of int in Python, so exclude it."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_valid_alias(alias: object) -> bool:
    """Blueprint §3.1 alias rule."""
    return isinstance(alias, str) and ALIAS_PATTERN.fullmatch(alias) is not None


def decode(frame: bytes) -> dict:
    """Turn one frame into a dict, or raise ProtocolError(MALFORMED_JSON)."""
    try:
        msg = json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError("MALFORMED_JSON", f"Frame is not valid UTF-8 JSON: {e}") from None
    if not isinstance(msg, dict):
        raise ProtocolError("MALFORMED_JSON", "Frame is not a JSON object.")
    return msg


def validate_client_message(msg: dict) -> None:
    """Check the envelope (Blueprint §2) and the payload fields of a client message.

    Raises ProtocolError with MISSING_FIELD, UNKNOWN_MSG_TYPE or INVALID_PAYLOAD.
    Unknown extra keys are ignored for forward compatibility.
    """
    raw_type = msg.get("msg_type")
    ref = raw_type if isinstance(raw_type, str) else None

    for key in ENVELOPE_KEYS:
        if key not in msg:
            raise ProtocolError("MISSING_FIELD", f"Envelope key '{key}' is missing.", ref)

    if not isinstance(raw_type, str):
        raise ProtocolError("INVALID_PAYLOAD", "'msg_type' must be a string.", None)
    if raw_type not in CLIENT_MSG_TYPES:
        if raw_type in MSG_TYPES:
            detail = f"'{raw_type}' is a server-to-client message type."
        else:
            detail = f"'{raw_type}' is not a defined message type."
        raise ProtocolError("UNKNOWN_MSG_TYPE", detail, ref)

    if msg["player_id"] is not None and not isinstance(msg["player_id"], str):
        raise ProtocolError("INVALID_PAYLOAD", "'player_id' must be a string or null.", ref)
    if not is_int(msg["timestamp"]):
        raise ProtocolError("INVALID_PAYLOAD", "'timestamp' must be an integer.", ref)
    payload = msg["payload"]
    if not isinstance(payload, dict):
        raise ProtocolError("INVALID_PAYLOAD", "'payload' must be a JSON object.", ref)

    for field_name, field_type in _PAYLOAD_FIELDS[raw_type].items():
        if field_name not in payload:
            raise ProtocolError("MISSING_FIELD", f"Payload field '{field_name}' is missing.", ref)
        value = payload[field_name]
        ok = is_int(value) if field_type is int else isinstance(value, field_type)
        if not ok:
            detail = f"Payload field '{field_name}' must be {field_type.__name__}."
            raise ProtocolError("INVALID_PAYLOAD", detail, ref)

    if raw_type == DISCONNECT and payload["reason"] not in DISCONNECT_REASONS:
        raise ProtocolError(
            "INVALID_PAYLOAD", "'reason' must be USER_QUIT or CLIENT_SHUTDOWN.", ref)


# ---------------------------------------------------------------------------
# Message builders. Each returns a full envelope; parameters map 1:1 to the
# payload tables in Blueprint §3.
# ---------------------------------------------------------------------------

def envelope(msg_type: str, player_id: str | None, payload: dict) -> dict:
    """Build the common four-key envelope (Blueprint §2)."""
    return {
        "msg_type": msg_type,
        "player_id": player_id,
        "payload": payload,
        "timestamp": int(time.time()),
    }


# Client -> Server (Blueprint §3.1, §3.4, §3.7, §3.9)

def make_connect(alias: str) -> dict:
    return envelope(CONNECT, None, {"alias": alias, "protocol_version": PROTOCOL_VERSION})


def make_move(player_id: str, row: int, col: int) -> dict:
    return envelope(MOVE, player_id, {"row": row, "col": col})


def make_disconnect(player_id: str | None, reason: str = "USER_QUIT") -> dict:
    return envelope(DISCONNECT, player_id, {"reason": reason})


def make_reconnect(player_id: str | None, alias: str, session_token: str) -> dict:
    return envelope(RECONNECT, player_id, {
        "alias": alias,
        "session_token": session_token,
        "protocol_version": PROTOCOL_VERSION,
    })


# Server -> Client (Blueprint §3.2, §3.3, §3.5, §3.6, §3.8, §3.10, §3.11)

def make_lobby_wait() -> dict:
    return envelope(LOBBY_WAIT, SERVER_ID, {
        "players_connected": 1,
        "players_needed": 2,
        "message": "Waiting for Player 2...",
    })


def make_game_start(your_player_id: str, your_alias: str, opponent_alias: str,
                    session_token: str, resumed: bool) -> dict:
    return envelope(GAME_START, SERVER_ID, {
        "your_player_id": your_player_id,
        "your_alias": your_alias,
        "opponent_alias": opponent_alias,
        "session_token": session_token,
        "board_size": BOARD_SIZE,
        "first_player": "P1",
        "stones_to_win": STONES_TO_WIN,
        "captures_to_win": CAPTURES_TO_WIN,
        "resumed": resumed,
    })


def make_state_update(board: list[str], turn_number: int, active_player: str | None,
                      captures: dict, last_move: dict | None, game_status: str) -> dict:
    return envelope(STATE_UPDATE, SERVER_ID, {
        "board": board,
        "turn_number": turn_number,
        "active_player": active_player,
        "captures": dict(captures),
        "last_move": last_move,
        "game_status": game_status,
    })


def make_error(code: str, detail: str, ref_msg_type: str | None = None) -> dict:
    """ERROR message. 'fatal' is derived from the code so it can never disagree."""
    assert code in ERROR_CODES, code
    return envelope(ERROR, SERVER_ID, {
        "code": code,
        "detail": detail,
        "fatal": code in FATAL_CODES,
        "ref_msg_type": ref_msg_type,
    })


def make_game_over(result: str, winner: str, winner_alias: str, win_reason: str,
                   final_captures: dict, final_board: list[str], total_turns: int) -> dict:
    return envelope(GAME_OVER, SERVER_ID, {
        "result": result,
        "winner": winner,
        "winner_alias": winner_alias,
        "win_reason": win_reason,
        "final_captures": dict(final_captures),
        "final_board": final_board,
        "total_turns": total_turns,
    })


def make_player_status(subject_player: str, status: str,
                       grace_seconds_remaining: int | None, message: str) -> dict:
    return envelope(PLAYER_STATUS, SERVER_ID, {
        "subject_player": subject_player,
        "status": status,
        "grace_seconds_remaining": grace_seconds_remaining,
        "message": message,
    })


def make_score_update(reason: str, round_result: dict | None, leaderboard: list[dict]) -> dict:
    return envelope(SCORE_UPDATE, SERVER_ID, {
        "reason": reason,
        "round": round_result,
        "leaderboard": leaderboard,
    })
