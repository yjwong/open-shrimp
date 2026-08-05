"""Keyboard mapping for the HCS RDP session: keysyms/characters → PS/2 set-1
scancodes.

The RDP wire protocol carries keyboard input as set-1 scancodes with an
"extended" (E0-prefixed) flag.  Three lookup surfaces feed it:

- :func:`keysym_to_scancode` — X11 keysyms from RFB ``KeyEvent`` messages
  (the noVNC / Mini App path).  RFB clients send their own Shift press for
  shifted characters, so ``A`` and ``a`` both map to the plain letter key.
- :func:`char_to_scancode` — characters typed programmatically via
  ``send_type``; here the shift state must be synthesized, so the result
  carries a ``needs_shift`` flag.  US layout.
- :func:`combo_scancodes` — ``send_key`` strings like ``"ctrl+a"`` or
  ``"Return"``, following the same naming as the libvirt backend's
  QMP combos.

Scancode values are the base set-1 code with :data:`EXTENDED` (0x100) OR'd
in for E0-prefixed keys — the same encoding FreeRDP's
``freerdp_input_send_keyboard_event_ex`` takes.
"""

from __future__ import annotations

EXTENDED = 0x100
"""E0-prefix flag OR'd into a scancode (FreeRDP ``KBDEXT`` convention)."""

SC_LSHIFT = 0x2A
SC_ENTER = 0x1C

#: Unshifted US-layout printables → set-1 scancode.
_PLAIN: dict[str, int] = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "-": 0x0C, "=": 0x0D,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "y": 0x15, "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19,
    "[": 0x1A, "]": 0x1B,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22,
    "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26,
    ";": 0x27, "'": 0x28, "`": 0x29, "\\": 0x2B,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "n": 0x31, "m": 0x32,
    ",": 0x33, ".": 0x34, "/": 0x35,
    " ": 0x39,
}

#: Shifted US-layout printables → the unshifted character on the same key.
_SHIFTED: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=",
    "{": "[", "}": "]",
    ":": ";", '"': "'", "~": "`", "|": "\\",
    "<": ",", ">": ".", "?": "/",
}

#: Non-printable keys by lowercase name → scancode (EXTENDED already OR'd in).
NAMED_KEY_TO_SCANCODE: dict[str, int] = {
    "escape": 0x01,
    "backspace": 0x0E,
    "tab": 0x0F,
    "return": SC_ENTER,
    "enter": SC_ENTER,
    "ctrl": 0x1D,
    "shift": SC_LSHIFT,
    "alt": 0x38,
    "space": 0x39,
    "capslock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F,
    "f6": 0x40, "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44,
    "f11": 0x57, "f12": 0x58,
    "home": 0x47 | EXTENDED,
    "up": 0x48 | EXTENDED,
    "pageup": 0x49 | EXTENDED,
    "left": 0x4B | EXTENDED,
    "right": 0x4D | EXTENDED,
    "end": 0x4F | EXTENDED,
    "down": 0x50 | EXTENDED,
    "pagedown": 0x51 | EXTENDED,
    "insert": 0x52 | EXTENDED,
    "delete": 0x53 | EXTENDED,
    "super": 0x5B | EXTENDED,
    "menu": 0x5D | EXTENDED,
}

#: X11 keysyms outside the Latin-1 printable range → scancode.
_KEYSYM_SPECIAL: dict[int, int] = {
    0xFF08: 0x0E,             # BackSpace
    0xFF09: 0x0F,             # Tab
    0xFF0D: SC_ENTER,         # Return
    0xFF1B: 0x01,             # Escape
    0xFF50: 0x47 | EXTENDED,  # Home
    0xFF51: 0x4B | EXTENDED,  # Left
    0xFF52: 0x48 | EXTENDED,  # Up
    0xFF53: 0x4D | EXTENDED,  # Right
    0xFF54: 0x50 | EXTENDED,  # Down
    0xFF55: 0x49 | EXTENDED,  # Page_Up
    0xFF56: 0x51 | EXTENDED,  # Page_Down
    0xFF57: 0x4F | EXTENDED,  # End
    0xFF63: 0x52 | EXTENDED,  # Insert
    0xFF67: 0x5D | EXTENDED,  # Menu
    0xFF8D: SC_ENTER | EXTENDED,  # KP_Enter
    0xFFBE: 0x3B, 0xFFBF: 0x3C, 0xFFC0: 0x3D, 0xFFC1: 0x3E,  # F1-F4
    0xFFC2: 0x3F, 0xFFC3: 0x40, 0xFFC4: 0x41, 0xFFC5: 0x42,  # F5-F8
    0xFFC6: 0x43, 0xFFC7: 0x44, 0xFFC8: 0x57, 0xFFC9: 0x58,  # F9-F12
    0xFFE1: SC_LSHIFT,        # Shift_L
    0xFFE2: 0x36,             # Shift_R
    0xFFE3: 0x1D,             # Control_L
    0xFFE4: 0x1D | EXTENDED,  # Control_R
    0xFFE5: 0x3A,             # Caps_Lock
    0xFFE9: 0x38,             # Alt_L
    0xFFEA: 0x38 | EXTENDED,  # Alt_R
    0xFFEB: 0x5B | EXTENDED,  # Super_L
    0xFFEC: 0x5C | EXTENDED,  # Super_R
    0xFFFF: 0x53 | EXTENDED,  # Delete
}


def char_to_scancode(ch: str) -> tuple[int, bool] | None:
    """Map a character to ``(scancode, needs_shift)``, or ``None`` if unmapped.

    ``needs_shift`` is ``True`` for characters that require a synthesized
    Shift press on a US layout (uppercase letters, shifted symbols).
    """
    if ch == "\n":
        return SC_ENTER, False
    if ch == "\t":
        return 0x0F, False
    if "a" <= ch <= "z":
        return _PLAIN[ch], False
    if "A" <= ch <= "Z":
        return _PLAIN[ch.lower()], True
    sc = _PLAIN.get(ch)
    if sc is not None:
        return sc, False
    base = _SHIFTED.get(ch)
    if base is not None:
        return _PLAIN[base], True
    return None


def keysym_to_scancode(keysym: int) -> int | None:
    """Map an X11 keysym (RFB ``KeyEvent``) to a scancode, or ``None``.

    Shifted printables map to their base key — the RFB client sends its
    own Shift press/release around them.
    """
    special = _KEYSYM_SPECIAL.get(keysym)
    if special is not None:
        return special
    if 0x20 <= keysym <= 0x7E:
        entry = char_to_scancode(chr(keysym))
        if entry is not None:
            return entry[0]
    return None


def _single_key_scancode(name: str) -> int | None:
    """Resolve one ``send_key`` component (a name or single character)."""
    sc = NAMED_KEY_TO_SCANCODE.get(name.lower())
    if sc is not None:
        return sc
    if len(name) == 1:
        entry = char_to_scancode(name)
        if entry is not None:
            sc, needs_shift = entry
            # A bare shifted character in a combo cannot carry its own
            # Shift; the caller's modifiers decide the shift state.
            return sc
    return None


def combo_scancodes(key_str: str) -> list[tuple[int, bool]]:
    """Expand ``"ctrl+shift+v"``-style input into ``(scancode, down)`` events.

    Modifiers are pressed in order, the final key is pressed and released,
    then modifiers are released in reverse order.  A single key (no ``+``)
    is a plain press+release.  Raises :class:`ValueError` for unknown keys.
    """
    parts = [p for p in key_str.split("+") if p] if key_str != "+" else ["+"]
    if not parts:
        raise ValueError(f"empty key string: {key_str!r}")

    *modifiers, key = parts
    key_sc = _single_key_scancode(key)
    if key_sc is None:
        raise ValueError(f"unknown key {key!r} in {key_str!r}")
    mod_scs = []
    for mod in modifiers:
        sc = _single_key_scancode(mod)
        if sc is None:
            raise ValueError(f"unknown modifier {mod!r} in {key_str!r}")
        mod_scs.append(sc)

    events: list[tuple[int, bool]] = [(sc, True) for sc in mod_scs]
    events.append((key_sc, True))
    events.append((key_sc, False))
    events.extend((sc, False) for sc in reversed(mod_scs))
    return events


def type_scancodes(text: str) -> list[tuple[int, bool]]:
    """Expand *text* into ``(scancode, down)`` events, synthesizing Shift.

    Unmapped characters are skipped (mirrors the libvirt backend's typing
    behaviour).
    """
    events: list[tuple[int, bool]] = []
    for ch in text:
        entry = char_to_scancode(ch)
        if entry is None:
            continue
        sc, needs_shift = entry
        if needs_shift:
            events.append((SC_LSHIFT, True))
        events.append((sc, True))
        events.append((sc, False))
        if needs_shift:
            events.append((SC_LSHIFT, False))
    return events
