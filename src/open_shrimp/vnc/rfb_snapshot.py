"""RFB 3.8 snapshot client: capture one framebuffer to PNG.

Designed for Apple's private ``_VZVNCServer`` SPI exposed by the
patched ``limactl``.  Two protocol quirks shape the implementation:

- ``SetPixelFormat`` (RFB type 0) resets the connection.
- ``SetEncodings`` (type 2) crashes the host process via
  ``Base::report_fixme_if_and_trap``.

This client never sends either; it relies on the server-default 32-bit
BGRA pixel format and RFB's default raw encoding.  For the byte-stream
filter used on the noVNC proxy path, see :mod:`rfb_filter`.
"""

from __future__ import annotations

import socket
import struct
import zlib
from pathlib import Path


class RfbSnapshotError(RuntimeError):
    """Raised when the RFB handshake or framebuffer read fails."""


def capture_to_png(
    host: str,
    port: int,
    output_path: Path,
    *,
    timeout_secs: float = 15.0,
) -> tuple[int, int]:
    """Connect to an RFB 3.8 server, request a full framebuffer, write PNG.

    Returns ``(width, height)`` of the captured framebuffer.
    """
    sock = socket.create_connection((host, port), timeout=timeout_secs)
    try:
        sock.settimeout(timeout_secs)
        fb_w, fb_h = _handshake(sock)
        rgba = _read_full_framebuffer(sock, fb_w, fb_h)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    output_path.write_bytes(_encode_png(fb_w, fb_h, bytes(rgba)))
    return fb_w, fb_h


def _handshake(sock: socket.socket) -> tuple[int, int]:
    """Run the RFB 3.8 None-auth handshake; return framebuffer ``(w, h)``."""
    banner = _recv_exact(sock, 12)
    if not banner.startswith(b"RFB "):
        raise RfbSnapshotError(f"bad server banner: {banner!r}")
    sock.sendall(b"RFB 003.008\n")

    nsec = _recv_exact(sock, 1)[0]
    if nsec == 0:
        (reason_len,) = struct.unpack(">I", _recv_exact(sock, 4))
        reason = _recv_exact(sock, reason_len).decode("utf-8", "replace")
        raise RfbSnapshotError(f"server refused connection: {reason}")
    types = list(_recv_exact(sock, nsec))
    if 1 not in types:
        raise RfbSnapshotError(
            f"server requires auth (offered types {types}); "
            "snapshot client only supports None (type 1)"
        )
    sock.sendall(bytes([1]))

    (sec_result,) = struct.unpack(">I", _recv_exact(sock, 4))
    if sec_result != 0:
        raise RfbSnapshotError(f"security handshake failed (result={sec_result})")

    sock.sendall(bytes([1]))  # ClientInit shared=1

    si = _recv_exact(sock, 24)
    fb_w, fb_h = struct.unpack(">HH", si[:4])
    (name_len,) = struct.unpack(">I", si[20:24])
    if name_len:
        _recv_exact(sock, name_len)

    if fb_w == 0 or fb_h == 0:
        raise RfbSnapshotError(
            f"server reported empty framebuffer ({fb_w}x{fb_h}); "
            "is the guest's graphics driver attached?"
        )
    return fb_w, fb_h


def _read_full_framebuffer(
    sock: socket.socket, fb_w: int, fb_h: int,
) -> bytearray:
    """Send one full FramebufferUpdateRequest and read until every pixel covered.

    Returns RGBA8 bytes (alpha forced to 0xFF) of length ``fb_w*fb_h*4``.
    Skips Bell / ServerCutText.  Raises on unknown encodings or message types.
    """
    sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, fb_w, fb_h))

    rgba = bytearray(fb_w * fb_h * 4)
    rgba[3::4] = b"\xff" * (fb_w * fb_h)
    covered = 0
    target = fb_w * fb_h

    while covered < target:
        msg_type = _recv_exact(sock, 1)[0]
        if msg_type == 0:  # FramebufferUpdate
            hdr = _recv_exact(sock, 3)
            (n_rects,) = struct.unpack(">H", hdr[1:3])
            for _ in range(n_rects):
                rect_hdr = _recv_exact(sock, 12)
                x, y, rw, rh = struct.unpack(">HHHH", rect_hdr[:8])
                (enc,) = struct.unpack(">i", rect_hdr[8:12])
                if enc != 0:
                    raise RfbSnapshotError(
                        f"unsupported RFB encoding {enc} (only Raw/0)"
                    )
                pixels = _recv_exact(sock, rw * rh * 4)
                _blit_bgra_to_rgba(rgba, fb_w, x, y, rw, rh, pixels)
                covered += rw * rh
        elif msg_type == 2:  # Bell
            continue
        elif msg_type == 3:  # ServerCutText
            cut_hdr = _recv_exact(sock, 7)
            (cut_len,) = struct.unpack(">I", cut_hdr[3:7])
            if cut_len:
                _recv_exact(sock, cut_len)
        else:
            raise RfbSnapshotError(f"unexpected RFB message type {msg_type}")

    return rgba


def _blit_bgra_to_rgba(
    rgba: bytearray, fb_w: int, x: int, y: int, rw: int, rh: int,
    pixels: bytes,
) -> None:
    """Copy ``rw×rh`` BGRA pixels at ``(x, y)`` into the RGBA framebuffer.

    Uses C-level bytearray slice assignment — one pass per channel per row.
    """
    if x == 0 and rw == fb_w:
        # Hot path: full-width rect — copy contiguously across all rows.
        dst_off = y * fb_w * 4
        n = rw * rh
        rgba[dst_off : dst_off + n * 4 : 4] = pixels[2::4]      # R <- B-position
        rgba[dst_off + 1 : dst_off + n * 4 : 4] = pixels[1::4]  # G stays
        rgba[dst_off + 2 : dst_off + n * 4 : 4] = pixels[0::4]  # B <- R-position
        return

    row_stride = rw * 4
    for row in range(rh):
        src_off = row * row_stride
        dst_off = ((y + row) * fb_w + x) * 4
        src = pixels[src_off : src_off + row_stride]
        rgba[dst_off : dst_off + row_stride : 4] = src[2::4]
        rgba[dst_off + 1 : dst_off + row_stride : 4] = src[1::4]
        rgba[dst_off + 2 : dst_off + row_stride : 4] = src[0::4]


def _encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode 8-bit RGBA pixels as a minimal PNG (one IDAT, no filtering)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)

    stride = width * 4
    raw = bytearray(height * (stride + 1))
    for y in range(height):
        out = y * (stride + 1)
        in_ = y * stride
        raw[out + 1 : out + 1 + stride] = rgba[in_ : in_ + stride]
    idat = zlib.compress(bytes(raw), 6)

    return (
        sig
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(typ: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(typ + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise RfbSnapshotError(
                f"connection closed during read ({len(buf)}/{n} bytes)"
            )
        buf.extend(chunk)
    return bytes(buf)
