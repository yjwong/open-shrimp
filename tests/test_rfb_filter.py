"""Tests for the RFB byte-stream filters used on the VZ-host VNC path."""

from __future__ import annotations

import struct

import pytest

from open_shrimp.vnc.rfb_filter import (
    RfbClientFilter,
    RfbFilterError,
    RfbServerFilter,
)


# ---- Builders for RFB client-to-server messages ----


HANDSHAKE = b"RFB 003.008\n" + bytes([1, 1])  # version + sec-type 1 + ClientInit


def set_pixel_format() -> bytes:
    return bytes([0]) + b"\x00\x00\x00" + (b"\x00" * 16)


def set_encodings(encodings: list[int]) -> bytes:
    n = len(encodings)
    body = bytes([2, 0]) + struct.pack("!H", n)
    for enc in encodings:
        body += struct.pack("!i", enc)
    return body


def framebuffer_update_request(
    incremental: int, x: int, y: int, w: int, h: int,
) -> bytes:
    return bytes([3, incremental]) + struct.pack("!HHHH", x, y, w, h)


def key_event(down: int, key: int) -> bytes:
    return bytes([4, down]) + b"\x00\x00" + struct.pack("!I", key)


def pointer_event(buttons: int, x: int, y: int) -> bytes:
    return bytes([5, buttons]) + struct.pack("!HH", x, y)


def client_cut_text(text: bytes) -> bytes:
    return bytes([6]) + b"\x00\x00\x00" + struct.pack("!I", len(text)) + text


# ---- Handshake passthrough ----


class TestHandshake:
    def test_full_handshake_passes_through(self) -> None:
        f = RfbClientFilter()
        assert f.feed(HANDSHAKE) == HANDSHAKE

    def test_handshake_byte_by_byte(self) -> None:
        f = RfbClientFilter()
        out = b""
        for b in HANDSHAKE:
            out += f.feed(bytes([b]))
        assert out == HANDSHAKE

    def test_partial_handshake_buffers(self) -> None:
        f = RfbClientFilter()
        # Only 5 bytes of the 14-byte handshake — should forward those 5
        # and then wait for more.
        out = f.feed(HANDSHAKE[:5])
        assert out == HANDSHAKE[:5]
        # Rest of handshake plus a FramebufferUpdateRequest.
        fur = framebuffer_update_request(1, 0, 0, 100, 100)
        out2 = f.feed(HANDSHAKE[5:] + fur)
        assert out2 == HANDSHAKE[5:] + fur


# ---- Drop semantics for SetPixelFormat / SetEncodings ----


class TestDrop:
    def test_set_pixel_format_dropped(self) -> None:
        f = RfbClientFilter()
        out = f.feed(HANDSHAKE + set_pixel_format())
        assert out == HANDSHAKE

    def test_set_encodings_zero_dropped(self) -> None:
        f = RfbClientFilter()
        out = f.feed(HANDSHAKE + set_encodings([]))
        assert out == HANDSHAKE

    def test_set_encodings_with_encodings_dropped(self) -> None:
        f = RfbClientFilter()
        encodings = [0, 1, 2, -239, -223]  # Raw, CopyRect, RRE, cursor, desktop
        out = f.feed(HANDSHAKE + set_encodings(encodings))
        assert out == HANDSHAKE

    def test_drops_then_forwards(self) -> None:
        f = RfbClientFilter()
        fur = framebuffer_update_request(0, 0, 0, 1920, 1080)
        msg = HANDSHAKE + set_pixel_format() + set_encodings([0, 1, 2]) + fur
        assert f.feed(msg) == HANDSHAKE + fur


# ---- Forward semantics for the rest ----


class TestForward:
    def test_framebuffer_update_request(self) -> None:
        f = RfbClientFilter()
        fur = framebuffer_update_request(1, 100, 200, 300, 400)
        assert f.feed(HANDSHAKE + fur) == HANDSHAKE + fur

    def test_key_event(self) -> None:
        f = RfbClientFilter()
        ke = key_event(1, 0xFF0D)  # XK_Return
        assert f.feed(HANDSHAKE + ke) == HANDSHAKE + ke

    def test_pointer_event(self) -> None:
        f = RfbClientFilter()
        pe = pointer_event(0x01, 50, 75)
        assert f.feed(HANDSHAKE + pe) == HANDSHAKE + pe

    def test_client_cut_text(self) -> None:
        f = RfbClientFilter()
        cct = client_cut_text(b"hello world")
        assert f.feed(HANDSHAKE + cct) == HANDSHAKE + cct

    def test_client_cut_text_empty(self) -> None:
        f = RfbClientFilter()
        cct = client_cut_text(b"")
        assert f.feed(HANDSHAKE + cct) == HANDSHAKE + cct


# ---- Streaming: split at every possible boundary ----


class TestStreaming:
    def _full_session(self) -> tuple[bytes, bytes]:
        """Return (input, expected_output) for a realistic session."""
        msgs = (
            set_pixel_format()                           # dropped
            + set_encodings([0, 1, 2, -239, -223, 16])   # dropped
            + framebuffer_update_request(0, 0, 0, 1920, 1080)
            + framebuffer_update_request(1, 0, 0, 1920, 1080)
            + pointer_event(0, 100, 100)
            + pointer_event(1, 100, 100)
            + key_event(1, 0x61)
            + key_event(0, 0x61)
            + client_cut_text(b"copied text from client")
            + framebuffer_update_request(1, 0, 0, 1920, 1080)
        )
        expected = (
            framebuffer_update_request(0, 0, 0, 1920, 1080)
            + framebuffer_update_request(1, 0, 0, 1920, 1080)
            + pointer_event(0, 100, 100)
            + pointer_event(1, 100, 100)
            + key_event(1, 0x61)
            + key_event(0, 0x61)
            + client_cut_text(b"copied text from client")
            + framebuffer_update_request(1, 0, 0, 1920, 1080)
        )
        return HANDSHAKE + msgs, HANDSHAKE + expected

    def test_one_shot(self) -> None:
        f = RfbClientFilter()
        data, expected = self._full_session()
        assert f.feed(data) == expected

    def test_byte_by_byte(self) -> None:
        f = RfbClientFilter()
        data, expected = self._full_session()
        out = b""
        for b in data:
            out += f.feed(bytes([b]))
        assert out == expected

    @pytest.mark.parametrize("split", [1, 7, 14, 15, 33, 80, 200])
    def test_split_at_boundary(self, split: int) -> None:
        f = RfbClientFilter()
        data, expected = self._full_session()
        first = f.feed(data[:split])
        second = f.feed(data[split:])
        assert first + second == expected

    def test_partial_set_encodings_buffers(self) -> None:
        """SetEncodings header arrives before its body — must wait, not crash."""
        f = RfbClientFilter()
        # Handshake + start of SetEncodings (just the 4-byte header
        # claiming 5 encodings, body still pending).
        encodings = [0, 1, 2, -239, -223]
        msg = set_encodings(encodings)
        out1 = f.feed(HANDSHAKE + msg[:4])
        assert out1 == HANDSHAKE
        # Send the body and a FramebufferUpdateRequest.
        fur = framebuffer_update_request(1, 0, 0, 800, 600)
        out2 = f.feed(msg[4:] + fur)
        assert out2 == fur

    def test_partial_client_cut_text_buffers(self) -> None:
        f = RfbClientFilter()
        cct = client_cut_text(b"some longer text payload")
        # Send only the 8-byte header (length declared but text not yet here).
        out1 = f.feed(HANDSHAKE + cct[:8])
        assert out1 == HANDSHAKE
        out2 = f.feed(cct[8:])
        assert out2 == cct


# ---- Errors ----


class TestErrors:
    def test_unknown_type_raises(self) -> None:
        f = RfbClientFilter()
        f.feed(HANDSHAKE)
        with pytest.raises(RfbFilterError):
            f.feed(bytes([99, 0, 0, 0]))

    def test_unknown_type_after_valid_messages(self) -> None:
        f = RfbClientFilter()
        f.feed(HANDSHAKE + framebuffer_update_request(1, 0, 0, 10, 10))
        with pytest.raises(RfbFilterError):
            f.feed(bytes([200]))


# ---- Recorded noVNC handshake ----


class TestRecordedNoVNC:
    """Mimic the exact byte sequence noVNC sends right after the security
    handshake, then verify only types 0 and 2 leave the filter."""

    def test_novnc_post_handshake_burst(self) -> None:
        # noVNC's first burst after sec-type 1 + ClientInit:
        #   1) SetPixelFormat (32bpp BGRA, true-colour)
        #   2) SetEncodings advertising the standard noVNC default set
        #   3) FramebufferUpdateRequest(non-incremental, full screen)
        # The filter should drop (1) and (2) and forward (3) verbatim.
        spf = bytes([0]) + b"\x00\x00\x00" + bytes([
            32,   # bits-per-pixel
            24,   # depth
            0,    # big-endian
            1,    # true-colour
            0, 255, 0, 255, 0, 255,  # max R/G/B (big-endian uint16)
            16, 8, 0,  # shifts
            0, 0, 0,   # padding
        ])
        # noVNC's default encoding list (from core/rfb.js).
        novnc_encodings = [
            -314,  # ExtendedDesktopSize
            -313,  # Tight
            -224,  # CompressionLevel0
            -223,  # DesktopSize
            -239,  # Cursor
            -240,  # XCursor
            -257,  # JPEG quality 7
            5,     # Hextile
            16,    # ZRLE
            6,     # zlib
            7,     # tight
            0,     # Raw
        ]
        se = set_encodings(novnc_encodings)
        fur = framebuffer_update_request(0, 0, 0, 1280, 720)

        f = RfbClientFilter()
        out = f.feed(HANDSHAKE + spf + se + fur)
        assert out == HANDSHAKE + fur


# ---- Builders for RFB server-to-client messages ----


def server_version() -> bytes:
    return b"RFB 003.008\n"


def server_security_types(types: list[int]) -> bytes:
    return bytes([len(types)]) + bytes(types)


def server_security_result(result: int = 0) -> bytes:
    return struct.pack("!I", result)


def pixel_format(
    bpp: int, depth: int, big_endian: int, true_color: int,
    rmax: int, gmax: int, bmax: int,
    rsh: int, gsh: int, bsh: int,
) -> bytes:
    return struct.pack(
        "!BBBBHHHBBB3x",
        bpp, depth, big_endian, true_color,
        rmax, gmax, bmax, rsh, gsh, bsh,
    )


# Apple-advertised pixel format with the wrong shifts (R↔B swap symptom).
# Real values were not captured but any non-BGRA layout reproduces the
# bug; this one matches what RGBA-on-wire would look like.
APPLE_BAD_PIXEL_FORMAT = pixel_format(32, 24, 0, 1, 255, 255, 255, 0, 8, 16)

# What the filter should rewrite the ServerInit pixel format to: BGRA
# little-endian, matching the bytes ``_VZVNCServer`` actually sends.
BGRA_PIXEL_FORMAT = pixel_format(32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def server_init(
    width: int, height: int, name: bytes, pf: bytes = APPLE_BAD_PIXEL_FORMAT,
) -> bytes:
    return (
        struct.pack("!HH", width, height)
        + pf
        + struct.pack("!I", len(name))
        + name
    )


SERVER_HANDSHAKE = (
    server_version()
    + server_security_types([1])
    + server_security_result(0)
)


# ---- ServerInit pixel-format rewrite ----


class TestServerInitRewrite:
    def test_rewrites_pixel_format_one_shot(self) -> None:
        f = RfbServerFilter()
        si = server_init(1280, 720, b"vz-host", APPLE_BAD_PIXEL_FORMAT)
        out = f.feed(SERVER_HANDSHAKE + si)

        expected = (
            SERVER_HANDSHAKE
            + server_init(1280, 720, b"vz-host", BGRA_PIXEL_FORMAT)
        )
        assert out == expected

    def test_handshake_passes_through_when_pixfmt_already_bgra(self) -> None:
        f = RfbServerFilter()
        si = server_init(800, 600, b"name", BGRA_PIXEL_FORMAT)
        out = f.feed(SERVER_HANDSHAKE + si)
        # Idempotent: rewriting BGRA → BGRA is a no-op.
        assert out == SERVER_HANDSHAKE + si

    def test_post_serverinit_passthrough(self) -> None:
        f = RfbServerFilter()
        si = server_init(100, 100, b"x", APPLE_BAD_PIXEL_FORMAT)
        # 4 bytes of pixel data after ServerInit must pass through untouched
        # (raw FramebufferUpdate would have its own header but for this
        # passthrough check any bytes work).
        post = b"\xde\xad\xbe\xef\x01\x02\x03\x04"
        out = f.feed(SERVER_HANDSHAKE + si + post)
        # Pixel format rewrite happens, but bytes after ServerInit are
        # untouched.
        assert out.endswith(post)

    def test_empty_name(self) -> None:
        f = RfbServerFilter()
        si = server_init(1024, 768, b"", APPLE_BAD_PIXEL_FORMAT)
        out = f.feed(SERVER_HANDSHAKE + si)
        expected = (
            SERVER_HANDSHAKE
            + server_init(1024, 768, b"", BGRA_PIXEL_FORMAT)
        )
        assert out == expected

    def test_byte_by_byte(self) -> None:
        f = RfbServerFilter()
        si = server_init(640, 480, b"vz", APPLE_BAD_PIXEL_FORMAT)
        data = SERVER_HANDSHAKE + si + b"\x00trailing"
        out = b""
        for b in data:
            out += f.feed(bytes([b]))

        expected = (
            SERVER_HANDSHAKE
            + server_init(640, 480, b"vz", BGRA_PIXEL_FORMAT)
            + b"\x00trailing"
        )
        assert out == expected

    @pytest.mark.parametrize("split", [1, 5, 12, 13, 15, 18, 22, 30, 42])
    def test_split_at_boundary(self, split: int) -> None:
        f = RfbServerFilter()
        si = server_init(1280, 720, b"server", APPLE_BAD_PIXEL_FORMAT)
        data = SERVER_HANDSHAKE + si + b"trailing-bytes"
        first = f.feed(data[:split])
        second = f.feed(data[split:])

        expected = (
            SERVER_HANDSHAKE
            + server_init(1280, 720, b"server", BGRA_PIXEL_FORMAT)
            + b"trailing-bytes"
        )
        assert first + second == expected

    def test_multiple_security_types_handled(self) -> None:
        f = RfbServerFilter()
        # Server offers types [1, 2, 30] (None, VNC auth, Apple DH).
        sec = server_security_types([1, 2, 30])
        handshake = server_version() + sec + server_security_result(0)
        si = server_init(640, 480, b"x", APPLE_BAD_PIXEL_FORMAT)
        out = f.feed(handshake + si)

        expected = handshake + server_init(640, 480, b"x", BGRA_PIXEL_FORMAT)
        assert out == expected

    def test_security_count_zero_passthrough(self) -> None:
        """Server refusing the connection (count=0): forward verbatim and
        never reach ServerInit."""
        f = RfbServerFilter()
        # count=0 followed by reason length + reason string.
        reason = b"too many connections"
        refusal = (
            server_version()
            + bytes([0])
            + struct.pack("!I", len(reason))
            + reason
        )
        out = f.feed(refusal)
        assert out == refusal

    def test_serverinit_split_inside_pixel_format(self) -> None:
        """Pixel format struct delivered across two reads — must still be
        rewritten as a unit, not partially."""
        f = RfbServerFilter()
        si = server_init(1280, 720, b"host", APPLE_BAD_PIXEL_FORMAT)
        data = SERVER_HANDSHAKE + si

        # Split inside the pixel format struct (handshake = 17 bytes,
        # ServerInit header starts at 17, pixel format at 17+4=21).
        split = 25  # mid-pixel-format
        first = f.feed(data[:split])
        second = f.feed(data[split:])

        # Header+pixfmt+name-len must wait for the full 24 bytes, so the
        # first chunk forwards only the handshake (17 bytes) and the
        # rewrite happens atomically on the second chunk.
        assert first == SERVER_HANDSHAKE
        expected_si = server_init(1280, 720, b"host", BGRA_PIXEL_FORMAT)
        assert second == expected_si

    def test_idempotent_subsequent_feeds(self) -> None:
        """After ServerInit is consumed the filter is in passthrough mode
        and never re-parses anything."""
        f = RfbServerFilter()
        si = server_init(800, 600, b"x", APPLE_BAD_PIXEL_FORMAT)
        f.feed(SERVER_HANDSHAKE + si)
        # Subsequent bytes — even ones that look like a fresh handshake —
        # are forwarded verbatim, not re-rewritten.
        more = SERVER_HANDSHAKE + si
        assert f.feed(more) == more
