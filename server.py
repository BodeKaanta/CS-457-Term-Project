#!/usr/bin/env python3
"""Networked Pente game server (CS 457 term project).

Usage:
    python3 server.py -h
    python3 server.py -p 5457

Threading model (concurrency_design.md §1-§2): the main thread only accepts
connections. Every accepted socket gets its own daemon handler thread that
reads bytes, splits them into NDJSON frames, and hands each frame to the
PenteServer FSM, which serializes all game logic behind one lock.
"""

import argparse
import itertools
import logging
import socket
import sys
import threading
import time

from pente.fsm import MAX_CONNECTIONS, Connection, PenteServer
from pente.framing import FrameReader, FrameTooLarge, send_msg
from pente.messages import make_error

log = logging.getLogger("pente.server")

# Configuration constants (concurrency_design.md §1.2).
DEFAULT_PORT = 5457
LISTEN_BACKLOG = 5
SOCKET_TIMEOUT_SEC = 5
HANDSHAKE_TIMEOUT_SEC = 10
RECV_SIZE = 4096


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Server CLI (Sprint 3 §2.1): -h for help, -p for the listening port."""
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Networked Pente game server. Listens on 0.0.0.0 (all interfaces) "
                    "and hosts one two-player game room.",
        epilog=f"Example: python3 server.py -p {DEFAULT_PORT}",
    )
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help=f"TCP port to listen on (the project uses {DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also log every message sent (DEBUG level)")
    return parser.parse_args(argv)


def port_number(text: str) -> int:
    """argparse type: an integer port in 1-65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} is not in the range 1-65535")
    return port


def create_listen_socket(port: int) -> socket.socket:
    """Passive open (concurrency_design.md §2.1): bind 0.0.0.0:port and listen."""
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_sock.bind(("0.0.0.0", port))
    listen_sock.listen(LISTEN_BACKLOG)
    # Wake accept() once a second so Ctrl-C and shutdown are noticed on Windows too.
    listen_sock.settimeout(1.0)
    return listen_sock


def configure_socket(sock: socket.socket) -> None:
    """Keepalive and timeouts for every accepted socket (Blueprint §5.3).

    Keepalive detects a silent link loss on an idle connection in about 25 s;
    TCP_USER_TIMEOUT does the same when sent data is never acknowledged.
    Options the OS lacks (for example on Windows) are skipped.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5),
                        ("TCP_KEEPCNT", 3), ("TCP_USER_TIMEOUT", 25000)):
        if hasattr(socket, name):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
            except OSError:
                pass
    # Bounds every sendall() (and so how long the FSM lock can be held), and
    # makes recv() wake up periodically for the handshake deadline (rule R3).
    sock.settimeout(SOCKET_TIMEOUT_SEC)


def client_handler(server: PenteServer, conn: Connection) -> None:
    """Receive loop for one connection (concurrency_design.md §2.5).

    The lock is never held here while waiting in recv() (rule R1); it is only
    taken inside the PenteServer entry points, once per complete frame.
    """
    reader = FrameReader()  # thread-confined: only this thread touches it
    opened_at = time.monotonic()
    try:
        while True:
            # A client that never sends CONNECT/RECONNECT can't hold a thread forever.
            if (not server.is_seated(conn)
                    and time.monotonic() - opened_at > HANDSHAKE_TIMEOUT_SEC):
                log.info("%s holds no seat after %ss; closing", conn, HANDSHAKE_TIMEOUT_SEC)
                break
            try:
                data = conn.sock.recv(RECV_SIZE)
            except TimeoutError as e:
                if e.errno is not None:   # ETIMEDOUT from keepalive: the peer is gone
                    raise
                continue                  # our own 5 s timeout: the player is thinking
            if not data:                  # EOF: the peer sent FIN (Blueprint §5.1)
                server.on_connection_lost(conn, cause="EOF")
                break
            try:
                frames = reader.feed(data)
            except FrameTooLarge:
                server.on_fatal_protocol_error(conn, "FRAME_TOO_LARGE")
                break
            for frame in frames:
                server.on_message(conn, frame)
                if conn.close_requested:  # fatal ERROR sent or DISCONNECT processed
                    break
            if conn.close_requested:
                break
    except OSError as e:  # ConnectionReset/Aborted, BrokenPipe, keepalive TimeoutError
        server.on_connection_lost(conn, cause=f"ABRUPT {e.__class__.__name__}")
    finally:
        # Safety net: a seat must never stay bound to a closed socket. This is a
        # no-op when the loss was already handled or the connection has no seat.
        server.on_connection_lost(conn, cause="CLOSED")
        server.unregister_connection(conn)
        conn.sock.close()  # only the owning thread closes its socket (rule R5)


def serve_forever(server: PenteServer, listen_sock: socket.socket,
                  stop_event: threading.Event) -> None:
    """Accept loop (concurrency_design.md §2.2). Runs on the main thread."""
    conn_ids = itertools.count(1)
    while not stop_event.is_set():
        try:
            sock, addr = listen_sock.accept()  # blocks only this thread
        except TimeoutError:
            continue  # no new client this second; check stop_event
        except OSError:
            if stop_event.is_set():
                break
            raise
        configure_socket(sock)
        conn = Connection(conn_id=next(conn_ids), sock=sock, addr=addr)
        log.info("Accepted %s", conn)
        if not server.register_connection(conn):
            # Over capacity: the only send made without the lock. Safe, because
            # this socket was never registered, so no other thread can reach it.
            log.warning("%s rejected: server at capacity (%d)", conn, MAX_CONNECTIONS)
            try:
                send_msg(sock, make_error("ROOM_FULL", "Server is at capacity."))
            except OSError:
                pass  # a failed reject must never stop the accept loop
            sock.close()
            continue
        threading.Thread(target=client_handler, args=(server, conn),
                         name=f"conn-{conn.conn_id}", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(threadName)-14s] %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        listen_sock = create_listen_socket(args.port)
    except OSError as e:
        log.error("Cannot listen on 0.0.0.0:%d: %s", args.port, e)
        log.error("Is another server already using this port? Try a different -p value.")
        return 1

    log.info("Pente server listening on 0.0.0.0:%d (Ctrl-C to stop)", args.port)
    server = PenteServer()
    stop_event = threading.Event()
    try:
        serve_forever(server, listen_sock, stop_event)
    except KeyboardInterrupt:
        log.info("Ctrl-C received, shutting down")
    finally:
        stop_event.set()
        listen_sock.close()
        server.shutdown_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
