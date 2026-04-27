"""Tests for the RFB 3.8 snapshot client used on the VZ-host VNC path."""

from __future__ import annotations

import socket
import struct
import threading
import zlib
from pathlib import Path

import pytest

from open_shrimp.vnc.rfb_snapshot import (
    RfbSnapshotError,
    capture_to_png,
)


# ---- Mock RFB server -----------------------------------------------------


class _RfbMockServer:
    """One-shot RFB 3.8 server that hands a pre-built scene to clients.

    Spins up a TCP listener on a random localhost port, runs the
    handshake, sends one FramebufferUpdate (split across the requested
    number of rects), then closes the connection.
    """

    def __init__(
        self,
        width: int,
        height: int,
        bgra: bytes,
        *,
        n_rects: int = 1,
        rects: list[tuple[int, int, int, int, bytes]] | None = None,
        offer_security: list[int] | None = None,
        sec_result: int = 0,
        send_servercuttext_first: bool = False,
        send_bell_first: bool = False,
    ) -> None:
        self.width = width
        self.height = height
        self.bgra = bgra
        self.n_rects = n_rects
        self.rects = rects
        self.offer_security = offer_security or [1]
        self.sec_result = sec_result
        self.send_servercuttext_first = send_servercuttext_first
        self.send_bell_first = send_bell_first
        self.client_post_init_bytes: bytes = b""
        self.error: str | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port: int = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _RfbMockServer:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=2.0)

    def _serve(self) -> None:
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(5.0)
            self._handshake(conn)
            self._send_update(conn)
            try:
                # Drain anything trailing so the client can tell us about
                # extra messages (none expected from this client).
                conn.settimeout(0.05)
                tail = conn.recv(4096)
                if tail:
                    self.client_post_init_bytes += tail
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            self.error = repr(e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read(self, conn: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("client disconnected during handshake")
            buf.extend(chunk)
        return bytes(buf)

    def _handshake(self, conn: socket.socket) -> None:
        conn.sendall(b"RFB 003.008\n")
        self._read(conn, 12)  # client version

        conn.sendall(bytes([len(self.offer_security)]) + bytes(self.offer_security))
        chosen = self._read(conn, 1)[0]
        if chosen not in self.offer_security:
            raise RuntimeError(f"client picked unsupported sec-type {chosen}")
        conn.sendall(struct.pack(">I", self.sec_result))
        if self.sec_result != 0:
            return

        self._read(conn, 1)  # ClientInit shared flag

        # ServerInit: width(2) height(2) pixfmt(16) name-len(4) name
        pixfmt = struct.pack(
            ">BBBBHHHBBBBBB",
            32, 24, 0, 1,        # bpp=32, depth=24, big_endian=0, true_color=1
            255, 255, 255,       # max R, G, B
            16, 8, 0,            # shifts R=16, G=8, B=0  (i.e. BGRA in mem)
            0, 0, 0,             # padding
        )
        name = b"openshrimp-test"
        conn.sendall(
            struct.pack(">HH", self.width, self.height)
            + pixfmt
            + struct.pack(">I", len(name))
            + name,
        )

        # Read FramebufferUpdateRequest (10 bytes total — type + 9 body).
        req = self._read(conn, 10)
        if req[0] != 3:
            raise RuntimeError(f"expected msg type 3, got {req[0]}")
        # Anything we read past here we record for forbidden-byte checks.

    def _send_update(self, conn: socket.socket) -> None:
        if self.send_bell_first:
            conn.sendall(bytes([2]))  # Bell
        if self.send_servercuttext_first:
            text = b"hi"
            conn.sendall(bytes([3]) + b"\x00\x00\x00" + struct.pack(">I", len(text)) + text)

        rects = self._resolve_rects()
        conn.sendall(bytes([0]) + b"\x00" + struct.pack(">H", len(rects)))
        for x, y, rw, rh, payload in rects:
            conn.sendall(
                struct.pack(">HHHH", x, y, rw, rh)
                + struct.pack(">i", 0),  # Raw
            )
            conn.sendall(payload)

    def _resolve_rects(self) -> list[tuple[int, int, int, int, bytes]]:
        if self.rects is not None:
            return self.rects

        # Default: tile the framebuffer with N horizontal stripes.
        rect_h = self.height // self.n_rects
        if rect_h * self.n_rects != self.height:
            raise RuntimeError("test setup: height must be divisible by n_rects")
        stride = self.width * 4
        return [
            (
                0, i * rect_h, self.width, rect_h,
                self.bgra[i * rect_h * stride : (i + 1) * rect_h * stride],
            )
            for i in range(self.n_rects)
        ]


# ---- PNG decode helpers --------------------------------------------------


def _png_size(blob: bytes) -> tuple[int, int]:
    assert blob.startswith(b"\x89PNG\r\n\x1a\n")
    # IHDR is the first chunk: length(4) + type(4) + data(13) + crc(4).
    ihdr = blob[8 + 8 : 8 + 8 + 13]
    w, h = struct.unpack(">II", ihdr[:8])
    return w, h


def _png_pixels(blob: bytes, w: int, h: int) -> bytes:
    """Decode a minimal PNG (single IDAT, filter byte 0 per row) to RGBA bytes."""
    pos = 8
    idat = b""
    while pos < len(blob):
        (n,) = struct.unpack(">I", blob[pos : pos + 4])
        typ = blob[pos + 4 : pos + 8]
        data = blob[pos + 8 : pos + 8 + n]
        pos += 12 + n
        if typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = w * 4
    pixels = bytearray(h * stride)
    for y in range(h):
        if raw[y * (stride + 1)] != 0:
            raise AssertionError("only filter byte 0 is supported in this decoder")
        pixels[y * stride : (y + 1) * stride] = raw[
            y * (stride + 1) + 1 : (y + 1) * (stride + 1)
        ]
    return bytes(pixels)


def _solid_bgra(w: int, h: int, b: int, g: int, r: int) -> bytes:
    return bytes([b, g, r, 0xFF]) * (w * h)


# ---- Tests ---------------------------------------------------------------


@pytest.mark.parametrize("n_rects", [1, 3])
def test_capture_to_png_single_and_multi_rect(tmp_path: Path, n_rects: int) -> None:
    width, height = 12, 9
    # Encode (B, G, R) = (10, 20, 30) so we can verify the channel swap.
    bgra = _solid_bgra(width, height, 10, 20, 30)
    with _RfbMockServer(width, height, bgra, n_rects=n_rects) as srv:
        out = tmp_path / "snap.png"
        w, h = capture_to_png("127.0.0.1", srv.port, out, timeout_secs=5.0)

    assert (w, h) == (width, height)
    assert srv.error is None
    blob = out.read_bytes()
    assert _png_size(blob) == (width, height)
    rgba = _png_pixels(blob, width, height)
    # Channel swap: PNG is RGBA → R=30, G=20, B=10, A=255.
    for px in range(width * height):
        assert rgba[px * 4 + 0] == 30, f"R wrong at pixel {px}"
        assert rgba[px * 4 + 1] == 20
        assert rgba[px * 4 + 2] == 10
        assert rgba[px * 4 + 3] == 0xFF


def test_capture_to_png_does_not_send_setencodings_or_setpixelformat(
    tmp_path: Path,
) -> None:
    # If the snapshot client ever sent SetEncodings (msg=2) or
    # SetPixelFormat (msg=0) Apple's _VZVNCServer would crash the host.
    # The mock records every byte the client writes after ClientInit.
    width, height = 8, 4
    bgra = _solid_bgra(width, height, 0, 0, 0)
    with _RfbMockServer(width, height, bgra) as srv:
        capture_to_png("127.0.0.1", srv.port, tmp_path / "x.png", timeout_secs=5.0)

    # The only bytes the client sends after the handshake are the 10-byte
    # FramebufferUpdateRequest. No 0x00 (SetPixelFormat) or 0x02
    # (SetEncodings) message types should ever leak through.
    # The mock's `_handshake` already consumed the FBUR; anything
    # `client_post_init_bytes` captures is unexpected.
    assert srv.client_post_init_bytes == b""


def test_capture_skips_bell_and_servercuttext(tmp_path: Path) -> None:
    width, height = 4, 2
    bgra = _solid_bgra(width, height, 1, 2, 3)
    with _RfbMockServer(
        width, height, bgra,
        send_bell_first=True, send_servercuttext_first=True,
    ) as srv:
        capture_to_png("127.0.0.1", srv.port, tmp_path / "x.png", timeout_secs=5.0)
    assert srv.error is None


def test_capture_rejects_auth_required(tmp_path: Path) -> None:
    # Server only offers VNC password auth (type 2); client supports None (1).
    with _RfbMockServer(
        4, 2, _solid_bgra(4, 2, 0, 0, 0), offer_security=[2],
    ) as srv:
        with pytest.raises(RfbSnapshotError, match="None"):
            capture_to_png("127.0.0.1", srv.port, tmp_path / "x.png", timeout_secs=2.0)


def test_capture_rejects_failed_security(tmp_path: Path) -> None:
    with _RfbMockServer(
        4, 2, _solid_bgra(4, 2, 0, 0, 0), sec_result=1,
    ) as srv:
        with pytest.raises(RfbSnapshotError, match="security handshake failed"):
            capture_to_png("127.0.0.1", srv.port, tmp_path / "x.png", timeout_secs=2.0)


def test_capture_to_png_partial_rect(tmp_path: Path) -> None:
    """A non-full-width overwrite rect exercises the row-by-row blit path."""
    width, height = 8, 4
    rects = [
        (0, 0, width, height, _solid_bgra(width, height, 0, 0, 0)),
        (2, 1, 4, 2, _solid_bgra(4, 2, 0, 0, 0xAA)),  # red overwrite
    ]
    with _RfbMockServer(
        width, height, b"", rects=rects,
    ) as srv:
        out = tmp_path / "x.png"
        capture_to_png("127.0.0.1", srv.port, out, timeout_secs=5.0)
    assert srv.error is None

    rgba = _png_pixels(out.read_bytes(), width, height)
    # Pixel (3, 1) is inside the red sub-rect → R=0xAA.
    px = (1 * width + 3) * 4
    assert rgba[px : px + 4] == bytes([0xAA, 0, 0, 0xFF])
    # Pixel (0, 0) is outside → black opaque.
    assert rgba[0:4] == bytes([0, 0, 0, 0xFF])
