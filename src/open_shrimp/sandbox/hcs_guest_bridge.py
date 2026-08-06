"""Guest-side half of the host→guest TCP bridge for the HCS sandbox backend.

Runs inside the sandbox guest.  For each guest port it is told to serve, it
listens on ``AF_VSOCK`` at that port number and, per accepted connection, dials
``127.0.0.1:<port>`` inside the guest and pipes the two halves.  The host half
(``hcs_win.open_guest_port_bridge``) listens on host loopback and dials
``AF_HYPERV`` at the matching service GUID, so a host client connecting to
``127.0.0.1:<host_port>`` lands on the guest service.

The vsock port *is* the guest TCP port — the same convention the in-guest RDP
relay follows — so no port negotiation crosses the link and the host sends no
header.

This is the mirror image of ``hcs_host_relay.py``, which serves the opposite
direction, and the two must not be confused.  Here the host initiates every
connection, so nothing needs the ``GuestCommunicationServices`` registry hive
(that hive is the guest→host allow-list, consulted only when the host listens)
and there is no port allow-list: the party dialing in is the trusted one.

Copied into the guest verbatim; stdlib-only, no package imports.
"""

from __future__ import annotations

import socket
import sys
import threading


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


def bridge(conn: socket.socket, port: int) -> None:
    """Serve one accepted host connection against guest ``127.0.0.1:port``."""
    try:
        down = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        conn.close()
        return
    t = threading.Thread(target=_pipe, args=(conn, down), daemon=True)
    t.start()
    _pipe(down, conn)
    conn.close()
    down.close()


def _serve(port: int) -> None:
    ls = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    ls.bind((socket.VMADDR_CID_ANY, port))
    ls.listen(64)
    while True:
        conn, _addr = ls.accept()
        threading.Thread(target=bridge, args=(conn, port), daemon=True).start()


def main() -> None:
    ports = [int(p) for p in sys.argv[1:]]
    threads = [threading.Thread(target=_serve, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    print("OPENSHRIMP-GUEST-BRIDGE-LISTENING " + ",".join(map(str, ports)), flush=True)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
