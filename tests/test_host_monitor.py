"""Tests for the host-side streaming monitor registry.

Exercises the load-bearing SDK-Monitor behaviours ported into
``host_monitor``: one event per coalesced window, suppression counting,
flood auto-stop, timeout vs persistent lifecycle, and scope teardown.
Dispatch is captured by monkeypatching the shared registry callback.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from open_shrimp import dispatch_registry, host_monitor
from open_shrimp.db import ChatScope
from open_shrimp.handlers.state import is_task_active
from open_shrimp.terminal.log_source import transient_task_output_path

SCOPE = ChatScope(chat_id=42, thread_id=None)


@pytest.fixture
def events(monkeypatch):
    """Capture every dispatched event's text."""
    captured: list[str] = []

    async def fake_dispatch(text, chat_id, thread_id=None):
        captured.append(text)

    monkeypatch.setattr(dispatch_registry, "dispatch", fake_dispatch)
    return captured


@pytest.fixture(autouse=True)
def _fast_windows(monkeypatch):
    monkeypatch.setattr(host_monitor, "THROTTLE_WINDOW_S", 0.05)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    # Synchronous teardown: kill any leaked process groups directly and clear
    # the registry so state never bleeds between tests.
    for mon in list(host_monitor._monitors.values()):
        try:
            os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if mon.output_fh is not None:
            try:
                mon.output_fh.close()
            except OSError:
                pass
        if mon.output_path is not None:
            mon.output_path.unlink(missing_ok=True)
    host_monitor._monitors.clear()
    host_monitor._monitors_by_scope.clear()

    from open_shrimp.handlers.state import _active_bg_tasks

    _active_bg_tasks.clear()


async def _wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _drain(mon, timeout=5.0):
    """Wait for the reader loop to finish and the monitor to deregister."""
    assert await _wait_until(
        lambda: mon.monitor_id not in host_monitor._monitors, timeout,
    )


@pytest.mark.asyncio
async def test_each_line_becomes_event(events):
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="printf 'alpha\\nbeta\\ngamma\\n'",
        description="lines",
        cwd="/tmp",
        timeout_ms=5_000,
        persistent=False,
    )
    await _drain(mon)
    blob = "\n".join(events)
    assert "alpha" in blob and "beta" in blob and "gamma" in blob
    assert "<status>completed</status>" in blob


@pytest.mark.asyncio
async def test_suppression_note(events, monkeypatch):
    monkeypatch.setattr(host_monitor, "MAX_PENDING", 2)
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="printf '%s\\n' 1 2 3 4 5 6 7 8 9 10",
        description="flood-a-bit",
        cwd="/tmp",
        timeout_ms=5_000,
        persistent=False,
    )
    await _drain(mon)
    blob = "\n".join(events)
    assert "events suppressed" in blob


@pytest.mark.asyncio
async def test_flood_auto_stop(events, monkeypatch):
    monkeypatch.setattr(host_monitor, "MAX_PENDING", 1)
    monkeypatch.setattr(host_monitor, "FLOOD_WINDOW_S", 0.0)
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        # Emit a burst then keep running so the flush window can fire the
        # flood check before the process exits on its own.
        command="printf '%s\\n' $(seq 1 200); sleep 30",
        description="runaway",
        cwd="/tmp",
        timeout_ms=60_000,
        persistent=False,
    )
    await _drain(mon)
    blob = "\n".join(events)
    assert "<status>flood</status>" in blob
    assert mon.stopped is True
    assert host_monitor.list_monitors(SCOPE) == []


@pytest.mark.asyncio
async def test_timeout_stops_and_notifies(events):
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="sleep 30",
        description="idle",
        cwd="/tmp",
        timeout_ms=120,  # fires quickly; no clamping at this layer
        persistent=False,
    )
    await _drain(mon)
    blob = "\n".join(events)
    assert "timed out" in blob


@pytest.mark.asyncio
async def test_persistent_has_no_timeout(events):
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="sleep 30",
        description="watcher",
        cwd="/tmp",
        timeout_ms=100,
        persistent=True,
    )
    assert mon.timeout_handle is None
    assert host_monitor.list_monitors(SCOPE) == [mon]
    assert await host_monitor.stop_monitor(mon.monitor_id) is True
    await _drain(mon)


@pytest.mark.asyncio
async def test_scope_limit(monkeypatch):
    monkeypatch.setattr(host_monitor, "MAX_MONITORS_PER_SCOPE", 1)
    await host_monitor.start_monitor(
        scope=SCOPE, command="sleep 30", description="one",
        cwd="/tmp", timeout_ms=60_000, persistent=True,
    )
    with pytest.raises(host_monitor.HostMonitorLimitError):
        await host_monitor.start_monitor(
            scope=SCOPE, command="sleep 30", description="two",
            cwd="/tmp", timeout_ms=60_000, persistent=True,
        )
    # Reap within the loop so the subprocess transport closes cleanly.
    await host_monitor.stop_scope_monitors(SCOPE)


@pytest.mark.asyncio
async def test_stop_scope_monitors_kills_all(monkeypatch):
    monkeypatch.setattr(host_monitor, "MAX_MONITORS_PER_SCOPE", 5)
    for i in range(3):
        await host_monitor.start_monitor(
            scope=SCOPE, command="sleep 30", description=f"m{i}",
            cwd="/tmp", timeout_ms=60_000, persistent=True,
        )
    assert len(host_monitor.list_monitors(SCOPE)) == 3
    stopped = await host_monitor.stop_scope_monitors(SCOPE)
    assert stopped == 3
    assert await _wait_until(lambda: not host_monitor.list_monitors(SCOPE))


@pytest.mark.asyncio
async def test_stop_unknown_monitor_returns_false():
    assert await host_monitor.stop_monitor("deadbeef") is False


# --- SDK-parity presentation: tee file, registry, on_end, envelope ----------


@pytest.mark.asyncio
async def test_output_file_tees_every_line(events, monkeypatch):
    # Force suppression so the chat gets fewer lines than the file.
    monkeypatch.setattr(host_monitor, "MAX_PENDING", 2)
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="printf '%s\\n' l1 l2 l3 l4 l5",
        description="tee",
        cwd="/tmp",
        timeout_ms=5_000,
        persistent=False,
    )
    path = transient_task_output_path(host_monitor.TASK_PROJECT, mon.monitor_id)
    assert mon.output_path == path
    await _drain(mon)
    try:
        content = path.read_text(encoding="utf-8")
        for expected in ("l1", "l2", "l3", "l4", "l5"):
            assert f"{expected}\n" in content
        assert mon.output_fh is None  # closed by _finalize
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_transient_task_registry_lifecycle(events):
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="sleep 30",
        description="registered",
        cwd="/tmp",
        timeout_ms=60_000,
        persistent=True,
    )
    assert is_task_active(mon.monitor_id) is True
    assert await host_monitor.stop_monitor(mon.monitor_id) is True
    assert is_task_active(mon.monitor_id) is False
    assert mon.output_fh is None
    if mon.output_path is not None:
        mon.output_path.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "timeout_ms", "persistent", "stop_via", "expected_reason"),
    [
        ("printf 'x\\n'", 5_000, False, None, "ended"),
        ("sleep 30", 120, False, None, "timeout"),
        ("sleep 30", 60_000, True, "stop_monitor", "stopped"),
        ("sleep 30", 60_000, True, "stop_scope", "stopped"),
    ],
)
async def test_on_end_fires_once_with_reason(
    events, command, timeout_ms, persistent, stop_via, expected_reason,
):
    reasons: list[str] = []

    async def on_end(reason: str) -> None:
        reasons.append(reason)

    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command=command,
        description="on-end",
        cwd="/tmp",
        timeout_ms=timeout_ms,
        persistent=persistent,
        on_end=on_end,
    )
    if stop_via == "stop_monitor":
        assert await host_monitor.stop_monitor(mon.monitor_id) is True
    elif stop_via == "stop_scope":
        await host_monitor.stop_scope_monitors(SCOPE)
    await _drain(mon)
    assert await _wait_until(lambda: bool(reasons))
    assert reasons == [expected_reason]
    if mon.output_path is not None:
        mon.output_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_on_end_flood_reason(events, monkeypatch):
    monkeypatch.setattr(host_monitor, "MAX_PENDING", 1)
    monkeypatch.setattr(host_monitor, "FLOOD_WINDOW_S", 0.0)
    reasons: list[str] = []

    async def on_end(reason: str) -> None:
        reasons.append(reason)

    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="printf '%s\\n' $(seq 1 200); sleep 30",
        description="flood-end",
        cwd="/tmp",
        timeout_ms=60_000,
        persistent=False,
        on_end=on_end,
    )
    await _drain(mon)
    assert await _wait_until(lambda: bool(reasons))
    assert reasons == ["flood"]
    if mon.output_path is not None:
        mon.output_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_event_envelope_format(events):
    mon = await host_monitor.start_monitor(
        scope=SCOPE,
        command="printf 'hello\\n'",
        description="envelope",
        cwd="/tmp",
        timeout_ms=5_000,
        persistent=False,
    )
    await _drain(mon)
    event = next(e for e in events if "<event>" in e)
    assert event.startswith("<task-notification>")
    assert f"<task-id>{mon.monitor_id}</task-id>" in event
    assert 'Monitor event: "envelope"' in event
    assert "hello" in event

    end = next(e for e in events if "<status>" in e)
    assert f"<task-id>{mon.monitor_id}</task-id>" in end
    assert "<status>completed</status>" in end
    assert f"<output-file>{mon.output_path}</output-file>" in end
    if mon.output_path is not None:
        mon.output_path.unlink(missing_ok=True)
