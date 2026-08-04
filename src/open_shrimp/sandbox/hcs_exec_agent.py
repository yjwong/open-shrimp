"""Guest-side exec server for the Windows HCS sandbox backend.

Runs *inside* the sandbox rootfs (launched via the control channel as
``chroot <rootfs> python3 /run/openshrimp/exec_agent.py <port>``) and listens
on ``AF_VSOCK``.  The host-side launcher executable connects over hvsocket,
sends one length-prefixed JSON header ``{"argv": [...], "cwd": ..., "env":
{...}}``, then streams raw stdin bytes; the server ``exec``\\ s the argv with
**no shell anywhere** — the arguments cross the host→guest boundary as a
structured list, which is what keeps cmd.exe/sh metacharacters inert.

Wire protocol (server → client), one frame per chunk::

    [type:1][length:4 big-endian][payload]

    type 1 = child stdout, 2 = child stderr,
    type 3 = child exit status (ASCII decimal payload; final frame)

Client → server after the header is raw child stdin; the client half-closing
its send side is child-stdin EOF.  One connection per CLI session, served
concurrently by threads.

This file is copied into the guest verbatim and must stay stdlib-only with
no package imports.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading

FRAME_STDOUT = 1
FRAME_STDERR = 2
FRAME_EXIT = 3

_HEADER_LIMIT = 8 * 1024 * 1024


def _read_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _serve(conn: socket.socket) -> None:
    try:
        raw = _read_exact(conn, 4)
        if raw is None:
            return
        (hlen,) = struct.unpack(">I", raw)
        if hlen > _HEADER_LIMIT:
            return
        raw = _read_exact(conn, hlen)
        if raw is None:
            return
        header = json.loads(raw)
        argv = [str(a) for a in header["argv"]]
        cwd = str(header.get("cwd") or "/")
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (header.get("env") or {}).items()})

        send_lock = threading.Lock()

        def send_frame(ftype: int, payload: bytes) -> None:
            with send_lock:
                conn.sendall(struct.pack(">BI", ftype, len(payload)) + payload)

        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            try:
                send_frame(FRAME_STDERR, f"exec failed: {exc}\n".encode())
                send_frame(FRAME_EXIT, b"127")
            except OSError:
                pass
            return

        def pump_stdin() -> None:
            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        def pump_out(stream, ftype: int) -> None:
            try:
                while True:
                    data = stream.read1(65536)
                    if not data:
                        break
                    send_frame(ftype, data)
            except (OSError, ValueError):
                # The client is gone; a headless child must not outlive it.
                proc.kill()

        threads = [
            threading.Thread(target=pump_stdin, daemon=True),
            threading.Thread(target=pump_out, args=(proc.stdout, FRAME_STDOUT), daemon=True),
            threading.Thread(target=pump_out, args=(proc.stderr, FRAME_STDERR), daemon=True),
        ]
        for t in threads:
            t.start()
        code = proc.wait()
        for t in threads[1:]:
            t.join(timeout=10)
        try:
            send_frame(FRAME_EXIT, str(code).encode("ascii"))
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 — a bad connection must not kill the server
        print(f"exec-agent connection error: {exc}", flush=True)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0x5001
    srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    srv.bind((socket.VMADDR_CID_ANY, port))
    srv.listen(16)
    print(f"OPENSHRIMP-EXEC-AGENT-LISTENING port={port}", flush=True)
    while True:
        conn, _addr = srv.accept()
        threading.Thread(target=_serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
