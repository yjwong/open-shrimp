"""Unit tests for the pure parts of the HCS RDP session: keyboard maps,
helper-protocol framing, RFB framing, and pixel translation."""

from __future__ import annotations

import struct

import pytest

from open_shrimp.sandbox import hcs_rdp_keymap as km
from open_shrimp.sandbox.hcs_rdp import (
    NATIVE_PIXEL_FORMAT,
    PTR_FLAGS_BUTTON1,
    PTR_FLAGS_BUTTON2,
    PTR_FLAGS_BUTTON3,
    PTR_FLAGS_DOWN,
    PTR_FLAGS_MOVE,
    PTR_FLAGS_WHEEL,
    PTR_FLAGS_WHEEL_NEGATIVE,
    bgrx_to_rgba,
    pack_request,
    parse_set_pixel_format,
    pointer_events,
    rfb_framebuffer_update,
    rfb_server_init,
    translate_pixels,
    unpack_request,
)


# -- keymap -------------------------------------------------------------------


def test_char_to_scancode_letters() -> None:
    assert km.char_to_scancode("a") == (0x1E, False)
    assert km.char_to_scancode("A") == (0x1E, True)
    assert km.char_to_scancode("z") == (0x2C, False)


def test_char_to_scancode_digits_and_symbols() -> None:
    assert km.char_to_scancode("1") == (0x02, False)
    assert km.char_to_scancode("!") == (0x02, True)
    assert km.char_to_scancode("0") == (0x0B, False)
    assert km.char_to_scancode("-") == (0x0C, False)
    assert km.char_to_scancode("_") == (0x0C, True)
    assert km.char_to_scancode(" ") == (0x39, False)
    assert km.char_to_scancode("\n") == (0x1C, False)
    assert km.char_to_scancode('"') == (0x28, True)


def test_char_to_scancode_unmapped() -> None:
    assert km.char_to_scancode("é") is None
    assert km.char_to_scancode("€") is None


def test_keysym_printables_map_to_base_key() -> None:
    # RFB clients send their own Shift press, so both cases map to the
    # same physical key.
    assert km.keysym_to_scancode(ord("a")) == 0x1E
    assert km.keysym_to_scancode(ord("A")) == 0x1E
    assert km.keysym_to_scancode(ord("!")) == 0x02


def test_keysym_specials() -> None:
    assert km.keysym_to_scancode(0xFF0D) == 0x1C          # Return
    assert km.keysym_to_scancode(0xFF1B) == 0x01          # Escape
    assert km.keysym_to_scancode(0xFF51) == 0x4B | km.EXTENDED  # Left
    assert km.keysym_to_scancode(0xFFFF) == 0x53 | km.EXTENDED  # Delete
    assert km.keysym_to_scancode(0xFFE3) == 0x1D          # Control_L
    assert km.keysym_to_scancode(0xFFE4) == 0x1D | km.EXTENDED  # Control_R
    assert km.keysym_to_scancode(0xFFC8) == 0x57          # F11
    assert km.keysym_to_scancode(0xDEAD) is None


def test_combo_single_key() -> None:
    assert km.combo_scancodes("Return") == [(0x1C, True), (0x1C, False)]
    assert km.combo_scancodes("a") == [(0x1E, True), (0x1E, False)]


def test_combo_modifiers() -> None:
    assert km.combo_scancodes("ctrl+a") == [
        (0x1D, True), (0x1E, True), (0x1E, False), (0x1D, False),
    ]
    assert km.combo_scancodes("ctrl+shift+v") == [
        (0x1D, True), (0x2A, True),
        (0x2F, True), (0x2F, False),
        (0x2A, False), (0x1D, False),
    ]


def test_combo_extended_key() -> None:
    assert km.combo_scancodes("alt+Tab") == [
        (0x38, True), (0x0F, True), (0x0F, False), (0x38, False),
    ]
    assert km.combo_scancodes("Left") == [
        (0x4B | km.EXTENDED, True), (0x4B | km.EXTENDED, False),
    ]


def test_combo_unknown_key_raises() -> None:
    with pytest.raises(ValueError):
        km.combo_scancodes("ctrl+nosuchkey")
    with pytest.raises(ValueError):
        km.combo_scancodes("")


def test_type_scancodes_shift_wrapping() -> None:
    assert km.type_scancodes("aA") == [
        (0x1E, True), (0x1E, False),
        (0x2A, True), (0x1E, True), (0x1E, False), (0x2A, False),
    ]


def test_type_scancodes_skips_unmapped() -> None:
    assert km.type_scancodes("é") == []


# -- helper protocol ----------------------------------------------------------


def test_request_roundtrip() -> None:
    data = pack_request(2, 0x1000, 640, 400)
    assert len(data) == 13
    assert unpack_request(data) == (2, 0x1000, 640, 400)


# -- RFB framing --------------------------------------------------------------


def test_server_init_layout() -> None:
    msg = rfb_server_init(1280, 800, "openshrimp-hcs")
    w, h = struct.unpack(">HH", msg[:4])
    assert (w, h) == (1280, 800)
    assert parse_set_pixel_format(msg[4:20]) == NATIVE_PIXEL_FORMAT
    (name_len,) = struct.unpack(">I", msg[20:24])
    assert msg[24 : 24 + name_len] == b"openshrimp-hcs"
    assert len(msg) == 24 + name_len


def test_framebuffer_update_layout() -> None:
    pixels = bytes(4 * 2 * 2)
    msg = rfb_framebuffer_update(2, 2, pixels)
    msg_type, n_rects = struct.unpack(">BxH", msg[:4])
    assert msg_type == 0 and n_rects == 1
    x, y, w, h, enc = struct.unpack(">HHHHi", msg[4:16])
    assert (x, y, w, h, enc) == (0, 0, 2, 2, 0)
    assert msg[16:] == pixels


def test_pixel_format_parse_roundtrip() -> None:
    packed = struct.pack(">BBBBHHHBBB3x", *NATIVE_PIXEL_FORMAT)
    assert parse_set_pixel_format(packed) == NATIVE_PIXEL_FORMAT


# -- pixel translation --------------------------------------------------------

# One red, one green, one blue pixel in BGRX byte order.
_BGRX = bytes([0, 0, 255, 0]) + bytes([0, 255, 0, 0]) + bytes([255, 0, 0, 0])


def test_translate_native_is_identity() -> None:
    assert translate_pixels(_BGRX, NATIVE_PIXEL_FORMAT) is _BGRX


def test_translate_rgbx() -> None:
    # Little-endian shifts R=0 G=8 B=16 → bytes on the wire R, G, B, X.
    fmt = (32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
    out = translate_pixels(_BGRX, fmt)
    assert out[0:3] == bytes([255, 0, 0])
    assert out[4:7] == bytes([0, 255, 0])
    assert out[8:11] == bytes([0, 0, 255])


def test_translate_big_endian() -> None:
    # Big-endian shifts R=16 G=8 B=0 → bytes on the wire X, R, G, B.
    fmt = (32, 24, 1, 1, 255, 255, 255, 16, 8, 0)
    out = translate_pixels(_BGRX, fmt)
    assert out[1:4] == bytes([255, 0, 0])
    assert out[5:8] == bytes([0, 255, 0])
    assert out[9:12] == bytes([0, 0, 255])


def test_translate_rejects_non_32bpp() -> None:
    with pytest.raises(ValueError):
        translate_pixels(b"", (16, 16, 0, 1, 31, 63, 31, 11, 5, 0))
    with pytest.raises(ValueError):
        translate_pixels(b"", (32, 24, 0, 0, 255, 255, 255, 16, 8, 0))


def test_bgrx_to_rgba() -> None:
    out = bgrx_to_rgba(_BGRX)
    assert out[0:4] == bytes([255, 0, 0, 255])
    assert out[4:8] == bytes([0, 255, 0, 255])
    assert out[8:12] == bytes([0, 0, 255, 255])


# -- pointer events -----------------------------------------------------------


def test_pointer_move_only() -> None:
    assert pointer_events(0, 0, 10, 20) == [(PTR_FLAGS_MOVE, 10, 20)]


def test_pointer_left_press_release() -> None:
    press = pointer_events(0, 1, 5, 5)
    assert press == [
        (PTR_FLAGS_MOVE, 5, 5),
        (PTR_FLAGS_DOWN | PTR_FLAGS_BUTTON1, 5, 5),
    ]
    release = pointer_events(1, 0, 5, 5)
    assert release == [(PTR_FLAGS_MOVE, 5, 5), (PTR_FLAGS_BUTTON1, 5, 5)]


def test_pointer_button_mapping() -> None:
    # RFB: bit1 = middle, bit2 = right.
    assert (PTR_FLAGS_DOWN | PTR_FLAGS_BUTTON3, 0, 0) in pointer_events(
        0, 0b010, 0, 0,
    )
    assert (PTR_FLAGS_DOWN | PTR_FLAGS_BUTTON2, 0, 0) in pointer_events(
        0, 0b100, 0, 0,
    )


def test_pointer_wheel() -> None:
    up = pointer_events(0, 0b1000, 3, 4)
    assert (PTR_FLAGS_WHEEL | 0x78, 3, 4) in up
    down = pointer_events(0, 0b10000, 3, 4)
    assert (
        PTR_FLAGS_WHEEL | PTR_FLAGS_WHEEL_NEGATIVE | 0x88, 3, 4,
    ) in down
    # Held wheel bit is not re-fired.
    assert pointer_events(0b1000, 0b1000, 3, 4) == [(PTR_FLAGS_MOVE, 3, 4)]
