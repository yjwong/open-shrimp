"""Host-side RDP session for HCS computer-use: frames, input, RFB front.

The HCS guest runs a weston desktop that serves RDP over an in-guest vsock
relay.  This module owns the host half: one persistent FreeRDP connection
per computer-use sandbox, fanned out to every ``Sandbox`` computer-use
member.

Split of responsibilities:

- ``hcs_rdp_helper.c`` (shipped prebuilt with its FreeRDP DLLs, or compiled
  once with MSYS2 mingw64 gcc against libfreerdp3 — see
  :func:`ensure_rdp_helper`) owns the RDP wire session:
  it dials ``AF_HYPERV`` to the guest's vsock RDP relay through an
  in-process TCP shim, decodes frames into a memory framebuffer, injects
  input PDUs, and reconnects when the RDP session drops.  It exposes a
  tiny binary control protocol on a loopback TCP port.  Keeping the
  FreeRDP dependency in a small C program avoids binding Python to
  libfreerdp's private struct layouts, which are not a stable ABI.
- :class:`HcsRdpSession` (this module) implements the ``Sandbox``
  computer-use members on top of the helper: screenshots (latest decoded
  frame → PNG — the RDP stream is damage-driven, so a fresh paint must
  never be awaited), input mapping (keysym/char → set-1 scancodes,
  pointer/wheel flags), the clipboard via ``wl-copy``/``wl-paste`` over
  the guest exec channel, and a minimal RFB 3.8 server front on loopback
  so the existing noVNC proxy, VNC Mini App, and
  :func:`open_shrimp.vnc.rfb_snapshot.capture_to_png` work unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_data_path

from open_shrimp.sandbox.hcs_rdp_keymap import (
    combo_scancodes,
    keysym_to_scancode,
    type_scancodes,
)
from open_shrimp.vnc.rfb_snapshot import encode_png

logger = logging.getLogger(__name__)

# -- RDP fastpath pointer flags (MS-RDPBCGR TS_POINTER_EVENT) ----------------

PTR_FLAGS_HWHEEL = 0x0400
PTR_FLAGS_WHEEL = 0x0200
PTR_FLAGS_WHEEL_NEGATIVE = 0x0100
PTR_FLAGS_MOVE = 0x0800
PTR_FLAGS_DOWN = 0x8000
PTR_FLAGS_BUTTON1 = 0x1000  # left
PTR_FLAGS_BUTTON2 = 0x2000  # right
PTR_FLAGS_BUTTON3 = 0x4000  # middle

#: One wheel detent, in the units the wheel-rotation field carries.
_WHEEL_DELTA = 0x78

_BUTTON_FLAGS = {
    "left": PTR_FLAGS_BUTTON1,
    "right": PTR_FLAGS_BUTTON2,
    "middle": PTR_FLAGS_BUTTON3,
}

# -- Helper control protocol -------------------------------------------------
#
# Requests are a fixed 13 bytes: u8 opcode + three u32 little-endian args.
# Every response starts with a u8 status (1 = ok); the payload depends on
# the opcode.  GETFRAME returns the frame only when its sequence number
# differs from the caller-supplied one, so pollers pay for pixels only on
# damage.

OP_GETFRAME = 1
OP_MOUSE = 2
OP_KEY = 3
OP_STATUS = 5
OP_QUIT = 6

_REQ = struct.Struct("<BIII")
_FRAME_HDR = struct.Struct("<BBIHHI")   # ok, connected, seq, w, h, payload len
_STATUS_HDR = struct.Struct("<BBIHH")   # ok, connected, seq, w, h


def pack_request(op: int, a: int = 0, b: int = 0, c: int = 0) -> bytes:
    return _REQ.pack(op, a, b, c)


def unpack_request(data: bytes) -> tuple[int, int, int, int]:
    return _REQ.unpack(data)


class HelperError(RuntimeError):
    """Raised when the native RDP helper misbehaves or is unreachable."""


class HelperClient:
    """Owns the helper subprocess and its control connection.

    Thread-safe: the RFB front and the sandbox protocol members call in
    from different threads; one lock serializes control-channel
    request/response exchanges.
    """

    def __init__(
        self,
        helper_exe: Path,
        target: str,
        width: int,
        height: int,
        *,
        dll_dir: Path | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self._helper_exe = helper_exe
        self._target = target
        self._width = width
        self._height = height
        self._dll_dir = dll_dir
        self._connect_timeout = connect_timeout
        self._proc: subprocess.Popen[str] | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    # -- lifecycle --

    def start(self) -> None:
        """Spawn the helper, connect its control port, and wait for the RDP
        session to come up."""
        with self._lock:
            self._start_locked()
        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            connected, _seq, _w, _h = self.status()
            if connected:
                return
            time.sleep(0.25)
        raise HelperError(
            f"RDP helper never connected within {self._connect_timeout}s"
        )

    def _start_locked(self) -> None:
        import os

        self.close_locked()
        env = None
        if self._dll_dir is not None:
            # The helper links libfreerdp3; its DLLs live next to the
            # toolchain, not the exe.  Without them on PATH the process
            # dies in the loader before printing anything.
            env = dict(os.environ)
            env["PATH"] = f"{self._dll_dir}{os.pathsep}{env.get('PATH', '')}"
        self._proc = subprocess.Popen(
            [
                str(self._helper_exe),
                self._target,
                f"{self._width}x{self._height}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert self._proc.stdout is not None
        port: int | None = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            logger.debug("rdp helper: %s", line)
            if line.startswith("HELPER-CONTROL-PORT "):
                port = int(line.split()[1])
                break
        if port is None:
            rc = self._proc.poll()
            raise HelperError(
                f"RDP helper did not announce its control port (exit={rc})"
            )
        # The helper keeps logging to stdout; drain it so the pipe never
        # fills and blocks the helper.
        threading.Thread(
            target=self._drain_stdout, args=(self._proc,), daemon=True,
        ).start()
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self._sock.settimeout(30)

    @staticmethod
    def _drain_stdout(proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                logger.debug("rdp helper: %s", line.rstrip())
        except (OSError, ValueError):
            pass

    def close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc = None

    def close(self) -> None:
        with self._lock:
            if self._sock is not None and self._proc is not None:
                try:
                    self._sock.sendall(pack_request(OP_QUIT))
                    self._proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.close_locked()

    # -- request plumbing --

    def _exchange(self, request: bytes, hdr: struct.Struct) -> tuple:
        """One request/response round trip, respawning a dead helper once."""
        with self._lock:
            for attempt in (0, 1):
                if (
                    self._sock is None
                    or self._proc is None
                    or self._proc.poll() is not None
                ):
                    self._start_locked()
                try:
                    assert self._sock is not None
                    self._sock.sendall(request)
                    fields = hdr.unpack(self._recv_exact(hdr.size))
                    if fields[0] != 1:
                        raise HelperError("helper reported request failure")
                    return fields
                except (OSError, HelperError):
                    if attempt == 1:
                        raise
                    self.close_locked()
            raise AssertionError("unreachable")

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise HelperError("helper control connection closed")
            buf.extend(chunk)
        return bytes(buf)

    # -- operations --

    def get_frame(
        self, last_seq: int,
    ) -> tuple[bool, int, int, int, bytes | None]:
        """Return ``(connected, seq, width, height, pixels|None)``.

        *pixels* is packed BGRX32 (``width*height*4``), present only when
        the helper's frame sequence differs from *last_seq*.
        """
        with self._lock:
            for attempt in (0, 1):
                if (
                    self._sock is None
                    or self._proc is None
                    or self._proc.poll() is not None
                ):
                    self._start_locked()
                try:
                    assert self._sock is not None
                    self._sock.sendall(pack_request(OP_GETFRAME, last_seq))
                    ok, connected, seq, w, h, length = _FRAME_HDR.unpack(
                        self._recv_exact(_FRAME_HDR.size)
                    )
                    if ok != 1:
                        raise HelperError("helper reported request failure")
                    data = self._recv_exact(length) if length else None
                    return bool(connected), seq, w, h, data
                except (OSError, HelperError):
                    if attempt == 1:
                        raise
                    self.close_locked()
            raise AssertionError("unreachable")

    def send_mouse(self, flags: int, x: int, y: int) -> None:
        self._exchange(
            pack_request(OP_MOUSE, flags, max(0, x), max(0, y)),
            struct.Struct("<B"),
        )

    def send_scancode(self, scancode: int, down: bool) -> None:
        """Send one key event; *scancode* carries the EXTENDED bit."""
        self._exchange(
            pack_request(OP_KEY, scancode, 1 if down else 0),
            struct.Struct("<B"),
        )

    def status(self) -> tuple[bool, int, int, int]:
        """Return ``(connected, frame_seq, width, height)``."""
        _ok, connected, seq, w, h = self._exchange(
            pack_request(OP_STATUS), _STATUS_HDR,
        )
        return bool(connected), seq, w, h


# -- Pixel translation -------------------------------------------------------


def bgrx_to_rgba(data: bytes) -> bytes:
    """Convert packed BGRX32 pixels to RGBA8 with opaque alpha."""
    n = len(data) // 4
    rgba = bytearray(n * 4)
    rgba[0::4] = data[2::4]
    rgba[1::4] = data[1::4]
    rgba[2::4] = data[0::4]
    rgba[3::4] = b"\xff" * n
    return bytes(rgba)


#: The native wire format of the decoded frame, as advertised in ServerInit:
#: 32 bpp, depth 24, little-endian, true colour, shifts R=16 G=8 B=0
#: (bytes on the wire: B, G, R, X — what ``capture_to_png`` expects).
NATIVE_PIXEL_FORMAT = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def translate_pixels(
    data: bytes,
    pixel_format: tuple[int, ...],
) -> bytes:
    """Re-order native BGRX pixels into the client's 32-bit true-colour
    format.

    Only 8-bits-per-channel 32 bpp formats are supported (every byte-aligned
    shift combination, both endiannesses); anything else must be rejected at
    ``SetPixelFormat`` time.
    """
    if pixel_format == NATIVE_PIXEL_FORMAT:
        return data
    bpp, _depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs = (
        pixel_format
    )
    if bpp != 32 or not true_colour or (rmax, gmax, bmax) != (255, 255, 255):
        raise ValueError(f"unsupported client pixel format: {pixel_format}")
    out = bytearray(len(data))
    for shift, src_off in ((rs, 2), (gs, 1), (bs, 0)):
        if shift % 8:
            raise ValueError(f"unsupported channel shift {shift}")
        byte_index = shift // 8
        if big_endian:
            byte_index = 3 - byte_index
        out[byte_index::4] = data[src_off::4]
    return bytes(out)


# -- RFB 3.8 framing (pure helpers, unit-tested) -----------------------------


def rfb_server_init(width: int, height: int, name: str) -> bytes:
    """Build the ``ServerInit`` message advertising the native pixel
    format."""
    bpp, depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs = (
        NATIVE_PIXEL_FORMAT
    )
    pixfmt = struct.pack(
        ">BBBBHHHBBB3x",
        bpp, depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs,
    )
    name_bytes = name.encode("utf-8")
    return (
        struct.pack(">HH", width, height)
        + pixfmt
        + struct.pack(">I", len(name_bytes))
        + name_bytes
    )


def rfb_framebuffer_update(
    width: int, height: int, pixels: bytes,
) -> bytes:
    """Build a one-rect full-frame ``FramebufferUpdate`` (Raw encoding)."""
    return (
        struct.pack(">BxH", 0, 1)
        + struct.pack(">HHHHi", 0, 0, width, height, 0)
        + pixels
    )


def parse_set_pixel_format(payload: bytes) -> tuple[int, ...]:
    """Parse the 16-byte pixel format from a ``SetPixelFormat`` body
    (after the 3 padding bytes)."""
    bpp, depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs = (
        struct.unpack(">BBBBHHHBBB3x", payload)
    )
    return (bpp, depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs)


def pointer_events(
    prev_mask: int, mask: int, x: int, y: int,
) -> list[tuple[int, int, int]]:
    """Translate an RFB ``PointerEvent`` into RDP mouse events.

    Returns ``(flags, x, y)`` tuples: a move, button transitions derived
    from the mask diff (RFB bits: 0=left, 1=middle, 2=right), and wheel
    steps for the transient scroll bits (3=up, 4=down, 5=left, 6=right).
    """
    events: list[tuple[int, int, int]] = [(PTR_FLAGS_MOVE, x, y)]
    for bit, button_flag in (
        (0, PTR_FLAGS_BUTTON1),
        (1, PTR_FLAGS_BUTTON3),
        (2, PTR_FLAGS_BUTTON2),
    ):
        was = prev_mask & (1 << bit)
        now = mask & (1 << bit)
        if now and not was:
            events.append((PTR_FLAGS_DOWN | button_flag, x, y))
        elif was and not now:
            events.append((button_flag, x, y))
    for bit, wheel_flags in (
        (3, PTR_FLAGS_WHEEL | _WHEEL_DELTA),
        (4, PTR_FLAGS_WHEEL | PTR_FLAGS_WHEEL_NEGATIVE | (0x100 - _WHEEL_DELTA)),
        (5, PTR_FLAGS_HWHEEL | PTR_FLAGS_WHEEL_NEGATIVE | (0x100 - _WHEEL_DELTA)),
        (6, PTR_FLAGS_HWHEEL | _WHEEL_DELTA),
    ):
        if mask & (1 << bit) and not prev_mask & (1 << bit):
            events.append((wheel_flags, x, y))
    return events


# -- RFB server front --------------------------------------------------------


class _RfbFront:
    """Minimal RFB 3.8 server on loopback re-serving the decoded RDP frame.

    Enough of the protocol for the two in-tree clients — the noVNC
    WebSocket proxy (a passthrough) and ``rfb_snapshot.capture_to_png``:
    None auth, Raw encoding, full-frame updates on damage.  Input events
    are forwarded to the RDP session; ``SetEncodings`` is ignored (Raw is
    always legal); ``SetPixelFormat`` is honoured for 32-bit true-colour
    formats.
    """

    _POLL_INTERVAL = 0.1

    def __init__(
        self,
        helper: HelperClient,
        width: int,
        height: int,
        *,
        name: str = "openshrimp-hcs",
        cut_text_handler: Callable[[str], None] | None = None,
    ) -> None:
        self._helper = helper
        self._width = width
        self._height = height
        self._name = name
        self._cut_text_handler = cut_text_handler
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._port: int = self._server.getsockname()[1]
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._accept_loop, name="hcs-rfb-front", daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return self._port

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._server.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                client, _addr = self._server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve_client, args=(client,), daemon=True,
            ).start()

    def _serve_client(self, sock: socket.socket) -> None:
        try:
            sock.settimeout(30)
            self._handshake(sock)
            self._client_loop(sock)
        except (OSError, ConnectionError):
            pass
        except Exception:
            logger.exception("RFB front client handler failed")
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handshake(self, sock: socket.socket) -> None:
        sock.sendall(b"RFB 003.008\n")
        version = _recv_exact(sock, 12)
        if not version.startswith(b"RFB 003.00"):
            raise ConnectionError(f"bad client version {version!r}")
        sock.sendall(bytes([1, 1]))  # one security type: None
        if _recv_exact(sock, 1)[0] != 1:
            raise ConnectionError("client refused None security")
        sock.sendall(struct.pack(">I", 0))  # SecurityResult OK
        _recv_exact(sock, 1)  # ClientInit (shared flag, ignored)
        sock.sendall(rfb_server_init(self._width, self._height, self._name))

    def _client_loop(self, sock: socket.socket) -> None:
        pixel_format = NATIVE_PIXEL_FORMAT
        last_seq = 0
        pending = False       # an update request is outstanding
        pending_full = False  # ...and it wants the frame even without damage
        pointer_mask = 0
        sock.settimeout(self._POLL_INTERVAL)
        while not self._stopping.is_set():
            try:
                head = sock.recv(1)
                if not head:
                    return
                msg_type = head[0]
            except TimeoutError:
                msg_type = None
            sock.settimeout(30)

            if msg_type == 0:  # SetPixelFormat
                payload = _recv_exact(sock, 19)[3:]
                candidate = parse_set_pixel_format(payload)
                translate_pixels(b"", candidate)  # validates; raises if bad
                pixel_format = candidate
            elif msg_type == 2:  # SetEncodings
                (count,) = struct.unpack(">xH", _recv_exact(sock, 3))
                _recv_exact(sock, 4 * count)
            elif msg_type == 3:  # FramebufferUpdateRequest
                incremental, _x, _y, _w, _h = struct.unpack(
                    ">BHHHH", _recv_exact(sock, 9),
                )
                pending = True
                pending_full = pending_full or not incremental
            elif msg_type == 4:  # KeyEvent
                down, _pad, keysym = struct.unpack(
                    ">BHI", _recv_exact(sock, 7),
                )
                sc = keysym_to_scancode(keysym)
                if sc is not None:
                    self._helper.send_scancode(sc, bool(down))
            elif msg_type == 5:  # PointerEvent
                mask, x, y = struct.unpack(">BHH", _recv_exact(sock, 5))
                for flags, ex, ey in pointer_events(pointer_mask, mask, x, y):
                    self._helper.send_mouse(flags, ex, ey)
                pointer_mask = mask
            elif msg_type == 6:  # ClientCutText
                (length,) = struct.unpack(">3xI", _recv_exact(sock, 7))
                text = _recv_exact(sock, length).decode("latin-1")
                if self._cut_text_handler is not None:
                    try:
                        self._cut_text_handler(text)
                    except Exception:
                        logger.warning(
                            "clipboard set from RFB client failed",
                            exc_info=True,
                        )
            elif msg_type is not None:
                raise ConnectionError(f"unknown RFB message type {msg_type}")

            if pending:
                _connected, seq, w, h, data = self._helper.get_frame(last_seq)
                if data is None and pending_full:
                    _connected, seq, w, h, data = self._helper.get_frame(
                        seq ^ 0xFFFFFFFF
                    )
                if data is None and pending_full and seq == 0:
                    # Nothing decoded yet — satisfy the request with a
                    # blank frame rather than stalling the client.
                    w, h = self._width, self._height
                    data = bytes(w * h * 4)
                if data is not None:
                    last_seq = seq
                    pending = False
                    pending_full = False
                    sock.sendall(
                        rfb_framebuffer_update(
                            w, h, translate_pixels(data, pixel_format),
                        )
                    )
            sock.settimeout(self._POLL_INTERVAL)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"peer closed mid-message ({len(buf)}/{n})")
        buf.extend(chunk)
    return bytes(buf)


# -- The session -------------------------------------------------------------

#: In-guest path recording the wayland socket name the compositor created.
_WAYLAND_DISPLAY_FILE = "/run/user/0/wayland-display"

#: Delay between synthetic key transitions, mirroring the proven cadence
#: the desktop reliably keeps up with.
_KEY_DELAY_S = 0.03


class HcsRdpSession:
    """One persistent RDP session to an HCS computer-use guest.

    *target* selects the transport the helper dials: ``hv:<RuntimeId>``
    for the production hvsocket path (vsock service port 3389), or
    ``tcp:<host>:<port>`` for a pre-bridged endpoint.

    *exec_fn* runs an argv inside the guest rootfs chroot and returns
    ``(exit_code, output)`` — the clipboard members are implemented over
    it with ``wl-copy``/``wl-paste`` (the compositor keeps the RDP session
    unaware of the clipboard, so it travels the exec channel instead).
    """

    def __init__(
        self,
        *,
        helper_exe: Path,
        target: str,
        # The default matches the 1280x720 coordinate contract of the
        # computer-use tools (and the desktop size they describe).
        width: int = 1280,
        height: int = 720,
        dll_dir: Path | None = None,
        exec_fn: Callable[..., tuple[int, str]] | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self._width = width
        self._height = height
        self._exec_fn = exec_fn
        self._helper = HelperClient(
            helper_exe, target, width, height,
            dll_dir=dll_dir, connect_timeout=connect_timeout,
        )
        self._front: _RfbFront | None = None

    # -- lifecycle --

    def start(self) -> None:
        """Connect the RDP session and bring up the RFB front."""
        self._helper.start()
        cut_handler = (
            self.set_clipboard if self._exec_fn is not None else None
        )
        self._front = _RfbFront(
            self._helper, self._width, self._height,
            cut_text_handler=cut_handler,
        )

    def stop(self) -> None:
        if self._front is not None:
            self._front.stop()
            self._front = None
        self._helper.close()

    # -- display --

    def get_vnc_port(self) -> int | None:
        return self._front.port if self._front is not None else None

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        return None

    def get_vnc_quirks(self) -> frozenset:
        return frozenset()

    def get_screenshots_dir(self) -> Path | None:
        return None

    def take_screenshot(self, output_path: Path) -> None:
        """Encode the latest decoded frame as PNG.

        The RDP stream is damage-driven — a static desktop paints nothing —
        so this serves the retained frame rather than waiting for a new
        paint.  The one wait is for the first-ever frame: right after
        connect the initial paint can still be in flight.
        """
        deadline = time.monotonic() + 10.0
        while True:
            _connected, _seq, w, h, data = self._helper.get_frame(0xFFFFFFFF)
            if data is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if data is None:
            raise RuntimeError(
                "no RDP frame decoded yet (desktop still starting?)"
            )
        output_path.write_bytes(encode_png(w, h, bgrx_to_rgba(data)))

    # -- input --

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        flag = _BUTTON_FLAGS.get(button)
        if flag is None:
            raise ValueError(f"unknown mouse button {button!r}")
        self._helper.send_mouse(PTR_FLAGS_MOVE, x, y)
        time.sleep(0.05)
        self._helper.send_mouse(PTR_FLAGS_DOWN | flag, x, y)
        time.sleep(0.05)
        self._helper.send_mouse(flag, x, y)

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        flags_by_direction = {
            "up": PTR_FLAGS_WHEEL | _WHEEL_DELTA,
            "down": PTR_FLAGS_WHEEL
            | PTR_FLAGS_WHEEL_NEGATIVE
            | (0x100 - _WHEEL_DELTA),
            "left": PTR_FLAGS_HWHEEL
            | PTR_FLAGS_WHEEL_NEGATIVE
            | (0x100 - _WHEEL_DELTA),
            "right": PTR_FLAGS_HWHEEL | _WHEEL_DELTA,
        }
        flags = flags_by_direction.get(direction)
        if flags is None:
            raise ValueError(f"unknown scroll direction {direction!r}")
        self._helper.send_mouse(PTR_FLAGS_MOVE, x, y)
        for _ in range(amount):
            self._helper.send_mouse(flags, x, y)
            time.sleep(0.02)

    def send_type(self, text: str) -> None:
        for sc, down in type_scancodes(text):
            self._helper.send_scancode(sc, down)
            time.sleep(_KEY_DELAY_S)

    def send_key(self, key_str: str) -> None:
        for sc, down in combo_scancodes(key_str):
            self._helper.send_scancode(sc, down)
            time.sleep(_KEY_DELAY_S)

    def focus_window(self, name: str) -> None:
        raise NotImplementedError(
            "Window focus via toplevel is not supported in VM contexts. "
            "Use computer_click to click on the desired window, or use "
            "alt+Tab to switch windows."
        )

    # -- clipboard (wl-clipboard over the guest exec channel) --

    def _wayland_env_prefix(self) -> str:
        return (
            'export XDG_RUNTIME_DIR=/run/user/0; '
            f'export WAYLAND_DISPLAY="$(cat {_WAYLAND_DISPLAY_FILE} '
            '2>/dev/null || echo wayland-0)"; '
        )

    def get_clipboard(self) -> str:
        if self._exec_fn is None:
            raise NotImplementedError(
                "clipboard requires the guest exec channel"
            )
        rc, out = self._exec_fn(
            ["sh", "-c", self._wayland_env_prefix() + "wl-paste --no-newline"],
            read_timeout=10.0,
        )
        if rc != 0:
            return ""
        return out

    def set_clipboard(self, text: str) -> None:
        if self._exec_fn is None:
            raise NotImplementedError(
                "clipboard requires the guest exec channel"
            )
        # wl-copy stays resident to serve paste requests; detach it from
        # the exec connection (stdio redirected, setsid) so the call
        # returns while the clipboard keeps serving.  The text travels
        # base64-encoded: it may contain any shell metacharacters.
        import base64

        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        script = (
            self._wayland_env_prefix()
            + f'printf %s {b64} | base64 -d > /tmp/.clip-set; '
            'setsid wl-copy < /tmp/.clip-set >/dev/null 2>&1 & '
            'sleep 0.2; rm -f /tmp/.clip-set; echo CLIP-SET-OK'
        )
        rc, out = self._exec_fn(["sh", "-c", script], read_timeout=10.0)
        if rc != 0 or "CLIP-SET-OK" not in out:
            raise RuntimeError(f"wl-copy failed: {out.strip()}")


# -- Helper resolution -------------------------------------------------------

_HELPER_SOURCE = Path(__file__).with_name("hcs_rdp_helper.c")

#: The prebuilt helper is published as one archive holding the exe and every
#: FreeRDP DLL it loads, so an operator needs no toolchain at all.
HELPER_EXE_NAME = "hcs_rdp_helper.exe"
HELPER_ASSET = "openshrimp-hcs-rdp-helper-windows-x86_64.zip"
_REPO = "yjwong/open-shrimp"
HELPER_DOWNLOAD_URL = (
    f"https://github.com/{_REPO}/releases/latest/download/{HELPER_ASSET}"
)


def shipped_helper_dir() -> Path:
    """Where a downloaded bundle is unpacked.  Per-user, not per-context: the
    bundle is host state shared by every computer-use sandbox."""
    return user_data_path("openshrimp") / "hcs-rdp-helper"


def find_shipped_helper() -> Path | None:
    """The prebuilt helper if one is already staged, else ``None``.

    ``OPENSHRIMP_HCS_RDP_HELPER`` names either the exe or the directory
    holding it (its DLLs sit alongside); when it is set and resolves to
    nothing, that is an error rather than a silent fall-through to a download
    the operator did not ask for.
    """
    override = os.environ.get("OPENSHRIMP_HCS_RDP_HELPER")
    if override:
        path = Path(override)
        exe = path / HELPER_EXE_NAME if path.is_dir() else path
        if not exe.is_file():
            raise RuntimeError(
                f"OPENSHRIMP_HCS_RDP_HELPER is set to {override!r}, which is "
                f"neither the RDP helper exe nor a directory containing "
                f"{HELPER_EXE_NAME}."
            )
        return exe
    cached = shipped_helper_dir() / HELPER_EXE_NAME
    return cached if cached.is_file() else None


def download_shipped_helper() -> Path:
    """Fetch the released helper bundle and unpack it; return the exe.

    The archive is extracted whole — the exe alone is not runnable, because
    the FreeRDP DLLs shipped beside it are what the loader resolves against.
    """
    target_dir = shipped_helper_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading the HCS RDP helper from %s ...", HELPER_DOWNLOAD_URL)
    req = urllib.request.Request(
        HELPER_DOWNLOAD_URL, headers={"Accept": "application/octet-stream"},
    )
    tmp = target_dir / f"{HELPER_ASSET}.tmp"
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
        with zipfile.ZipFile(tmp) as zf:
            zf.extractall(target_dir)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        raise RuntimeError(
            f"Failed to download the HCS RDP helper from "
            f"{HELPER_DOWNLOAD_URL}: {exc}"
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    exe = target_dir / HELPER_EXE_NAME
    if not exe.is_file():
        raise RuntimeError(
            f"The HCS RDP helper archive at {HELPER_DOWNLOAD_URL} contains no "
            f"{HELPER_EXE_NAME}."
        )
    return exe


def ensure_rdp_helper(
    out_dir: Path, mingw_bin: Path | None,
) -> tuple[Path, Path]:
    """Resolve the RDP helper exe and the directory its DLLs load from.

    A prebuilt helper wins over a local build: it ships with the FreeRDP DLLs
    it was linked against, so it is the path that works on a machine with no
    toolchain.  Compiling from source is the fallback for a source install,
    and needs ``mingw_bin``.  With neither, the error names both — supplying
    one of them is the only thing that makes computer use work.
    """
    shipped = find_shipped_helper()
    download_error: str | None = None
    if shipped is None:
        try:
            shipped = download_shipped_helper()
        except RuntimeError as exc:
            download_error = str(exc)
    if shipped is not None:
        return shipped, shipped.parent
    if mingw_bin is not None:
        logger.info("%s; building the helper locally instead.", download_error)
        return build_helper_exe(out_dir, mingw_bin), mingw_bin
    raise RuntimeError(
        "HCS computer use needs the RDP helper, and neither of the two ways "
        "to get one is available. Supply either: (1) the prebuilt bundle — "
        f"download {HELPER_ASSET} from an OpenShrimp release and unpack it "
        f"into {shipped_helper_dir()}, or point OPENSHRIMP_HCS_RDP_HELPER at "
        "a directory holding it; or (2) a toolchain to build it from source "
        r"— set sandbox.mingw_bin to an MSYS2 mingw64 bin directory (e.g. "
        r"C:\msys64\mingw64\bin) with the mingw-w64-x86_64-freerdp, -gcc and "
        f"-pkgconf packages installed. Fetching the bundle was tried first "
        f"and failed: {download_error}"
    )


def build_helper_exe(out_dir: Path, mingw_bin: Path) -> Path:
    """Compile the native RDP helper if missing or stale; return its path.

    Requires an MSYS2 mingw64 toolchain (``gcc``, ``pkgconf``) with the
    ``mingw-w64-x86_64-freerdp`` package installed.  The build is skipped
    while the staged source copy matches the packaged one, mirroring the
    launcher-exe build strategy.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = out_dir / HELPER_EXE_NAME
    staged_src = out_dir / "hcs_rdp_helper.c"
    source = _HELPER_SOURCE.read_text(encoding="utf-8")
    if (
        exe.exists()
        and staged_src.exists()
        and staged_src.read_text(encoding="utf-8") == source
    ):
        return exe
    staged_src.write_text(source, encoding="utf-8")

    # gcc's subprograms (cc1, the linker) resolve their DLLs through PATH;
    # invoking the toolchain by absolute path alone dies without output.
    env = dict(os.environ)
    env["PATH"] = f"{mingw_bin}{os.pathsep}{env.get('PATH', '')}"
    pkgconf = mingw_bin / "pkgconf.exe"
    gcc = mingw_bin / "gcc.exe"
    flags = subprocess.run(
        [str(pkgconf), "--cflags", "--libs",
         "freerdp-client3", "freerdp3", "winpr3"],
        capture_output=True, text=True, check=True, env=env,
    ).stdout.split()
    result = subprocess.run(
        [str(gcc), str(staged_src), "-o", str(exe),
         "-D__STDC_NO_THREADS__", "-O2", *flags, "-lws2_32"],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0 or not exe.exists():
        raise RuntimeError(
            f"failed to build the HCS RDP helper (exit {result.returncode}): "
            f"{(result.stdout + result.stderr).strip()}"
        )
    return exe
