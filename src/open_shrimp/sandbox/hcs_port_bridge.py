"""Host-side half of the host→guest TCP bridge for the HCS sandbox backend.

Binds ``127.0.0.1:<host_port>``, and per accepted connection calls a *dial*
callable for the guest-side stream and pipes the two halves.  The dial is
injected because it is the only Windows-specific part: ``hcs_win`` supplies the
``AF_HYPERV`` connect to ``(RuntimeId, svc(guest_port))``, while the
accept-and-bridge loop here is plain sockets.

Direction invariants this module encodes:

* The host initiates every connection, so no ``GuestCommunicationServices``
  registration is involved — that registry hive gates guest→host connections
  and matters only where the *host* listens (``hcs_win.ensure_host_relay``).
* The listener binds ``127.0.0.1`` only, never ``0.0.0.0``: a bridge is a door
  into the sandbox and must not be reachable from the network.

Bridges are keyed by ``(owner, host_port)``.  *owner* is opaque here; the
backend passes the VM's RuntimeId, which keeps a rebuilt VM's bridges distinct
from a dead one's.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Opens the guest-side stream for one bridged connection.
Dial = Callable[[], socket.socket]

_lock = threading.Lock()
_listeners: dict[tuple[str, int], socket.socket] = {}


def pipe(src: socket.socket, dst: socket.socket) -> None:
    """Copy *src* to *dst* until EOF, then half-close *dst*.

    Half-closing rather than closing is what lets a protocol whose client
    signals end-of-request by shutting down its send side (HTTP/1.0, git
    upload-pack) still read the response back.
    """
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


def _handle(conn: socket.socket, dial: Dial) -> None:
    try:
        upstream = dial()
    except Exception:
        # Any dial failure — the guest listener not up yet, the VM gone — must
        # close the accepted connection and leave the listener serving, so the
        # client sees one refused request rather than a hang.
        logger.debug("HCS bridge dial failed", exc_info=True)
        conn.close()
        return
    t = threading.Thread(target=pipe, args=(conn, upstream), daemon=True)
    t.start()
    pipe(upstream, conn)
    conn.close()
    upstream.close()


def _accept(srv: socket.socket, dial: Dial) -> None:
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(conn, dial), daemon=True).start()


def open_bridge(owner: str, host_port: int, dial: Dial) -> None:
    """Bind ``127.0.0.1:host_port`` and bridge each connection through *dial*.

    Idempotent per ``(owner, host_port)``.  Propagates the :class:`OSError` when
    the port cannot be bound — there is deliberately no ``SO_REUSEADDR``, since
    a bridge that quietly bound a port another listener already owns would hand
    the caller an endpoint reaching the wrong service.
    """
    key = (owner, host_port)
    with _lock:
        if key in _listeners:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", host_port))
            srv.listen(64)
        except OSError:
            srv.close()
            raise
        threading.Thread(target=_accept, args=(srv, dial), daemon=True).start()
        _listeners[key] = srv
    logger.info(
        "HCS host→guest bridge listening on 127.0.0.1:%d for %s",
        host_port, owner,
    )


def close_bridge(owner: str, host_port: int) -> bool:
    """Close one bridge listener; ``True`` when one was live."""
    with _lock:
        srv = _listeners.pop((owner, host_port), None)
    return close_listener(srv)


def close_owner_bridges(owner: str) -> None:
    """Close every bridge listener belonging to *owner*."""
    with _lock:
        victims = [
            (key, srv) for key, srv in _listeners.items() if key[0] == owner
        ]
        for key, _srv in victims:
            _listeners.pop(key, None)
    for key, srv in victims:
        close_listener(srv)
        logger.info("Closed HCS host→guest bridge on 127.0.0.1:%d", key[1])


def bridged_host_ports(owner: str) -> list[int]:
    """Host ports *owner* currently has bridge listeners on."""
    with _lock:
        return sorted(port for own, port in _listeners if own == owner)


def close_listener(srv: socket.socket | None) -> bool:
    """Retire an accept-loop listener; ``True`` when there was one.

    ``shutdown`` before ``close`` is what makes the blocked ``accept`` return —
    a bare ``close`` leaves the accept syscall holding the socket, so the port
    would stay bound and the loop's thread would leak.  Connections already
    piped through the listener end on their own.
    """
    if srv is None:
        return False
    try:
        srv.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        srv.close()
    except OSError:
        pass
    return True
