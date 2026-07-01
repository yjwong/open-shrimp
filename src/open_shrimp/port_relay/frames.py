"""Stream-multiplexing frame codec for the phone port-forward relay.

A single relay WebSocket carries many concurrent TCP streams (a browser
opens several connections to one dev server).  Each binary frame is::

    [type:1][stream_id:4 big-endian][payload:...]

The phone allocates ``stream_id`` per accepted local socket.  The Android
client and this module must agree byte-for-byte on this layout.
"""

from __future__ import annotations

import struct

FRAME_OPEN = 0x10
FRAME_DATA = 0x11
FRAME_CLOSE = 0x12
FRAME_KEEPALIVE = 0x13

HEADER_SIZE = 5
MAX_STREAM_ID = 0xFFFFFFFF
_HEADER = struct.Struct(">BI")


def encode_frame(frame_type: int, stream_id: int, payload: bytes = b"") -> bytes:
    """Encode one relay frame."""
    if not 0 <= frame_type <= 0xFF:
        raise ValueError("frame_type out of range")
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError("stream_id out of range")
    return _HEADER.pack(frame_type, stream_id) + payload


def decode_frame(data: bytes) -> tuple[int, int, bytes]:
    """Decode one relay frame into ``(frame_type, stream_id, payload)``."""
    if len(data) < HEADER_SIZE:
        raise ValueError("frame shorter than header")
    frame_type = data[0]
    stream_id = int.from_bytes(data[1:HEADER_SIZE], "big")
    return frame_type, stream_id, data[HEADER_SIZE:]
