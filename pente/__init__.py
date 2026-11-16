"""Shared code for the networked Pente client and server (CS 457 term project).

Modules:
    framing   NDJSON framing: FrameReader, encode_frame, send_msg (Blueprint §1)
    messages  Message vocabulary, decoding, validation and builders (Blueprint §2-§3)
    game      Pure Pente rules and Points Pente scoring (FSM spec §4)
    fsm       PenteServer: the server's game state machine (FSM spec, concurrency design)
"""
