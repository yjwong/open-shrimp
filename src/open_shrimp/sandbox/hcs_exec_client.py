"""Host-side client for the in-guest exec server (Windows only).

The compiled launcher exe is the *production* path (the Agent SDK spawns it as
``cli_path``); this Python client speaks the identical framing so the backend
can run one-shot commands inside the sandbox (the in-guest CLI installer), and
so the protocol can be exercised from tests without the launcher.

Frame layout matches :mod:`open_shrimp.sandbox.hcs_exec_agent`::

    header:  [len:4 big-endian][utf-8 json {"argv","cwd","env"}]
    server → client:  [type:1][len:4][payload]  (1=stdout 2=stderr 3=exit)
"""

from __future__ import annotations

import json
import socket
import struct

from open_shrimp.sandbox.hcs_helpers import vsock_service_id

FRAME_STDOUT = 1
FRAME_STDERR = 2
FRAME_EXIT = 3


def _read_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def run(
    runtime_id: str,
    port: int,
    argv: list[str],
    *,
    cwd: str = "/",
    env: dict[str, str] | None = None,
    stdin: bytes = b"",
    connect_timeout: float = 30.0,
    read_timeout: float = 300.0,
) -> tuple[int, str]:
    """Run *argv* in the guest exec server; return ``(exit_code, combined)``.

    ``combined`` folds stdout and stderr in arrival order — adequate for the
    installer's use; the launcher keeps the two streams separate.
    """
    sock = socket.socket(
        socket.AF_HYPERV, socket.SOCK_STREAM, socket.HV_PROTOCOL_RAW,
    )
    out = bytearray()
    try:
        try:
            sock.setsockopt(
                socket.HV_PROTOCOL_RAW,
                socket.HVSOCKET_CONNECT_TIMEOUT,
                int(connect_timeout * 1000),
            )
        except (AttributeError, OSError):
            pass
        sock.settimeout(connect_timeout)
        sock.connect((runtime_id, vsock_service_id(port)))

        header = json.dumps(
            {"argv": list(argv), "cwd": cwd, "env": env or {}}
        ).encode("utf-8")
        sock.sendall(struct.pack(">I", len(header)) + header)
        if stdin:
            sock.sendall(stdin)
        sock.shutdown(socket.SHUT_WR)

        sock.settimeout(read_timeout)
        while True:
            hdr = _read_exact(sock, 5)
            if hdr is None:
                return -1, out.decode("utf-8", "replace")
            ftype, length = hdr[0], struct.unpack(">I", hdr[1:5])[0]
            payload = _read_exact(sock, length) if length else b""
            if payload is None:
                return -1, out.decode("utf-8", "replace")
            if ftype in (FRAME_STDOUT, FRAME_STDERR):
                out += payload
            elif ftype == FRAME_EXIT:
                try:
                    return int(payload.decode("ascii")), out.decode("utf-8", "replace")
                except ValueError:
                    return -1, out.decode("utf-8", "replace")
    finally:
        sock.close()
