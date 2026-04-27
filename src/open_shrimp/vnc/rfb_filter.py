"""Stateful filter for RFB client-to-server messages.

Apple's private ``_VZVNCServer`` SPI — the host-side VNC server attached
to a ``VZVirtualMachine`` by our patched ``limactl`` — crashes the host
process (SIGTRAP inside ``Base::report_fixme_if_and_trap``) on RFB
``SetEncodings`` (type 2) and resets the connection on ``SetPixelFormat``
(type 0).  Any noVNC / RealVNC / TigerVNC client *will* send both during
its connection setup, so a filter that strips them before they reach the
server is mandatory on the VZ-host VNC path.

Wayvnc and Apple Screen Sharing handle these messages correctly; do not
filter them, or client-driven encoding negotiation gets silently disabled
and pixel quality regresses.

Usage::

    f = RfbClientFilter()
    while data := await ws.receive_bytes():
        out = f.feed(data)
        if out:
            tcp_writer.write(out)

The filter is stateful across :meth:`feed` calls so that messages split
across WebSocket frame boundaries are reassembled correctly.
"""

from __future__ import annotations

import struct

# Drop these — Apple's _VZVNCServer crashes on them.
_DROP_TYPES = frozenset({0, 2})

# Body length (bytes after the 1-byte type prefix) for fixed-size
# client-to-server messages.  See RFB 3.8 §6.4.
_FIXED_LEN: dict[int, int] = {
    0: 19,  # SetPixelFormat: 3 pad + 16 pixel-format
    3: 9,   # FramebufferUpdateRequest: 1 incremental + 4*uint16
    4: 7,   # KeyEvent: 1 down-flag + 2 pad + 4 key
    5: 5,   # PointerEvent: 1 button-mask + 2*uint16
}

# RFB 3.8 client→server handshake bytes the filter forwards verbatim
# before switching into message-demux mode.  Assumes security type 1
# (None) was negotiated, which is what _VZVNCNoSecuritySecurityConfiguration
# offers — the only configuration we ship.
#   12 bytes "RFB 003.008\n"     (ProtocolVersion)
#    1 byte  chosen sec-type     (must be 1)
#    1 byte  shared-flag         (ClientInit)
_HANDSHAKE_LEN = 14


class RfbFilterError(Exception):
    """Raised when the client sends a message the filter can't classify."""


class RfbClientFilter:
    """Stateful demuxer for the client-to-server byte stream of an RFB
    connection going to ``_VZVNCServer``.

    The first :data:`_HANDSHAKE_LEN` bytes (ProtocolVersion + sec-type
    choice + ``ClientInit``) are forwarded byte-identical.  After that,
    each message is parsed by its 1-byte type:

    * Type 0 (``SetPixelFormat``) and type 2 (``SetEncodings``) are
      consumed and dropped.
    * Types 3 (``FramebufferUpdateRequest``), 4 (``KeyEvent``),
      5 (``PointerEvent``) and 6 (``ClientCutText``) are forwarded as
      a single contiguous chunk.
    * Anything else raises :class:`RfbFilterError` — the connection
      should be torn down.

    The filter is intentionally narrow: noVNC, after a server with only
    sec-type 1 advertised and ``SetEncodings`` filtered out, will never
    send the QEMU (250) or extended (255) client messages, because
    those are gated on pseudo-encodings the server never agreed to.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._handshake_emitted = 0

    def feed(self, data: bytes) -> bytes:
        """Feed bytes from the client; return bytes to forward to the server.

        Raises:
            RfbFilterError: if the client sends a message with a type
                byte the filter doesn't recognise.  The caller should
                close the connection.
        """
        self._buf.extend(data)
        out = bytearray()

        if self._handshake_emitted < _HANDSHAKE_LEN:
            need = _HANDSHAKE_LEN - self._handshake_emitted
            take = min(need, len(self._buf))
            out.extend(self._buf[:take])
            del self._buf[:take]
            self._handshake_emitted += take

        while self._handshake_emitted >= _HANDSHAKE_LEN:
            if not self._consume_one(out):
                break

        return bytes(out)

    def _consume_one(self, out: bytearray) -> bool:
        """Consume one complete RFB message from the buffer.

        Returns ``True`` if a message was fully consumed (and appended
        to *out* if it should be forwarded), ``False`` if more bytes are
        needed before the next message can be classified.
        """
        if not self._buf:
            return False
        type_byte = self._buf[0]

        if type_byte in _FIXED_LEN:
            total = 1 + _FIXED_LEN[type_byte]
            if len(self._buf) < total:
                return False
            if type_byte not in _DROP_TYPES:
                out.extend(self._buf[:total])
            del self._buf[:total]
            return True

        if type_byte == 2:  # SetEncodings: 1 type + 1 pad + uint16 N + 4*N
            if len(self._buf) < 4:
                return False
            (n,) = struct.unpack_from("!H", self._buf, 2)
            total = 4 + 4 * n
            if len(self._buf) < total:
                return False
            del self._buf[:total]  # always dropped
            return True

        if type_byte == 6:  # ClientCutText: 1 type + 3 pad + uint32 L + L
            if len(self._buf) < 8:
                return False
            (text_len,) = struct.unpack_from("!I", self._buf, 4)
            total = 8 + text_len
            if len(self._buf) < total:
                return False
            out.extend(self._buf[:total])
            del self._buf[:total]
            return True

        raise RfbFilterError(
            f"unknown RFB client message type {type_byte}"
        )
