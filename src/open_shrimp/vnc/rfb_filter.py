"""Stateful filters for the RFB byte streams on the VZ-host VNC path.

Apple's private ``_VZVNCServer`` SPI — the host-side VNC server attached
to a ``VZVirtualMachine`` by our patched ``limactl`` — has two bugs that
the proxy must work around byte-by-byte:

1. **Crashes on client requests.**  ``SetEncodings`` (type 2) hits a
   ``Base::report_fixme_if_and_trap`` (SIGTRAP) and ``SetPixelFormat``
   (type 0) resets the TCP connection.  Any noVNC / RealVNC / TigerVNC
   client *will* send both during connection setup, so
   :class:`RfbClientFilter` strips them from the client→server stream.

2. **Lies about its pixel format.**  The framebuffer rectangles on the
   wire are 32-bit little-endian BGRA, but the ``ServerInit`` message
   advertises a pixel format with shifts that don't match.  Because
   ``SetPixelFormat`` is dropped under (1), the client can't renegotiate.
   :class:`RfbServerFilter` rewrites ``ServerInit``'s 16-byte pixel
   format struct on the server→client stream so noVNC interprets the
   raw bytes correctly.  Without the rewrite, R and B channels appear
   swapped (blue Finder icon shows orange).

Wayvnc and Apple Screen Sharing handle the protocol correctly; do not
apply either filter on those paths, or client-driven encoding
negotiation gets silently disabled and pixel quality regresses.

Usage::

    cf = RfbClientFilter()
    sf = RfbServerFilter()
    while data := await ws.receive_bytes():
        out = cf.feed(data)
        if out:
            tcp_writer.write(out)
    # ...separately, on the TCP→WS side:
    while data := await tcp_reader.read(...):
        await ws.send_bytes(sf.feed(data))

Both filters are stateful across :meth:`feed` calls so that messages
split across WebSocket / TCP read boundaries are reassembled correctly.
"""

from __future__ import annotations

import struct
from typing import Literal

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


# Pixel format ``_VZVNCServer`` actually puts on the wire (32-bit
# little-endian BGRA).  Sent in place of the upstream's mismatched
# ``ServerInit`` advertisement so noVNC decodes the bytes correctly.
_BGRA_PIXEL_FORMAT: bytes = struct.pack(
    "!BBBBHHHBBB3x",
    32, 24, 0, 1,            # bpp, depth, big-endian, true-colour
    255, 255, 255,           # max R/G/B
    16, 8, 0,                # shifts R/G/B
)


_ServerState = Literal[
    "version",
    "security_count",
    "security_types",
    "security_result",
    "server_init_header",
    "server_init_name",
    "passthrough",
]


class RfbServerFilter:
    """Stateful filter for the server-to-client byte stream of an RFB
    connection from ``_VZVNCServer``.

    Walks the RFB 3.8 server-side handshake just far enough to find the
    ``ServerInit`` message, rewrites its 16-byte ``PIXEL_FORMAT`` struct
    to match the BGRA byte order the server actually sends, and
    forwards everything else byte-identical.

    Assumes the proxy is in passthrough mode (``credentials is None``):
    the server's full handshake — ``ProtocolVersion``, security types,
    ``SecurityResult``, ``ServerInit`` — flows through the filter.  When
    the proxy intercepts auth instead, the upstream handshake is
    consumed inside ``_authenticate_to_server`` and this filter would
    see only ``ServerInit`` onward; that mode isn't used by Lima/macOS
    today (``_VZVNCNoSecuritySecurityConfiguration``) so it isn't
    handled here.
    """

    _STATE_VERSION: _ServerState = "version"
    _STATE_SECURITY_COUNT: _ServerState = "security_count"
    _STATE_SECURITY_TYPES: _ServerState = "security_types"
    _STATE_SECURITY_RESULT: _ServerState = "security_result"
    _STATE_SERVER_INIT_HEADER: _ServerState = "server_init_header"
    _STATE_SERVER_INIT_NAME: _ServerState = "server_init_name"
    _STATE_PASSTHROUGH: _ServerState = "passthrough"

    def __init__(self) -> None:
        self._buf = bytearray()
        self._state: _ServerState = self._STATE_VERSION
        self._security_types_remaining = 0
        self._name_remaining = 0

    def feed(self, data: bytes) -> bytes:
        """Feed bytes from the server; return bytes to forward to the client.

        Bytes are accumulated until each protocol section can be
        forwarded in full (with ``ServerInit``'s pixel format rewritten);
        partial data is buffered until the next call.
        """
        # Hot path: once past ServerInit, every framebuffer-update chunk
        # flows through here.  Skip the buffer + state machine when there
        # is nothing left to inspect — no copies, no allocation.
        if self._state == self._STATE_PASSTHROUGH and not self._buf:
            return data
        self._buf.extend(data)
        out = bytearray()
        while self._step(out):
            pass
        return bytes(out)

    def _step(self, out: bytearray) -> bool:  # noqa: PLR0911
        """Advance one section if enough bytes are buffered.  Returns
        ``True`` if any progress was made, ``False`` if blocked on more
        input."""
        if self._state == self._STATE_VERSION:
            if len(self._buf) < 12:
                return False
            out.extend(self._buf[:12])
            del self._buf[:12]
            self._state = self._STATE_SECURITY_COUNT
            return True

        if self._state == self._STATE_SECURITY_COUNT:
            if len(self._buf) < 1:
                return False
            count = self._buf[0]
            out.extend(self._buf[:1])
            del self._buf[:1]
            if count == 0:
                # Server refused the connection: a 4-byte reason length
                # plus reason string follows; we have no ServerInit to
                # rewrite so fall through to verbatim forwarding.
                self._state = self._STATE_PASSTHROUGH
            else:
                self._security_types_remaining = count
                self._state = self._STATE_SECURITY_TYPES
            return True

        if self._state == self._STATE_SECURITY_TYPES:
            if not self._buf:
                return False
            take = min(self._security_types_remaining, len(self._buf))
            out.extend(self._buf[:take])
            del self._buf[:take]
            self._security_types_remaining -= take
            if self._security_types_remaining == 0:
                self._state = self._STATE_SECURITY_RESULT
            return True

        if self._state == self._STATE_SECURITY_RESULT:
            if len(self._buf) < 4:
                return False
            out.extend(self._buf[:4])
            del self._buf[:4]
            self._state = self._STATE_SERVER_INIT_HEADER
            return True

        if self._state == self._STATE_SERVER_INIT_HEADER:
            if len(self._buf) < 24:
                return False
            header = bytearray(self._buf[:24])
            header[4:20] = _BGRA_PIXEL_FORMAT
            out.extend(header)
            (self._name_remaining,) = struct.unpack_from(
                "!I", self._buf, 20,
            )
            del self._buf[:24]
            self._state = (
                self._STATE_SERVER_INIT_NAME
                if self._name_remaining > 0
                else self._STATE_PASSTHROUGH
            )
            return True

        if self._state == self._STATE_SERVER_INIT_NAME:
            if not self._buf:
                return False
            take = min(self._name_remaining, len(self._buf))
            out.extend(self._buf[:take])
            del self._buf[:take]
            self._name_remaining -= take
            if self._name_remaining == 0:
                self._state = self._STATE_PASSTHROUGH
            return True

        if self._state == self._STATE_PASSTHROUGH:
            if not self._buf:
                return False
            out.extend(self._buf)
            self._buf.clear()
            return True

        return False
