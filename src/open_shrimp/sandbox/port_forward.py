"""Shared helpers for runtime port forwarding from sandboxes to the host.

Backends spawn a long-lived ``ssh -L`` (or similar) subprocess per forward
and track it via :class:`PortForwardRegistry`.  Each forward is keyed by
an opaque ``scope_key`` (typically derived from :class:`ChatScope`) so
that ``/clear`` in one Telegram conversation tears down only its own
forwards.

Host ports are always bound to ``127.0.0.1``.  The agent may request a
specific host port (typically matching the guest port); if it's taken,
:func:`allocate_host_port` falls back to a kernel-assigned free port.
"""

from __future__ import annotations

import logging
import secrets
import socket
import subprocess
import threading
from dataclasses import dataclass

from open_shrimp.sandbox.base import PortForward

logger = logging.getLogger(__name__)

# ssh keeps the tunnel alive but exits fast on bind failure.  These two
# options are shared across libvirt and lima tunnels.
SSH_TUNNEL_OPTS: tuple[str, ...] = (
    "-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
)


def is_host_port_available(port: int) -> bool:
    """Return ``True`` if *port* can be bound on ``127.0.0.1``.

    Probes without ``SO_REUSEADDR`` so the result matches the bind
    semantics that ``ssh -L`` will use a moment later — otherwise a
    port in ``TIME_WAIT`` would look free here and then fail in ssh.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_free_host_port() -> int:
    """Ask the kernel for a free TCP port on ``127.0.0.1``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def allocate_host_port(requested: int | None, guest_port: int) -> int:
    """Pick a host port, preferring *requested* (or *guest_port* if None).

    Falls back to a kernel-assigned port if the preferred one is taken.
    The returned port is not reserved — the caller must bind it
    promptly.
    """
    preferred = requested if requested is not None else guest_port
    if 0 < preferred < 65536 and is_host_port_available(preferred):
        return preferred
    return pick_free_host_port()


def new_forward_id() -> str:
    """Return a short opaque identifier for a forward."""
    return "pf-" + secrets.token_hex(4)


def open_ssh_tunnel(
    cmd: list[str],
    *,
    guest_port: int,
    host_port: int,
    scope_key: str | None,
    description: str | None,
    registry: PortForwardRegistry,
    env: dict[str, str] | None = None,
) -> PortForward:
    """Spawn an ``ssh -L`` tunnel and register it.

    *cmd* is the full argv for ``ssh`` including all backend-specific
    options and the trailing ``-L 127.0.0.1:host:127.0.0.1:guest``.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    # Bind failure / auth refusal exits in ~50 ms via
    # ``ExitOnForwardFailure=yes`` — 0.5 s is plenty.
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    else:
        err = (proc.stderr.read() if proc.stderr else b"").decode(
            errors="replace"
        ).strip()
        raise RuntimeError(
            f"ssh -L tunnel exited immediately (rc={proc.returncode}): "
            f"{err or 'no stderr'}"
        )

    forward = PortForward(
        id=new_forward_id(),
        guest_port=guest_port,
        host_port=host_port,
        scope_key=scope_key,
        description=description,
    )
    registry.register(forward, proc)
    logger.info(
        "Opened port forward %s: guest=%d -> host 127.0.0.1:%d (pid=%d)",
        forward.id, guest_port, host_port, proc.pid,
    )
    return forward


@dataclass
class _Entry:
    forward: PortForward
    proc: subprocess.Popen[bytes]


class PortForwardRegistry:
    """Thread-safe registry of live forwards owned by a single sandbox.

    Backends instantiate one of these and route their port-forward
    methods through it.  The registry owns the subprocess handles and
    terminates them on removal.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def register(
        self, forward: PortForward, proc: subprocess.Popen[bytes],
    ) -> None:
        self._reap_dead()
        with self._lock:
            self._entries[forward.id] = _Entry(forward=forward, proc=proc)

    def remove(self, forward_id: str) -> bool:
        self._reap_dead()
        with self._lock:
            entry = self._entries.pop(forward_id, None)
        if entry is None:
            return False
        _terminate(entry.proc)
        logger.info(
            "Removed port forward %s (guest=%d host=%d)",
            entry.forward.id,
            entry.forward.guest_port, entry.forward.host_port,
        )
        return True

    def list(self, scope_key: str | None = None) -> list[PortForward]:
        self._reap_dead()
        with self._lock:
            entries = list(self._entries.values())
        return [
            e.forward for e in entries
            if scope_key is None or e.forward.scope_key == scope_key
        ]

    def cleanup(self, scope_key: str | None = None) -> None:
        with self._lock:
            if scope_key is None:
                victims = list(self._entries.values())
                self._entries.clear()
            else:
                victims = [
                    e for e in self._entries.values()
                    if e.forward.scope_key == scope_key
                ]
                for e in victims:
                    self._entries.pop(e.forward.id, None)
        # SIGTERM all first so wait deadlines run in parallel.
        for entry in victims:
            if entry.proc.poll() is None:
                try:
                    entry.proc.terminate()
                except Exception:
                    logger.debug(
                        "terminate() failed for forward %s",
                        entry.forward.id, exc_info=True,
                    )
        for entry in victims:
            _wait_terminated(entry.proc)
            logger.info(
                "Cleaned up port forward %s (guest=%d host=%d)",
                entry.forward.id,
                entry.forward.guest_port, entry.forward.host_port,
            )

    def _reap_dead(self) -> None:
        with self._lock:
            dead = [
                fid for fid, e in self._entries.items()
                if e.proc.poll() is not None
            ]
            for fid in dead:
                self._entries.pop(fid, None)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except (OSError, ProcessLookupError):
        logger.debug("terminate() failed", exc_info=True)
        return
    _wait_terminated(proc)


def _wait_terminated(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, ProcessLookupError):
            logger.debug("kill() failed", exc_info=True)
