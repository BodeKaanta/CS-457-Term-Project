"""Codec and validation tests (Blueprint §2-§3)."""

import json
import unittest

from pente import messages as m


def raw(obj) -> bytes:
    return json.dumps(obj).encode()


class DecodeTests(unittest.TestCase):
    def test_not_json(self):
        with self.assertRaises(m.ProtocolError) as cm:
            m.decode(b"{not json")
        self.assertEqual(cm.exception.code, "MALFORMED_JSON")

    def test_not_utf8(self):
        with self.assertRaises(m.ProtocolError) as cm:
            m.decode(b"\xff\xfe")
        self.assertEqual(cm.exception.code, "MALFORMED_JSON")

    def test_not_an_object(self):
        with self.assertRaises(m.ProtocolError) as cm:
            m.decode(b"[1,2,3]")
        self.assertEqual(cm.exception.code, "MALFORMED_JSON")


class ValidateTests(unittest.TestCase):
    def check(self, msg, code):
        with self.assertRaises(m.ProtocolError) as cm:
            m.validate_client_message(msg)
        self.assertEqual(cm.exception.code, code)

    def test_blueprint_client_examples_are_valid(self):
        examples = [
            '{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Alice","protocol_version":1},"timestamp":1791266400}',
            '{"msg_type":"MOVE","player_id":"P1","payload":{"row":9,"col":9},"timestamp":1791266420}',
            '{"msg_type":"DISCONNECT","player_id":"P2","payload":{"reason":"USER_QUIT"},"timestamp":1791266470}',
            '{"msg_type":"RECONNECT","player_id":"P2","payload":{"alias":"Bob","session_token":"3b7e19c0a4f25d88","protocol_version":1},"timestamp":1791266490}',
        ]
        for text in examples:
            m.validate_client_message(m.decode(text.encode()))

    def test_missing_envelope_key(self):
        self.check({"msg_type": "MOVE", "player_id": "P1", "payload": {}}, "MISSING_FIELD")

    def test_unknown_type(self):
        self.check(m.envelope("JUMP", "P1", {}), "UNKNOWN_MSG_TYPE")

    def test_server_only_type_from_client(self):
        self.check(m.envelope("STATE_UPDATE", "P1", {}), "UNKNOWN_MSG_TYPE")

    def test_payload_not_object(self):
        msg = m.make_move("P1", 1, 1)
        msg["payload"] = [1, 1]
        self.check(msg, "INVALID_PAYLOAD")

    def test_connect_missing_alias(self):
        self.check(m.envelope("CONNECT", None, {"protocol_version": 1}), "MISSING_FIELD")

    def test_connect_version_wrong_type(self):
        self.check(m.envelope("CONNECT", None, {"alias": "A", "protocol_version": "1"}),
                   "INVALID_PAYLOAD")

    def test_bool_is_not_an_int(self):
        self.check(m.envelope("CONNECT", None, {"alias": "A", "protocol_version": True}),
                   "INVALID_PAYLOAD")

    def test_bad_disconnect_reason(self):
        self.check(m.make_disconnect("P1", "BORED"), "INVALID_PAYLOAD")

    def test_extra_keys_ignored(self):
        msg = m.make_connect("Alice")
        msg["extra"] = 1
        m.validate_client_message(msg)


class BuilderTests(unittest.TestCase):
    def test_error_fatal_flag_follows_code(self):
        self.assertTrue(m.make_error("ROOM_FULL", "x")["payload"]["fatal"])
        self.assertFalse(m.make_error("NOT_YOUR_TURN", "x")["payload"]["fatal"])

    def test_game_start_matches_blueprint_example(self):
        expected = json.loads('{"msg_type":"GAME_START","player_id":"SERVER","payload":{"your_player_id":"P2","your_alias":"Bob","opponent_alias":"Alice","session_token":"3b7e19c0a4f25d88","board_size":19,"first_player":"P1","stones_to_win":5,"captures_to_win":5,"resumed":false},"timestamp":1791266412}')
        built = m.make_game_start("P2", "Bob", "Alice", "3b7e19c0a4f25d88", resumed=False)
        built["timestamp"] = expected["timestamp"]
        self.assertEqual(built, expected)

    def test_lobby_wait_matches_blueprint_example(self):
        expected = json.loads('{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"players_connected":1,"players_needed":2,"message":"Waiting for Player 2..."},"timestamp":1791266400}')
        built = m.make_lobby_wait()
        built["timestamp"] = expected["timestamp"]
        self.assertEqual(built, expected)

    def test_every_builder_uses_the_envelope(self):
        for msg in (m.make_lobby_wait(), m.make_error("CELL_OCCUPIED", "x"),
                    m.make_player_status("P2", "RECONNECTED", None, "back"),
                    m.make_score_update("STANDINGS", None, [])):
            self.assertEqual(set(msg), set(m.ENVELOPE_KEYS))
            self.assertEqual(msg["player_id"], "SERVER")
            self.assertIn(msg["msg_type"], m.MSG_TYPES)

    def test_alias_rule(self):
        self.assertTrue(m.is_valid_alias("Alice_99"))
        for bad in ("", "a" * 17, "has space", "semi;colon", 5, None):
            self.assertFalse(m.is_valid_alias(bad))


if __name__ == "__main__":
    unittest.main()
