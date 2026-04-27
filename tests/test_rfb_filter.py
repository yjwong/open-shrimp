"""Tests for the RFB client→server filter used on the VZ-host VNC path."""

from __future__ import annotations

import struct

import pytest

from open_shrimp.vnc.rfb_filter import (
    RfbClientFilter,
    RfbFilterError,
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
