"""Newline-delimited JSON (NDJSON) framing.

Blueprint §1.1: every message is one compact JSON object followed by a single
b"\\n". The receiver keeps a per-connection byte buffer and splits on b"\\n",
so TCP coalescing (several messages in one recv) and fragmentation (one
message across several recvs) are handled the same way.
"""

import json
import socket

# Blueprint §1: max frame size is 8192 bytes, including the trailing newline.
MAX_FRAME = 8192


class FrameTooLarge(Exception):
    """Raised when a peer sends more than MAX_FRAME bytes without a newline."""


class FrameReader:
    """Accumulates TCP bytes and returns complete frames (Blueprint §1.3).

    The reader only splits bytes. Turning a frame into a dict is the codec's
    job (messages.decode), so one bad frame never affects the frames around it.
    """

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        self.max_frame = max_frame
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes and return every complete frame, without b"\\n".

        Bytes after the last newline stay buffered for the next call.
        Empty lines are ignored (Blueprint §1.1 rule 4).

        Raises:
            FrameTooLarge: a frame (including its newline) exceeds max_frame.
        """
        self.buffer.extend(data)
        frames = []
        while True:
            idx = self.buffer.find(b"\n")
            if idx == -1:
                # No complete frame yet. Refuse to buffer without limit (rule 5).
                if len(self.buffer) > self.max_frame:
                    raise FrameTooLarge(f"{len(self.buffer)} bytes without a newline")
                break
            if idx + 1 > self.max_frame:
                raise FrameTooLarge(f"frame of {idx + 1} bytes")
            line = bytes(self.buffer[:idx])
            del self.buffer[:idx + 1]  # drop the frame and its newline
            if line.strip():
                frames.append(line)
        return frames


def encode_frame(msg: dict) -> bytes:
    """Serialize one message as compact UTF-8 JSON plus b"\\n" (Blueprint §1.1).

    Compact separators mean the JSON itself never contains a raw newline:
    a newline inside a string is escaped as the two characters \\ and n.
    """
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def send_msg(sock: socket.socket, msg: dict) -> None:
    """Send one framed message. Socket errors propagate to the caller."""
    sock.sendall(encode_frame(msg))
