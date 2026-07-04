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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

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
    #: Full-stream tee: every stdout line is appended here unthrottled, so the
    #: Terminal Mini App can tail the complete output like an SDK task file.
    output_path: Path | None = None
    output_fh: TextIO | None = None
    #: Presenter hook fired exactly once at end of life with the reason
    #: ("ended" | "timeout" | "flood" | "stopped").
    on_end: Callable[[str], Awaitable[None]] | None = None
    _finalized: bool = False


#: ``project`` segment for :func:`transient_task_output_path` and the
#: ``task_type`` under which monitors register in ``_active_bg_tasks``.
TASK_PROJECT = "host_monitor"
TASK_TYPE = "host_monitor"

_monitors: dict[str, HostMonitor] = {}
_monitors_by_scope: dict[ChatScope, set[str]] = {}


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
    """One coalesced event, in the SDK Monitor's ``<task-notification>`` shape."""
    parts: list[str] = []
    if suppressed:
        parts.append(
            f"[{suppressed} events suppressed — output rate too high. "
            f"Consider host_monitor_stop {mon.monitor_id} and re-arm with a "
            f"tighter grep --line-buffered / awk filter.]"
        )
    if lines:
        parts.append("\n".join(lines))
    event = "\n".join(parts)
    return (
        "<task-notification>\n"
        f"<task-id>{mon.monitor_id}</task-id>\n"
        f'<summary>Monitor event: "{mon.description}"</summary>\n'
        f"<event>{event}</event>\n"
        "</task-notification>"
    )


def _end_message(mon: HostMonitor, status: str, detail: str) -> str:
    """The end-of-stream notification, mimicking the SDK completion envelope."""
    parts = [
        "<task-notification>",
        f"<task-id>{mon.monitor_id}</task-id>",
    ]
    if mon.output_path is not None:
        parts.append(f"<output-file>{mon.output_path}</output-file>")
    parts.extend([
        f"<status>{status}</status>",
        f'<summary>Monitor "{mon.description}" stream ended — {detail}</summary>',
        "</task-notification>",
    ])
    return "\n".join(parts)


# --- Coalescing + throttle reader loop --------------------------------------


def _on_line(mon: HostMonitor, line: str) -> None:
    if mon.stopped:
        return
    # Tee the full stream to the output file before any throttle bookkeeping:
    # the file gets everything, the chat gets coalesced events (SDK parity).
    if mon.output_fh is not None:
        try:
            mon.output_fh.write(line + "\n")
            mon.output_fh.flush()
        except OSError:
            pass
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
        await _dispatch(mon, _end_message(
            mon, "flood",
            "stopped, too much output. Re-arm host_monitor with a command "
            "that filters more aggressively; pipe through "
            "grep --line-buffered / awk so only the lines you care about "
            "become events.",
        ))
        await _stop(mon, reason="flood")
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
        mon, _end_message(mon, "completed", f"process exited (code {rc})."),
    )
    _deregister(mon)
    await _finalize(mon, "ended")


# --- Lifecycle: reap, kill, stop --------------------------------------------


async def _finalize(mon: HostMonitor, reason: str) -> None:
    """One-shot presentation cleanup: close the tee, unregister, fire on_end.

    Called from both terminal paths (``_natural_exit`` and ``_stop``) so it
    runs exactly once regardless of which wins the race.  Best-effort — a
    presentation failure must never break monitor teardown.
    """
    if mon._finalized:
        return
    mon._finalized = True
    if mon.output_fh is not None:
        try:
            mon.output_fh.close()
        except OSError:
            pass
        mon.output_fh = None
    try:
        from open_shrimp.handlers.state import unregister_transient_task

        unregister_transient_task(mon.scope, mon.monitor_id)
    except Exception:
        logger.debug(
            "host_monitor %s unregister failed", mon.monitor_id, exc_info=True,
        )
    if mon.on_end is not None:
        try:
            await mon.on_end(reason)
        except Exception:
            logger.debug(
                "host_monitor %s on_end failed", mon.monitor_id, exc_info=True,
            )


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


async def _stop(mon: HostMonitor, reason: str = "stopped") -> None:
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
    await _finalize(mon, reason)


def _on_timeout(mon: HostMonitor) -> None:
    mon.timeout_handle = None
    asyncio.create_task(_timeout_stop(mon))


async def _timeout_stop(mon: HostMonitor) -> None:
    if mon.stopped:
        return
    await _dispatch(
        mon, _end_message(
            mon, "timeout",
            "timed out; re-arm host_monitor if you still need it.",
        ),
    )
    await _stop(mon, reason="timeout")


# --- Public API -------------------------------------------------------------


async def start_monitor(
    *,
    scope: ChatScope,
    command: str,
    description: str,
    cwd: str,
    timeout_ms: int,
    persistent: bool,
    on_end: Callable[[str], Awaitable[None]] | None = None,
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
        on_end=on_end,
    )
    _register(mon)

    # Presentation plumbing (tee file + transient-task registry) is
    # best-effort: mirror _ProgressSink — never let it break the monitor.
    try:
        from open_shrimp.terminal.log_source import transient_task_output_path

        path = transient_task_output_path(TASK_PROJECT, mon.monitor_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        mon.output_fh = open(path, "a", encoding="utf-8")
        mon.output_path = path
    except OSError:
        logger.debug(
            "host_monitor %s could not open output tee", mon.monitor_id,
            exc_info=True,
        )
    try:
        from open_shrimp.handlers.state import register_transient_task

        register_transient_task(
            scope, mon.monitor_id,
            description=description, task_type=TASK_TYPE,
        )
    except Exception:
        logger.debug(
            "host_monitor %s could not register transient task",
            mon.monitor_id, exc_info=True,
        )

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
