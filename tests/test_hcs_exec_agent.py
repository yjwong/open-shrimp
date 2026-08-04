"""Protocol round-trip tests for the HCS in-guest exec server.

Drives :func:`open_shrimp.sandbox.hcs_exec_agent._serve` over a local
``socketpair`` (the real server binds ``AF_VSOCK``, but ``_serve`` takes an
already-connected socket, so the framing is exercised the same way).  Runs on
Linux — no Windows, no vsock.
"""

from __future__ import annotations

import json
import socket
import struct
import threading

from open_shrimp.sandbox import hcs_exec_agent as A

FRAME_STDOUT = 1
FRAME_STDERR = 2
FRAME_EXIT = 3


def _drive(argv, cwd="/", env=None, stdin=b""):
    """Run *argv* through the exec server; return ``(exit, stdout, stderr)``."""
    server_sock, client_sock = socket.socketpair()
    t = threading.Thread(target=A._serve, args=(server_sock,), daemon=True)
    t.start()

    header = json.dumps({"argv": argv, "cwd": cwd, "env": env or {}}).encode()
    client_sock.sendall(struct.pack(">I", len(header)) + header)
    if stdin:
        client_sock.sendall(stdin)
    client_sock.shutdown(socket.SHUT_WR)

    out, err = bytearray(), bytearray()
    code = None
    buf = b""

    def recv_exact(n):
        nonlocal buf
        while len(buf) < n:
            chunk = client_sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
        head, buf = buf[:n], buf[n:]
        return head

    while True:
        hdr = recv_exact(5)
        if hdr is None:
            break
        ftype, length = hdr[0], struct.unpack(">I", hdr[1:5])[0]
        payload = recv_exact(length) if length else b""
        if payload is None:
            break
        if ftype == FRAME_STDOUT:
            out += payload
        elif ftype == FRAME_STDERR:
            err += payload
        elif ftype == FRAME_EXIT:
            code = int(payload.decode())
            break
    client_sock.close()
    t.join(timeout=10)
    return code, bytes(out), bytes(err)


def test_stdout_and_exit_zero():
    code, out, err = _drive(["/bin/echo", "hello world"])
    assert code == 0
    assert out == b"hello world\n"
    assert err == b""


def test_metacharacters_are_inert_no_shell():
    # A literal '&' / '|' / '%' must reach argv unchanged — there is no shell.
    payload = "a&b|c %PATH% $(whoami)"
    code, out, err = _drive(["/bin/echo", payload])
    assert code == 0
    assert out == payload.encode() + b"\n"


def test_nonzero_exit_passthrough():
    code, out, err = _drive(["/bin/sh", "-c", "exit 7"])
    assert code == 7


def test_stdin_is_piped():
    code, out, err = _drive(["/bin/cat"], stdin=b"piped-input\n")
    assert code == 0
    assert out == b"piped-input\n"


def test_stderr_separated_from_stdout():
    code, out, err = _drive(
        ["/bin/sh", "-c", "echo OUT; echo ERR 1>&2"]
    )
    assert code == 0
    assert b"OUT" in out
    assert b"ERR" in err


def test_env_is_applied():
    code, out, err = _drive(
        ["/bin/sh", "-c", "echo $OPENSHRIMP_TEST"],
        env={"OPENSHRIMP_TEST": "marker42"},
    )
    assert code == 0
    assert out.strip() == b"marker42"


def test_missing_executable_reports_127():
    code, out, err = _drive(["/nonexistent/binary-xyz"])
    assert code == 127
    assert b"exec failed" in err
