"""Tests for tools/dns_pcap_report.py using captures built byte by byte."""

import contextlib
import io
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import dns_pcap_report as dpr  # noqa: E402

CLIENT, R1 = "192.168.10.2", "192.168.10.1"


def dns_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def query(txid: int, name: str, qtype: int = 1) -> bytes:
    return (struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + dns_name(name)
            + struct.pack("!HH", qtype, 1))


def response(txid: int, name: str, qtype: int = 1, ip: str | None = None, rcode: int = 0) -> bytes:
    msg = (struct.pack("!HHHHHH", txid, 0x8580 | rcode, 1, 1 if ip else 0, 0, 0)
           + dns_name(name) + struct.pack("!HH", qtype, 1))
    if ip:
        msg += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 10, 4) + socket.inet_aton(ip)
    return msg


def ethernet_udp(src: str, dst: str, sport: int, dport: int, payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0,
                     socket.inet_aton(src), socket.inet_aton(dst)) + udp
    return b"\x52\x54\x00\x00\x00\x01" + b"\x52\x54\x00\x00\x00\x02" + b"\x08\x00" + ip


def ethernet_icmp() -> bytes:
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 28, 1, 0, 64, 1, 0,
                     socket.inet_aton(CLIENT), socket.inet_aton("192.168.20.100")) + b"\x08" * 8
    return b"\x00" * 12 + b"\x08\x00" + ip


def lookup_frames(answer_ip: str | None = "192.168.20.100", rcode: int = 0) -> list[bytes]:
    """An ICMP packet, then what busybox nslookup sends: A and AAAA queries and replies."""
    return [
        ethernet_icmp(),
        ethernet_udp(CLIENT, R1, 40000, 53, query(0x1a2b, "server.kaanta.edu")),
        ethernet_udp(CLIENT, R1, 40000, 53, query(0x1a2c, "server.kaanta.edu", 28)),
        ethernet_udp(R1, CLIENT, 53, 40000,
                     response(0x1a2b, "server.kaanta.edu", 1, answer_ip, rcode)),
        ethernet_udp(R1, CLIENT, 53, 40000, response(0x1a2c, "server.kaanta.edu", 28)),
    ]


def pcap(frames: list[bytes]) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for i, f in enumerate(frames):
        out += struct.pack("<IIII", 1_790_000_000, i * 1000, len(f), len(f)) + f
    return out


def pcapng(frames: list[bytes]) -> bytes:
    shb = struct.pack("<IIIHHqI", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1, 28)
    idb = struct.pack("<IIHHII", 1, 20, 1, 0, 65535, 20)
    out = shb + idb
    for i, f in enumerate(frames):
        padded = f + b"\0" * (-len(f) % 4)
        ts = 1_790_000_000_000_000 + i * 1000  # microseconds
        blen = 32 + len(padded)
        out += struct.pack("<IIIIIII", 6, blen, 0, ts >> 32, ts & 0xFFFFFFFF, len(f), len(f))
        out += padded + struct.pack("<I", blen)
    return out


class DnsPcapReportTests(unittest.TestCase):
    def run_tool(self, data: bytes, *extra: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.pcap"
            path.write_bytes(data)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = dpr.main([str(path), *extra])
        return code, out.getvalue()

    def test_pcap_lookup_passes_and_is_annotated(self):
        code, out = self.run_tool(pcap(lookup_frames()))
        self.assertEqual(code, 0, out)
        self.assertIn("5 packets read, 4 DNS messages", out)
        self.assertIn("**Frame 2: DNS Standard Query.**", out)  # ICMP was frame 1
        self.assertIn("**Frame 4: DNS Standard Query Response.**", out)
        self.assertIn("0x1a2b", out)
        self.assertIn("A 192.168.20.100", out)
        self.assertIn("AA = authoritative", out)
        self.assertIn("AAAA", out)
        self.assertIn("PASS", out)

    def test_pcapng_lookup_passes(self):
        code, out = self.run_tool(pcapng(lookup_frames()))
        self.assertEqual(code, 0, out)
        self.assertIn("**Frame 4: DNS Standard Query Response.**", out)

    def test_nxdomain_fails(self):
        code, out = self.run_tool(pcap(lookup_frames(answer_ip=None, rcode=3)))
        self.assertEqual(code, 1)
        self.assertIn("NXDOMAIN", out)
        self.assertIn("FAIL", out)

    def test_wrong_address_fails(self):
        code, _ = self.run_tool(pcap(lookup_frames(answer_ip="10.9.9.9")))
        self.assertEqual(code, 1)

    def test_other_name_and_ip_options(self):
        code, _ = self.run_tool(pcap(lookup_frames()), "--name", "server.kaanta.edu.",
                                "--ip", "192.168.20.100")
        self.assertEqual(code, 0)

    def test_not_a_capture(self):
        code, out = self.run_tool(b"this is not a capture file at all")
        self.assertEqual(code, 2)
        self.assertIn("[ERROR]", out)

    def test_compression_loop_is_rejected(self):
        msg = struct.pack("!HHHHHH", 1, 0, 1, 0, 0, 0) + b"\xc0\x0c"
        with self.assertRaises(ValueError):
            dpr.parse_dns(msg)


if __name__ == "__main__":
    unittest.main()
