"""Framing tests using the wire examples from Blueprint §1.2."""

import json
import unittest

from pente.framing import MAX_FRAME, FrameReader, FrameTooLarge, encode_frame

MOVE = b'{"msg_type":"MOVE","player_id":"P1","payload":{"row":9,"col":9},"timestamp":1791266420}'
QUIT = b'{"msg_type":"DISCONNECT","player_id":"P1","payload":{"reason":"USER_QUIT"},"timestamp":1791266421}'


class FrameReaderTests(unittest.TestCase):
    def test_coalesced_messages_in_one_recv(self):
        # Example B: two messages arrive in a single recv().
        frames = FrameReader().feed(MOVE + b"\n" + QUIT + b"\n")
        self.assertEqual(frames, [MOVE, QUIT])

    def test_fragmented_message_across_three_recvs(self):
        # Example C: one MOVE split across three recv() calls, plus the start of the next.
        reader = FrameReader()
        msg = b'{"msg_type":"MOVE","player_id":"P2","payload":{"row":9,"col":10},"timestamp":1791266433}'
        self.assertEqual(reader.feed(b'{"msg_type":"MO'), [])
        self.assertEqual(reader.feed(b'VE","player_id":"P2","payload":{"row":9,'), [])
        frames = reader.feed(b'"col":10},"timestamp":1791266433}\n{"msg_type":"MO')
        self.assertEqual(frames, [msg])
        self.assertEqual(bytes(reader.buffer), b'{"msg_type":"MO')  # kept for next time

    def test_byte_at_a_time(self):
        reader = FrameReader()
        out = []
        for b in MOVE + b"\n":
            out.extend(reader.feed(bytes([b])))
        self.assertEqual(out, [MOVE])

    def test_empty_lines_ignored(self):
        self.assertEqual(FrameReader().feed(b"\n\n" + MOVE + b"\n\r\n"), [MOVE])

    def test_too_large_without_newline(self):
        reader = FrameReader()
        with self.assertRaises(FrameTooLarge):
            reader.feed(b"x" * (MAX_FRAME + 1))

    def test_too_large_even_with_newline(self):
        with self.assertRaises(FrameTooLarge):
            FrameReader().feed(b"x" * MAX_FRAME + b"\n")

    def test_max_size_frame_accepted(self):
        line = b"x" * (MAX_FRAME - 1)  # 8191 bytes + newline = 8192
        self.assertEqual(FrameReader().feed(line + b"\n"), [line])


class EncodeFrameTests(unittest.TestCase):
    def test_single_trailing_newline_even_with_newline_in_string(self):
        data = encode_frame({"msg_type": "ERROR", "payload": {"detail": "line1\nline2"}})
        self.assertEqual(data.count(b"\n"), 1)
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(json.loads(data)["payload"]["detail"], "line1\nline2")

    def test_compact_separators(self):
        self.assertEqual(encode_frame({"a": 1, "b": [1, 2]}), b'{"a":1,"b":[1,2]}\n')


if __name__ == "__main__":
    unittest.main()
