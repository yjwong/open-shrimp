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
    host_monitor._monitors.clear()
    host_monitor._monitors_by_scope.clear()


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
    assert "Monitor ended" in blob


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
    assert "Monitor stopped" in blob
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
