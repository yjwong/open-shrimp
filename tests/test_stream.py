"""Tests for stream.py's checklist-update triggers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.opencode.policy import OpenCodePolicy
from open_shrimp.backend.types import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from open_shrimp.stream import stream_response


class _QuietPolicy:
    """A real backend policy with tool notifications suppressed, so tests
    exercise the genuine checklist taxonomy without draft-message traffic."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def suppress_notification(self, tool_name: str) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _events(*items: Any) -> Any:
    for item in items:
        yield item


def _run_args(policy: Any = None) -> dict[str, Any]:
    return {
        "bot": AsyncMock(),
        "chat_id": 1,
        "policy": _QuietPolicy(policy or ClaudeSdkPolicy()),
    }


def _task_update_turn() -> list[Any]:
    """A TaskUpdate call followed by its result and the turn end."""
    return [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="t1",
                    name="TaskUpdate",
                    input={"taskId": "1", "status": "completed"},
                ),
            ],
            session_id="sess-1",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1")]),
        ResultMessage(session_id="sess-1"),
    ]


@pytest.mark.asyncio
async def test_checklist_result_triggers_store_read() -> None:
    reads: list[str] = []

    async def reader(session_id: str) -> list[dict[str, Any]]:
        reads.append(session_id)
        return [{"content": "a", "status": "pending", "activeForm": "a-ing"}]

    updates: list[list[dict[str, Any]]] = []

    async def on_todo_update(todos: list[dict[str, Any]]) -> None:
        updates.append(todos)

    await stream_response(
        events=_events(*_task_update_turn()),
        on_todo_update=on_todo_update,
        checklist_reader=reader,
        **_run_args(),
    )
    # One read at the tool result; the turn-end read returns the same
    # list, so change detection folds it into a single update.
    assert reads == ["sess-1", "sess-1"]
    assert updates == [
        [{"content": "a", "status": "pending", "activeForm": "a-ing"}],
    ]


@pytest.mark.asyncio
async def test_tool_use_alone_does_not_trigger_read() -> None:
    # The trigger sits on the ToolResultBlock (post-execution), not on
    # the ToolUseBlock announcement.
    reads: list[str] = []

    async def reader(session_id: str) -> list[dict[str, Any]]:
        reads.append(session_id)
        return []

    await stream_response(
        events=_events(
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="TaskUpdate", input={}),
                ],
                session_id="sess-1",
            ),
        ),
        on_todo_update=AsyncMock(),
        checklist_reader=reader,
        **_run_args(),
    )
    assert reads == []


@pytest.mark.asyncio
async def test_coalesces_results_in_one_message() -> None:
    reads: list[str] = []

    async def reader(session_id: str) -> list[dict[str, Any]]:
        reads.append(session_id)
        return [{"content": "a", "status": "pending", "activeForm": "a"}]

    await stream_response(
        events=_events(
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="TaskCreate", input={}),
                    ToolUseBlock(id="t2", name="TaskCreate", input={}),
                ],
                session_id="sess-1",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="t1"),
                    ToolResultBlock(tool_use_id="t2"),
                ],
            ),
        ),
        on_todo_update=AsyncMock(),
        checklist_reader=reader,
        **_run_args(),
    )
    assert reads == ["sess-1"]


@pytest.mark.asyncio
async def test_read_only_task_tools_do_not_trigger() -> None:
    reads: list[str] = []

    async def reader(session_id: str) -> list[dict[str, Any]]:
        reads.append(session_id)
        return []

    await stream_response(
        events=_events(
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="TaskList", input={}),
                    ToolUseBlock(id="t2", name="TaskGet", input={"taskId": "1"}),
                ],
                session_id="sess-1",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="t1"),
                    ToolResultBlock(tool_use_id="t2"),
                ],
            ),
        ),
        on_todo_update=AsyncMock(),
        checklist_reader=reader,
        **_run_args(),
    )
    assert reads == []


@pytest.mark.asyncio
async def test_turn_end_skips_empty_store_without_activity() -> None:
    updates: list[list[dict[str, Any]]] = []

    async def reader(session_id: str) -> list[dict[str, Any]]:
        return []

    async def on_todo_update(todos: list[dict[str, Any]]) -> None:
        updates.append(todos)

    await stream_response(
        events=_events(ResultMessage(session_id="sess-1")),
        on_todo_update=on_todo_update,
        checklist_reader=reader,
        **_run_args(),
    )
    assert updates == []


@pytest.mark.asyncio
async def test_turn_end_catches_subagent_writes() -> None:
    # No checklist tool in the main stream (a subagent wrote to the store),
    # but the turn-end read finds a non-empty list and fires the update.
    async def reader(session_id: str) -> list[dict[str, Any]]:
        return [{"content": "sub", "status": "pending", "activeForm": "sub"}]

    updates: list[list[dict[str, Any]]] = []

    async def on_todo_update(todos: list[dict[str, Any]]) -> None:
        updates.append(todos)

    await stream_response(
        events=_events(ResultMessage(session_id="sess-1")),
        on_todo_update=on_todo_update,
        checklist_reader=reader,
        **_run_args(),
    )
    assert updates == [
        [{"content": "sub", "status": "pending", "activeForm": "sub"}],
    ]


@pytest.mark.asyncio
async def test_emptied_checklist_still_pushes_clear() -> None:
    # A turn that ran a checklist tool and ended with an empty store
    # must push the empty list (the agent deleted every task).
    updates: list[list[dict[str, Any]]] = []

    async def reader(session_id: str) -> list[dict[str, Any]]:
        return []

    async def on_todo_update(todos: list[dict[str, Any]]) -> None:
        updates.append(todos)

    await stream_response(
        events=_events(*_task_update_turn()),
        on_todo_update=on_todo_update,
        checklist_reader=reader,
        **_run_args(),
    )
    # Fired once for the tool result; the identical turn-end read dedups.
    assert updates == [[]]


@pytest.mark.asyncio
async def test_snapshot_tool_fires_from_input() -> None:
    # OpenCode's todowrite carries the full list in its input — no reader.
    updates: list[list[dict[str, Any]]] = []

    async def on_todo_update(todos: list[dict[str, Any]]) -> None:
        updates.append(todos)

    todos = [{"content": "x", "status": "pending", "activeForm": "x"}]
    await stream_response(
        events=_events(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1", name="todowrite", input={"todos": todos},
                    ),
                ],
            ),
        ),
        on_todo_update=on_todo_update,
        checklist_reader=None,
        **_run_args(policy=OpenCodePolicy()),
    )
    assert updates == [todos]


@pytest.mark.asyncio
async def test_checklist_tool_without_reader_is_noop() -> None:
    updates: list[list[dict[str, Any]]] = []

    async def on_todo_update(todos: list[dict[str, Any]]) -> None:
        updates.append(todos)

    await stream_response(
        events=_events(*_task_update_turn()),
        on_todo_update=on_todo_update,
        checklist_reader=None,
        **_run_args(),
    )
    assert updates == []
