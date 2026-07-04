"""Host-side streaming monitors — the streaming analog of ``host_bash``.

A ``host_monitor`` runs a long-running shell command on the HOST (outside the
sandbox) and delivers **each stdout line as an event** into the agent's session
via :func:`open_shrimp.dispatch_registry.dispatch`.  The behavioural contract is
copied from the Claude Agent SDK's built-in Monitor tool: each line is one
event, a token-bucket-style throttle coalesces bursts within a window, a
suppression counter folds over-budget lines into a note, and a sustained flood
auto-stops the monitor and tells the model to re-arm with a tighter filter.

Unlike the SDK Monitor (which runs inside the CLI against its own task
registry), a host-side process is invisible to the CLI's ``TaskStop`` registry,
so this module owns the full lifecycle: spawn, throttle, timeout, flood-kill,
process-exit, and teardown on session close.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from dataclasses import dataclass, field

from open_shrimp import dispatch_registry
from open_shrimp.db import ChatScope

logger = logging.getLogger(__name__)

# --- Tunables ---------------------------------------------------------------

#: Throttle window ``W``: at most one dispatch per window.
THROTTLE_WINDOW_S = 1.0
#: Flood window ``H``: sustained suppression beyond this auto-stops the monitor.
FLOOD_WINDOW_S = 10.0
#: Max lines buffered within a window before further lines count as suppressed.
MAX_PENDING = 50
#: Max concurrent monitors per ChatScope (per-turn cost bounds this).
MAX_MONITORS_PER_SCOPE = 3

#: Lifecycle timeout bounds (ms), mirroring the SDK Monitor.
DEFAULT_TIMEOUT_MS = 300_000
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 3_600_000


class HostMonitorLimitError(RuntimeError):
    """Raised when a scope already has the maximum number of monitors."""


@dataclass
class HostMonitor:
    """A single running host monitor and its coalescing/throttle state."""

    monitor_id: str
    scope: ChatScope
    description: str
    command: str
    proc: asyncio.subprocess.Process
    loop: asyncio.AbstractEventLoop
    persistent: bool = False
    reader_task: asyncio.Task | None = None
    timeout_handle: asyncio.TimerHandle | None = None
    flush_handle: asyncio.TimerHandle | None = None
    #: Lines accumulated within the current throttle window.
    pending: list[str] = field(default_factory=list)
    #: Over-budget lines counted, not emitted, within the current window.
    suppressed: int = 0
    #: Monotonic time suppression began (None when not suppressing), used to
    #: detect a sustained flood past ``FLOOD_WINDOW_S``.
    suppress_started: float | None = None
    stopped: bool = False


_monitors: dict[str, HostMonitor] = {}
_monitors_by_scope: dict[ChatScope, set[str]] = {}


def _header(mon: HostMonitor) -> str:
    return f"[host_monitor {mon.monitor_id}] {mon.description}"


# --- Registry bookkeeping ---------------------------------------------------


def _register(mon: HostMonitor) -> None:
    _monitors[mon.monitor_id] = mon
    _monitors_by_scope.setdefault(mon.scope, set()).add(mon.monitor_id)


def _deregister(mon: HostMonitor) -> None:
    _monitors.pop(mon.monitor_id, None)
    ids = _monitors_by_scope.get(mon.scope)
    if ids is not None:
        ids.discard(mon.monitor_id)
        if not ids:
            _monitors_by_scope.pop(mon.scope, None)


def list_monitors(scope: ChatScope) -> list[HostMonitor]:
    """Return the running monitors for *scope* (for the ``/tasks`` handler)."""
    ids = _monitors_by_scope.get(scope, set())
    return [_monitors[i] for i in list(ids) if i in _monitors]


# --- Dispatch ---------------------------------------------------------------


async def _dispatch(mon: HostMonitor, text: str) -> None:
    """Push one event into the agent's session for this monitor's scope.

    Routes through the shared dispatch registry so ``_dispatch_to_agent``
    decides mid-turn-inject vs. fresh-turn vs. setup-queue.  Never raises —
    an event racing session teardown is dropped cleanly.
    """
    try:
        await dispatch_registry.dispatch(
            text, mon.scope.chat_id, mon.scope.thread_id,
        )
    except Exception:
        logger.warning(
            "host_monitor %s dispatch failed (event dropped)",
            mon.monitor_id, exc_info=True,
        )


def _event_message(mon: HostMonitor, lines: list[str], suppressed: int) -> str:
    parts = [_header(mon)]
    if suppressed:
        parts.append(
            f"[{suppressed} events suppressed — output rate too high. "
            f"Consider host_monitor_stop {mon.monitor_id} and re-arm with a "
            f"tighter grep --line-buffered / awk filter.]"
        )
    if lines:
        parts.append("\n".join(lines))
    return "\n".join(parts)


def _flood_message(mon: HostMonitor) -> str:
    return (
        f"{_header(mon)}\n"
        "[Monitor stopped — too much output. Re-arm host_monitor with a "
        "command that filters more aggressively; pipe through "
        "grep --line-buffered / awk so only the lines you care about become "
        "events.]"
    )


# --- Coalescing + throttle reader loop --------------------------------------


def _on_line(mon: HostMonitor, line: str) -> None:
    if mon.stopped:
        return
    if len(mon.pending) < MAX_PENDING:
        mon.pending.append(line)
    else:
        mon.suppressed += 1
        if mon.suppress_started is None:
            mon.suppress_started = mon.loop.time()
    if mon.flush_handle is None:
        mon.flush_handle = mon.loop.call_later(
            THROTTLE_WINDOW_S, lambda: _schedule_flush(mon),
        )


def _schedule_flush(mon: HostMonitor) -> None:
    mon.flush_handle = None
    asyncio.create_task(_flush(mon))


async def _flush(mon: HostMonitor, *, final: bool = False) -> None:
    if mon.stopped and not final:
        return
    lines = mon.pending
    suppressed = mon.suppressed
    mon.pending = []
    mon.suppressed = 0
    if not lines and not suppressed:
        return

    # Sustained flood past the hard window: stop and tell the model to re-arm.
    if (
        mon.suppress_started is not None
        and (mon.loop.time() - mon.suppress_started) > FLOOD_WINDOW_S
    ):
        await _dispatch(mon, _flood_message(mon))
        await _stop(mon)
        return

    if suppressed == 0:
        mon.suppress_started = None
    await _dispatch(mon, _event_message(mon, lines, suppressed))


async def _reader_loop(mon: HostMonitor) -> None:
    assert mon.proc.stdout is not None
    try:
        async for raw in mon.proc.stdout:
            if mon.stopped:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            _on_line(mon, line)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "host_monitor %s reader loop error", mon.monitor_id, exc_info=True,
        )
    # stdout hit EOF.  A forced stop (_stop) owns its own teardown, so only the
    # natural-exit path runs here.
    if not mon.stopped:
        await _natural_exit(mon)


async def _natural_exit(mon: HostMonitor) -> None:
    """The process exited on its own: flush the tail, reap, notify, deregister."""
    if mon.flush_handle is not None:
        mon.flush_handle.cancel()
        mon.flush_handle = None
    await _flush(mon, final=True)
    rc = await _reap(mon)
    # A _stop may have raced us across the awaits above; if so it owns teardown.
    if mon.stopped:
        return
    mon.stopped = True
    if mon.timeout_handle is not None:
        mon.timeout_handle.cancel()
        mon.timeout_handle = None
    await _dispatch(
        mon, f"{_header(mon)}\n[Monitor ended — process exited (code {rc}).]",
    )
    _deregister(mon)


# --- Lifecycle: reap, kill, stop --------------------------------------------


def _kill_proc(mon: HostMonitor) -> None:
    try:
        os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _reap(mon: HostMonitor) -> int | None:
    """Wait for the child to exit so it doesn't linger as a zombie."""
    try:
        return await asyncio.wait_for(mon.proc.wait(), timeout=2)
    except Exception:
        return mon.proc.returncode


async def _stop(mon: HostMonitor) -> None:
    """Authoritative teardown: mark stopped, cancel timers, kill, reap, drop.

    Owns every step so a monitor never leaks even when the killed process's
    stdout never reaches EOF (e.g. a grandchild inherited the pipe).  The
    reader loop, seeing ``stopped``, skips its own natural-exit teardown.
    """
    if mon.stopped:
        return
    mon.stopped = True
    if mon.timeout_handle is not None:
        mon.timeout_handle.cancel()
        mon.timeout_handle = None
    if mon.flush_handle is not None:
        mon.flush_handle.cancel()
        mon.flush_handle = None
    _kill_proc(mon)
    if mon.reader_task is not None and mon.reader_task is not asyncio.current_task():
        mon.reader_task.cancel()
    await _reap(mon)
    _deregister(mon)


def _on_timeout(mon: HostMonitor) -> None:
    mon.timeout_handle = None
    asyncio.create_task(_timeout_stop(mon))


async def _timeout_stop(mon: HostMonitor) -> None:
    if mon.stopped:
        return
    await _dispatch(
        mon,
        f"{_header(mon)}\n[Monitor timed out — re-arm host_monitor if you "
        "still need it.]",
    )
    await _stop(mon)


# --- Public API -------------------------------------------------------------


async def start_monitor(
    *,
    scope: ChatScope,
    command: str,
    description: str,
    cwd: str,
    timeout_ms: int,
    persistent: bool,
) -> HostMonitor:
    """Spawn a host command, register it, and begin streaming its lines.

    Raises :class:`HostMonitorLimitError` if *scope* already has the maximum
    number of concurrent monitors.
    """
    if len(_monitors_by_scope.get(scope, set())) >= MAX_MONITORS_PER_SCOPE:
        raise HostMonitorLimitError(
            f"scope already has {MAX_MONITORS_PER_SCOPE} monitors; stop one "
            "with host_monitor_stop first."
        )

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    loop = asyncio.get_running_loop()
    mon = HostMonitor(
        monitor_id=uuid.uuid4().hex[:8],
        scope=scope,
        description=description,
        command=command,
        proc=proc,
        loop=loop,
        persistent=persistent,
    )
    _register(mon)
    mon.reader_task = asyncio.create_task(_reader_loop(mon))
    if not persistent:
        mon.timeout_handle = loop.call_later(
            timeout_ms / 1000, lambda: _on_timeout(mon),
        )
    logger.warning(
        "host_monitor %s armed (persistent=%s): %s",
        mon.monitor_id, persistent, command[:200],
    )
    return mon


async def stop_monitor(monitor_id: str) -> bool:
    """Stop a monitor by id. Returns False if no such active monitor."""
    mon = _monitors.get(monitor_id)
    if mon is None:
        return False
    await _stop(mon)
    return True


async def stop_scope_monitors(scope: ChatScope) -> int:
    """Stop every monitor for *scope* (session close / clear / shutdown)."""
    ids = list(_monitors_by_scope.get(scope, set()))
    for mid in ids:
        await stop_monitor(mid)
    return len(ids)
