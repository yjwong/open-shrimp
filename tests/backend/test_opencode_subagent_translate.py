"""Translation tests for the OpenCode adapter's subagent (``task``) handling.

The ``task`` tool spawns a child session; the adapter must surface it as a
``TaskStartedMessage`` (lineage keyed on the task ``callID``, ``task_id`` =
child session id) plus the usual ``ToolUseBlock`` row, and emit a
``TaskNotificationMessage`` on completion.
"""

from __future__ import annotations

import pytest

from open_shrimp.backend import types as bt
from open_shrimp.backend.opencode.sse import EventQueueClosed
from open_shrimp.backend.opencode.translate import _iter_response


class _FakeQueue:
    def __init__(self, events: list[dict]) -> None:
        self._events = list(events)

    async def get(self) -> dict:
        if not self._events:
            raise EventQueueClosed
        return self._events.pop(0)


def _task_part(status: str, **state_extra) -> dict:
    state = {"status": status, **state_extra}
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "parent-1",
            "part": {
                "type": "tool",
                "tool": "task",
                "callID": "call-1",
                "state": state,
            },
        },
    }


async def _collect(events: list[dict]) -> list[bt.Message]:
    queue = _FakeQueue(events)
    out: list[bt.Message] = []
    async for msg in _iter_response(queue, "parent-1", None, None, None):
        out.append(msg)
    return out


@pytest.mark.asyncio
async def test_task_part_emits_started_and_notification():
    events = [
        _task_part(
            "running",
            input={
                "description": "summarise the repo",
                "subagent_type": "general",
                "prompt": "go",
            },
            metadata={"sessionId": "child-1"},
        ),
        _task_part(
            "completed",
            output="all done",
            metadata={"sessionId": "child-1"},
        ),
        {"type": "session.idle", "properties": {"sessionID": "parent-1"}},
    ]
    out = await _collect(events)

    tool_uses = [
        b
        for m in out
        if isinstance(m, bt.AssistantMessage)
        for b in m.content
        if isinstance(b, bt.ToolUseBlock)
    ]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "task"
    assert tool_uses[0].id == "call-1"

    started = [m for m in out if isinstance(m, bt.TaskStartedMessage)]
    assert len(started) == 1
    assert started[0].task_id == "child-1"          # /tasks + terminal key
    assert started[0].tool_use_id == "call-1"        # lineage / suppression key
    assert started[0].task_type == "local_agent"
    assert started[0].description == "summarise the repo"
    assert started[0].session_id == "parent-1"

    notifs = [m for m in out if isinstance(m, bt.TaskNotificationMessage)]
    assert len(notifs) == 1
    assert notifs[0].task_id == "child-1"
    assert notifs[0].status == "completed"

    results = [
        b
        for m in out
        if isinstance(m, bt.UserMessage)
        for b in m.content
        if isinstance(b, bt.ToolResultBlock)
    ]
    assert len(results) == 1
    assert results[0].is_error is False


@pytest.mark.asyncio
async def test_task_started_not_emitted_before_child_session_known():
    # A running part with input but no metadata.sessionId yet: the
    # ToolUseBlock row appears, but TaskStarted waits for the child id.
    events = [
        _task_part(
            "running",
            input={"description": "x", "subagent_type": "general"},
        ),
        {"type": "session.idle", "properties": {"sessionID": "parent-1"}},
    ]
    out = await _collect(events)
    assert any(
        isinstance(m, bt.AssistantMessage)
        and any(isinstance(b, bt.ToolUseBlock) for b in m.content)
        for m in out
    )
    assert not any(isinstance(m, bt.TaskStartedMessage) for m in out)


@pytest.mark.asyncio
async def test_task_error_emits_error_notification():
    events = [
        _task_part(
            "running",
            input={"description": "boom", "subagent_type": "general"},
            metadata={"sessionId": "child-9"},
        ),
        _task_part("error", error="kaboom", metadata={"sessionId": "child-9"}),
        {"type": "session.idle", "properties": {"sessionID": "parent-1"}},
    ]
    out = await _collect(events)
    notifs = [m for m in out if isinstance(m, bt.TaskNotificationMessage)]
    assert len(notifs) == 1
    assert notifs[0].status == "error"
    assert notifs[0].task_id == "child-9"
    err_results = [
        b
        for m in out
        if isinstance(m, bt.UserMessage)
        for b in m.content
        if isinstance(b, bt.ToolResultBlock) and b.is_error
    ]
    assert len(err_results) == 1
