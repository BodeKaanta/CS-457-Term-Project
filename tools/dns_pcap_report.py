#!/usr/bin/env python3
"""Summarize and annotate the DNS packets in a capture (Sprint 4 deliverable 2).

Reads a .pcap or .pcapng file (for example sprint_4_dns_lookup.pcap), finds
every DNS message over UDP port 53, pairs each query with its response, and
prints a Markdown table plus a plain-English annotation of the exchange for
the network report. It also checks that the capture shows the required
lookup: a query for the server name answered with the expected address.
With --annotate it also writes a copy of the capture with a Wireshark
comment on every DNS packet (pcapng format; see write_pcapng).

Usage (from the repository root):
    python tools/dns_pcap_report.py sprint_4_dns_lookup.pcap
    python tools/dns_pcap_report.py cml_capture.pcap --annotate sprint_4_dns_lookup.pcap
    python tools/dns_pcap_report.py capture.pcapng --name server.kaanta.edu --ip 192.168.20.100

Exit status: 0 if the required query and answer were found, 1 if not, 2 if
the file could not be read. Frame numbers match Wireshark's "No." column.
Standard library only.
"""

import argparse
import socket
import struct
import sys
from dataclasses import dataclass, field

DNS_PORT = 53
RR_TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT",
            28: "AAAA", 33: "SRV", 255: "ANY"}
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}

# Link-layer header types (https://www.tcpdump.org/linktypes.html).
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LINUX_SLL = 113
LINKTYPE_IPV4 = 228
LINKTYPE_LINUX_SLL2 = 276


class CaptureError(Exception):
    """The file is not a capture this tool can read."""


# ---------------------------------------------------------------------------
# Capture file readers
# ---------------------------------------------------------------------------

@dataclass
class Packet:
    number: int          # 1-based, like Wireshark's "No." column
    ts_ns: int           # capture time, nanoseconds since the epoch (exact)
    linktype: int
    data: bytes
    orig_len: int = 0    # length on the wire (data may be truncated by the snaplen)
    comments: list[str] = field(default_factory=list)  # pcapng packet comments

    @property
    def time(self) -> float:
        """Capture time in seconds since the epoch."""
        return self.ts_ns / 1e9


def read_capture(path: str) -> list[Packet]:
    with open(path, "rb") as f:
        return read_capture_bytes(f.read())


def read_capture_bytes(raw: bytes) -> list[Packet]:
    """Parse a whole pcap or pcapng file that is already in memory."""
    if len(raw) < 24:
        raise CaptureError("file is too short to be a capture")
    if raw[:4] == b"\x0a\x0d\x0d\x0a":
        return _read_pcapng(raw)
    return _read_pcap(raw)


def _read_pcap(raw: bytes) -> list[Packet]:
    magics = {b"\xd4\xc3\xb2\xa1": ("<", 1e-6), b"\xa1\xb2\xc3\xd4": (">", 1e-6),
              b"\x4d\x3c\xb2\xa1": ("<", 1e-9), b"\xa1\xb2\x3c\x4d": (">", 1e-9)}
    if raw[:4] not in magics:
        raise CaptureError("not a pcap or pcapng file (unknown magic number)")
    endian, tick = magics[raw[:4]]
    ns_per_frac = 1000 if tick == 1e-6 else 1
    linktype = struct.unpack(endian + "I", raw[20:24])[0] & 0x0FFFFFFF
    packets, offset = [], 24
    while offset + 16 <= len(raw):
        sec, frac, caplen, orig = struct.unpack(endian + "IIII", raw[offset:offset + 16])
        offset += 16
        packets.append(Packet(len(packets) + 1, sec * 10**9 + frac * ns_per_frac, linktype,
                              raw[offset:offset + caplen], orig))
        offset += caplen
    return packets


def _read_pcapng(raw: bytes) -> list[Packet]:
    packets: list[Packet] = []
    interfaces: list[tuple[int, tuple[int, int]]] = []  # (linktype, if_tsresol base/exponent)
    endian, offset = "<", 0
    while offset + 12 <= len(raw):
        if raw[offset:offset + 4] == b"\x0a\x0d\x0d\x0a":  # Section Header Block
            endian = "<" if raw[offset + 8:offset + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            interfaces = []
        btype, blen = struct.unpack(endian + "II", raw[offset:offset + 8])
        if blen < 12 or offset + blen > len(raw):
            break
        body = raw[offset + 8:offset + blen - 4]
        if btype == 1:  # Interface Description Block
            linktype = struct.unpack(endian + "H", body[0:2])[0]
            interfaces.append((linktype, _if_tsresol(body[8:], endian)))
        elif btype in (6, 2):  # Enhanced Packet Block / obsolete Packet Block
            if btype == 6:
                iface, ts_hi, ts_lo, caplen, orig = struct.unpack(endian + "IIIII", body[0:20])
            else:
                iface, _drops, ts_hi, ts_lo, caplen, orig = struct.unpack(endian + "HHIIII",
                                                                         body[0:20])
            linktype, resol = interfaces[iface] if iface < len(interfaces) else (1, (10, 6))
            options = body[20 + caplen + (-caplen % 4):]
            packets.append(Packet(len(packets) + 1, _to_ns((ts_hi << 32) | ts_lo, resol),
                                  linktype, body[20:20 + caplen], orig,
                                  _option_strings(options, endian, 1)))
        elif btype == 3:  # Simple Packet Block (no timestamp)
            linktype = interfaces[0][0] if interfaces else 1
            orig = struct.unpack(endian + "I", body[0:4])[0]
            packets.append(Packet(len(packets) + 1, 0, linktype, body[4:4 + orig], orig))
        offset += blen
    return packets


def _to_ns(ticks: int, resol: tuple[int, int]) -> int:
    """A pcapng timestamp in units of base**-exponent seconds, as whole nanoseconds."""
    base, exp = resol
    if base == 2:
        return (ticks * 10**9) >> exp
    return ticks * 10**(9 - exp) if exp <= 9 else ticks // 10**(exp - 9)


def _option_strings(options: bytes, endian: str, wanted: int) -> list[str]:
    """The UTF-8 values of every option with code `wanted` (1 = comment)."""
    found, i = [], 0
    while i + 4 <= len(options):
        code, length = struct.unpack(endian + "HH", options[i:i + 4])
        if code == 0:
            break
        if code == wanted:
            found.append(options[i + 4:i + 4 + length].decode("utf-8", "replace"))
        i += 4 + length + (-length % 4)
    return found


def _if_tsresol(options: bytes, endian: str) -> tuple[int, int]:
    """Timestamp unit from the IDB if_tsresol option, as (base, exponent): units of
    base**-exponent seconds. The default is microseconds, (10, 6)."""
    i = 0
    while i + 4 <= len(options):
        code, length = struct.unpack(endian + "HH", options[i:i + 4])
        if code == 0:
            break
        if code == 9 and length >= 1:
            v = options[i + 4]
            return (2, v & 0x7F) if v & 0x80 else (10, v)
        i += 4 + length + (-length % 4)
    return (10, 6)


# ---------------------------------------------------------------------------
# Ethernet / IPv4 / UDP
# ---------------------------------------------------------------------------

@dataclass
class Datagram:
    src: str
    dst: str
    sport: int
    dport: int
    payload: bytes


def udp_datagram(pkt: Packet) -> Datagram | None:
    """Return the UDP datagram inside an IPv4 packet, or None."""
    data, lt = pkt.data, pkt.linktype
    if lt == LINKTYPE_ETHERNET:
        if len(data) < 14:
            return None
        ethertype, off = struct.unpack("!H", data[12:14])[0], 14
        while ethertype in (0x8100, 0x88A8) and len(data) >= off + 4:  # VLAN tags
            ethertype, off = struct.unpack("!H", data[off + 2:off + 4])[0], off + 4
        if ethertype != 0x0800:
            return None
        ip = data[off:]
    elif lt in (LINKTYPE_RAW, LINKTYPE_IPV4):
        ip = data
    elif lt == LINKTYPE_LINUX_SLL:
        if len(data) < 16 or struct.unpack("!H", data[14:16])[0] != 0x0800:
            return None
        ip = data[16:]
    elif lt == LINKTYPE_LINUX_SLL2:
        if len(data) < 20 or struct.unpack("!H", data[0:2])[0] != 0x0800:
            return None
        ip = data[20:]
    else:
        return None

    if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 17:  # IPv4 and UDP only
        return None
    ihl = (ip[0] & 0x0F) * 4
    frag = struct.unpack("!H", ip[6:8])[0]
    if frag & 0x1FFF:  # a later fragment: no UDP header
        return None
    udp = ip[ihl:]
    if len(udp) < 8:
        return None
    sport, dport, ulen = struct.unpack("!HHH", udp[0:6])
    return Datagram(socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20]),
                    sport, dport, udp[8:max(8, ulen)])


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

@dataclass
class DnsMessage:
    txid: int
    is_response: bool
    opcode: int
    aa: bool
    tc: bool
    rd: bool
    ra: bool
    rcode: int
    questions: list = field(default_factory=list)  # (name, type)
    answers: list = field(default_factory=list)    # (name, type, ttl, value)

    def flags_text(self) -> str:
        names = [n for n, on in (("QR", self.is_response), ("AA", self.aa), ("TC", self.tc),
                                 ("RD", self.rd), ("RA", self.ra)) if on]
        return " ".join(names) or "-"


def _read_name(msg: bytes, offset: int) -> tuple[str, int]:
    """Decode a possibly compressed domain name. Returns (name, offset after it)."""
    labels, jumped, end, hops = [], False, offset, 0
    while True:
        if offset >= len(msg):
            raise ValueError("name runs past the end of the message")
        length = msg[offset]
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(msg):
                raise ValueError("truncated compression pointer")
            if not jumped:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | msg[offset + 1]
            jumped, hops = True, hops + 1
            if hops > 50:
                raise ValueError("compression pointer loop")
            continue
        if length == 0:
            return ".".join(labels), (end if jumped else offset + 1)
        labels.append(msg[offset + 1:offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length


def parse_dns(msg: bytes) -> DnsMessage:
    if len(msg) < 12:
        raise ValueError("shorter than a DNS header")
    txid, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", msg[:12])
    dns = DnsMessage(txid=txid, is_response=bool(flags & 0x8000), opcode=(flags >> 11) & 0xF,
                     aa=bool(flags & 0x0400), tc=bool(flags & 0x0200), rd=bool(flags & 0x0100),
                     ra=bool(flags & 0x0080), rcode=flags & 0xF)
    off = 12
    for _ in range(qd):
        name, off = _read_name(msg, off)
        qtype = struct.unpack("!H", msg[off:off + 2])[0]
        off += 4
        dns.questions.append((name, RR_TYPES.get(qtype, str(qtype))))
    for _ in range(an):
        name, off = _read_name(msg, off)
        rtype, _cls, ttl, rdlen = struct.unpack("!HHIH", msg[off:off + 10])
        off += 10
        rdata = msg[off:off + rdlen]
        if rtype == 1 and rdlen == 4:
            value = socket.inet_ntoa(rdata)
        elif rtype == 28 and rdlen == 16:
            value = socket.inet_ntop(socket.AF_INET6, rdata)
        elif rtype in (2, 5, 12):
            value = _read_name(msg, off)[0]
        else:
            value = rdata.hex()
        dns.answers.append((name, RR_TYPES.get(rtype, str(rtype)), ttl, value))
        off += rdlen
    return dns


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class DnsPacket:
    packet: Packet
    dgram: Datagram
    dns: DnsMessage


def find_dns(packets: list[Packet]) -> list[DnsPacket]:
    found = []
    for pkt in packets:
        d = udp_datagram(pkt)
        if d is None or DNS_PORT not in (d.sport, d.dport):
            continue
        try:
            found.append(DnsPacket(pkt, d, parse_dns(d.payload)))
        except (ValueError, struct.error, IndexError):
            continue
    return found


def match_response(query: DnsPacket, items: list[DnsPacket]) -> DnsPacket | None:
    """The response with the same transaction ID, sent back to the querier's port."""
    for r in items:
        if (r.dns.is_response and r.dns.txid == query.dns.txid
                and r.packet.number > query.packet.number
                and r.dgram.dst == query.dgram.src and r.dgram.dport == query.dgram.sport):
            return r
    return None


def describe_answer(dns: DnsMessage) -> str:
    if not dns.is_response:
        return ""
    if dns.answers:
        return ", ".join(f"{t} {v}" for _, t, _, v in dns.answers)
    return RCODES.get(dns.rcode, f"rcode {dns.rcode}") if dns.rcode else "(no answer)"


def find_required(items: list[DnsPacket], name: str,
                  ip: str) -> tuple[DnsPacket, DnsPacket, int] | None:
    """The required exchange: an A query for `name` and the response that answers `ip`.

    Returns (query, response, TTL of the answer), or None if the capture lacks it.
    """
    wanted = name.rstrip(".").lower()
    for query in items:
        if query.dns.is_response or (wanted, "A") not in [
                (n.lower(), t) for n, t in query.dns.questions]:
            continue
        resp = match_response(query, items)
        if resp is None:
            continue
        for _, rtype, ttl, value in resp.dns.answers:
            if rtype == "A" and value == ip:
                return query, resp, ttl
    return None


def render(path: str, items: list[DnsPacket], name: str, ip: str) -> tuple[str, bool]:
    lines = [f"DNS messages in `{path}` (UDP port {DNS_PORT}):", "",
             "| No. | Time (s) | Source | Destination | Type | Transaction ID | Question | "
             "Answer | Flags |",
             "|---|---|---|---|---|---|---|---|---|"]
    t0 = items[0].packet.time if items else 0.0
    for it in items:
        q = ", ".join(f"{n} {t}" for n, t in it.dns.questions)
        kind = "Standard query response" if it.dns.is_response else "Standard query"
        lines.append(
            f"| {it.packet.number} | {it.packet.time - t0:.6f} | {it.dgram.src}:{it.dgram.sport}"
            f" | {it.dgram.dst}:{it.dgram.dport} | {kind} | 0x{it.dns.txid:04x} | {q} | "
            f"{describe_answer(it.dns)} | {it.dns.flags_text()} |")

    wanted = name.rstrip(".").lower()
    found = find_required(items, name, ip)
    passed = found is not None
    if found:
        q, r, ttl = found
        lines += [
            "", "Annotation of the required lookup:", "",
            f"- **Frame {q.packet.number}: DNS Standard Query.** {q.dgram.src} (the client) "
            f"sends a UDP datagram from port {q.dgram.sport} to {q.dgram.dst} port "
            f"{q.dgram.dport}, the DNS server on R1 that DHCP gave the client. It asks for "
            f"the **A record** (IPv4 address) of `{wanted}`. Transaction ID "
            f"0x{q.dns.txid:04x}; flags: {q.dns.flags_text()} (RD = recursion desired).",
            f"- **Frame {r.packet.number}: DNS Standard Query Response.** {r.dgram.src} port "
            f"{r.dgram.sport} answers back to {r.dgram.dst} port {r.dgram.dport} "
            f"{(r.packet.time - q.packet.time) * 1000:.1f} ms later with the same transaction "
            f"ID 0x{r.dns.txid:04x}, which is how the client matches it to its query. Flags: "
            f"{r.dns.flags_text()} (QR = this is a response"
            + (", AA = authoritative answer from R1's `ip host` table" if r.dns.aa else "")
            + f"). Reply code {RCODES.get(r.dns.rcode, r.dns.rcode)}. Answer: "
            f"**`{wanted}` A {ip}** (TTL {ttl} s).",
        ]

    others = [it for it in items if not it.dns.is_response
              and (wanted, "A") not in [(n.lower(), t) for n, t in it.dns.questions]]
    if passed and others:
        kinds = sorted({t for it in others for _, t in it.dns.questions})
        lines.append(f"- Other queries in the capture ({', '.join(kinds)}) are normal: "
                     "nslookup also asks for an IPv6 (AAAA) address, and some versions "
                     "look up the DNS server's own name (PTR).")
    lines += ["", ("PASS" if passed else "FAIL") + f": query for `{wanted}` answered with "
              f"{ip} {'found' if passed else 'NOT found'}."]
    return "\n".join(lines), passed


# ---------------------------------------------------------------------------
# Annotated copy (Wireshark packet comments)
# ---------------------------------------------------------------------------

def packet_comments(items: list[DnsPacket], found: tuple[DnsPacket, DnsPacket, int],
                    name: str, ip: str) -> dict[int, str]:
    """Wireshark packet comments, keyed by frame number, for every DNS packet."""
    q, r, ttl = found
    wanted = name.rstrip(".").lower()
    comments = {
        q.packet.number:
            f"DNS STANDARD QUERY (client -> R1). {q.dgram.src} (bk-Client1, address from "
            f"DHCP) asks the DNS server it learned from DHCP option 6, {q.dgram.dst} (R1), "
            f"for the A record of {wanted}. UDP {q.dgram.sport} -> {q.dgram.dport} (DNS). "
            f"Transaction ID 0x{q.dns.txid:04x}. Flags: {q.dns.flags_text()} "
            "(RD = recursion desired).",
        r.packet.number:
            f"DNS STANDARD QUERY RESPONSE (R1 -> client). R1 answers {wanted} A {ip} "
            f"(TTL {ttl} s): the game server's address. UDP {r.dgram.sport} (DNS) -> "
            f"{r.dgram.dport}, same transaction ID 0x{r.dns.txid:04x} as frame "
            f"{q.packet.number}, so the client matches it to that query. Flags: "
            f"{r.dns.flags_text()} (QR = response"
            + (", AA = authoritative: from R1's 'ip host' table" if r.dns.aa else "")
            + f"). Reply code {RCODES.get(r.dns.rcode, r.dns.rcode)}.",
    }
    for it in items:
        if it.packet.number in comments:
            continue
        kinds = ", ".join(t for _, t in it.dns.questions) or "?"
        if it.dns.is_response:
            text = (f"DNS response to the {kinds} query (transaction ID 0x{it.dns.txid:04x}): "
                    f"{describe_answer(it.dns)}. Not part of the required lookup.")
        else:
            text = (f"DNS {kinds} query (transaction ID 0x{it.dns.txid:04x}). nslookup also "
                    "asks for this; R1 has no such record, so it is not part of the "
                    "required lookup.")
        comments[it.packet.number] = text
    return comments


def _option(code: int, value: bytes) -> bytes:
    return struct.pack("<HH", code, len(value)) + value + bytes(-len(value) % 4)


def _block(btype: int, body: bytes) -> bytes:
    length = 12 + len(body)
    return struct.pack("<II", btype, length) + body + struct.pack("<I", length)


def write_pcapng(path: str, packets: list[Packet], comments: dict[int, str],
                 file_comment: str) -> None:
    """Write the packets as pcapng with comments. Timestamps and bytes are unchanged.

    Comments need pcapng: the classic pcap format has no place to store them.
    """
    end = struct.pack("<HH", 0, 0)
    out = [_block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
                  + _option(1, file_comment.encode("utf-8"))
                  + _option(4, b"tools/dns_pcap_report.py --annotate") + end)]
    iface_of: dict[int, int] = {}
    for pkt in packets:
        if pkt.linktype not in iface_of:
            iface_of[pkt.linktype] = len(iface_of)
            out.append(_block(1, struct.pack("<HHI", pkt.linktype, 0, 0)
                              + _option(9, bytes([9])) + end))  # nanosecond timestamps
    for pkt in packets:
        texts = list(pkt.comments)                  # keep comments already in the file
        if pkt.number in comments and comments[pkt.number] not in texts:
            texts.append(comments[pkt.number])     # (annotating twice adds nothing)
        options = b"".join(_option(1, c.encode("utf-8")) for c in texts)
        data = pkt.data + bytes(-len(pkt.data) % 4)
        out.append(_block(6, struct.pack("<IIIII", iface_of[pkt.linktype], pkt.ts_ns >> 32,
                                         pkt.ts_ns & 0xFFFFFFFF, len(pkt.data),
                                         max(pkt.orig_len, len(pkt.data)))
                          + data + (options + end if options else b"")))
    with open(path, "wb") as f:
        f.write(b"".join(out))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", help=".pcap or .pcapng file")
    parser.add_argument("--name", default="server.kaanta.edu", help="name that must be queried")
    parser.add_argument("--ip", default="192.168.20.100", help="address the answer must contain")
    parser.add_argument("--annotate", metavar="OUT",
                        help="also write a copy with a Wireshark comment on every DNS packet "
                             "(pcapng format, which is needed to store comments)")
    args = parser.parse_args(argv)
    try:
        packets = read_capture(args.capture)
    except (OSError, CaptureError, struct.error) as e:
        print(f"[ERROR] Cannot read {args.capture}: {e}")
        return 2
    items = find_dns(packets)
    text, passed = render(args.capture, items, args.name, args.ip)
    print(f"{len(packets)} packets read, {len(items)} DNS messages.\n")
    print(text)
    if args.annotate:
        found = find_required(items, args.name, args.ip)
        if found is None:
            print(f"\n[ERROR] Not writing {args.annotate}: the required lookup is missing.")
        else:
            comments = packet_comments(items, found, args.name, args.ip)
            q, r, _ = found
            file_comment = (
                "CS 457 Sprint 4 DNS capture, taken on the link between bk-R1 Gi0/1 and "
                f"bk-Client1 eth0 while bk-Client1 ran 'nslookup {args.name.rstrip('.')}'. "
                f"Frame {q.packet.number} is the query and frame {r.packet.number} is R1's "
                f"answer, {args.ip}. Every DNS packet has a packet comment.")
            write_pcapng(args.annotate, packets, comments, file_comment)
            print(f"\nWrote {args.annotate}: {len(packets)} packets, {len(comments)} packet "
                  "comments (pcapng format, so Wireshark keeps the comments).")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
