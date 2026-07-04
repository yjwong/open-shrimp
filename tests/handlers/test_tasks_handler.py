"""Tests for ``/tasks`` host-monitor integration.

Host monitors register as transient tasks (``task_type="host_monitor"``),
so ``/tasks`` must list each running monitor exactly once (no bespoke
"Host monitors" section) and route ``/tasks stop <id>`` for a
host_monitor-typed task through ``host_monitor.stop_monitor`` rather than
the CLI ``stop_background_task`` registry.
"""

from __future__ import annotations

import os
import signal
from typing import Any

import pytest

from open_shrimp import dispatch_registry, host_monitor
from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)
from open_shrimp.db import ChatScope
from open_shrimp.handlers.commands import tasks_handler
from open_shrimp.handlers.state import _active_bg_tasks

CHAT_ID = 100
SCOPE = ChatScope(chat_id=CHAT_ID, thread_id=None)


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="test",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(),
    )


class _StubMessage:
    def __init__(self, text: str) -> None:
        self.chat_id = CHAT_ID
        self.message_thread_id = None
        self.text = text
        self.replies: list[str] = []

    async def reply_text(
        self, text: str, parse_mode: str | None = None, **_: Any
    ) -> None:
        self.replies.append(text)


class _StubUpdate:
    def __init__(self, text: str) -> None:
        self.effective_message = _StubMessage(text)
        self.effective_user = type("U", (), {"id": 1})()


class _StubContext:
    def __init__(self) -> None:
        self.bot_data = {"config": _config()}


@pytest.fixture(autouse=True)
def _quiet_dispatch(monkeypatch):
    async def fake_dispatch(text, chat_id, thread_id=None):
        pass

    monkeypatch.setattr(dispatch_registry, "dispatch", fake_dispatch)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
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
    _active_bg_tasks.clear()


async def _arm() -> host_monitor.HostMonitor:
    return await host_monitor.start_monitor(
        scope=SCOPE,
        command="sleep 30",
        description="watching build",
        cwd="/tmp",
        timeout_ms=60_000,
        persistent=True,
    )


@pytest.mark.asyncio
async def test_running_monitor_listed_exactly_once():
    mon = await _arm()
    update = _StubUpdate("/tasks")
    await tasks_handler(update, _StubContext())

    reply = "\n".join(update.effective_message.replies)
    assert reply.count(mon.monitor_id) == 1
    assert "host\\_monitor" in reply
    assert "Host monitors" not in reply  # bespoke section is gone
    # Reap within the loop so the subprocess transport closes cleanly.
    await host_monitor.stop_scope_monitors(SCOPE)


@pytest.mark.asyncio
async def test_stop_routes_to_host_monitor():
    mon = await _arm()
    update = _StubUpdate(f"/tasks stop {mon.monitor_id}")
    await tasks_handler(update, _StubContext())

    reply = "\n".join(update.effective_message.replies)
    assert "Stopped host monitor" in reply
    assert mon.monitor_id not in host_monitor._monitors
    assert _active_bg_tasks.get(SCOPE, {}).get(mon.monitor_id) is None


@pytest.mark.asyncio
async def test_stopped_monitor_disappears_from_list():
    mon = await _arm()
    await host_monitor.stop_monitor(mon.monitor_id)
    update = _StubUpdate("/tasks")
    await tasks_handler(update, _StubContext())

    reply = "\n".join(update.effective_message.replies)
    assert "No active background tasks" in reply
