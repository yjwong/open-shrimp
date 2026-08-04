"""Guest-side half of the guest→host TCP relay for the HCS sandbox backend.

Runs inside the sandbox guest.  For each host port it is told to forward, it
listens on ``127.0.0.1:<port>`` and, per accepted connection, dials
``AF_VSOCK`` to the host (``VMADDR_CID_HOST``) at :data:`RELAY_PORT`, sends the
target port as a 2-byte big-endian header, and pipes the two halves.  The host
half (``hcs_win.ensure_host_relay``) accepts on ``AF_HYPERV`` and bridges to
``127.0.0.1:<port>`` on the host.

This exists because the HNS NAT gateway forwards guest egress to the internet
but does not expose the host's own loopback listeners — so the agent CLI
reaches the OpenShrimp MCP proxy (host ``127.0.0.1``) through this relay, with
the sandbox reporting ``host_address == "127.0.0.1"``.

Copied into the guest verbatim; stdlib-only, no package imports.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading

RELAY_PORT = 0x5002
VMADDR_CID_HOST = 2


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(local: socket.socket, port: int) -> None:
    try:
        up = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        up.connect((VMADDR_CID_HOST, RELAY_PORT))
        up.sendall(struct.pack(">H", port))
    except OSError:
        local.close()
        return
    t = threading.Thread(target=_pipe, args=(local, up), daemon=True)
    t.start()
    _pipe(up, local)
    local.close()
    up.close()


def _serve(port: int) -> None:
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", port))
    ls.listen(64)
    while True:
        conn, _addr = ls.accept()
        threading.Thread(target=_handle, args=(conn, port), daemon=True).start()


def main() -> None:
    ports = [int(p) for p in sys.argv[1:]]
    threads = [threading.Thread(target=_serve, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    print("OPENSHRIMP-HOST-RELAY-LISTENING " + ",".join(map(str, ports)), flush=True)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
