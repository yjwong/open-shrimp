from __future__ import annotations

from pathlib import Path

import pytest

from open_shrimp import agent_tasks
from open_shrimp.agent_tasks import AgentBackgroundTask
from open_shrimp.db import ChatScope


def _task(task_id: str = "a1") -> AgentBackgroundTask:
    async def abort() -> None:
        return None

    return AgentBackgroundTask(
        task_id=task_id,
        scope=ChatScope(1, None),
        context_name="ctx",
        parent_session_id="parent",
        child_session_id="child",
        tool_use_id=None,
        description="desc",
        prompt="prompt",
        subagent_type="general",
        started_at=1.0,
        output_path=Path("/tmp") / f"{task_id}.jsonl",
        status="completed",
        abort=abort,
    )


@pytest.mark.asyncio
async def test_submit_parent_notifications_requeues_on_failure() -> None:
    agent_tasks._tasks.clear()
    agent_tasks._pending_notifications.clear()
    task = _task()
    agent_tasks._tasks[task.task_id] = task
    agent_tasks._pending_notifications["parent"] = [(task.task_id, "payload")]

    async def fail_submit(payload: str) -> None:
        assert payload == "payload"
        raise RuntimeError("nope")

    submitted = await agent_tasks.submit_parent_notifications("parent", fail_submit)

    assert submitted == 0
    assert agent_tasks._pending_notifications["parent"] == [(task.task_id, "payload")]
    assert task.injected is False


@pytest.mark.asyncio
async def test_submit_parent_notifications_acks_after_success() -> None:
    agent_tasks._tasks.clear()
    agent_tasks._pending_notifications.clear()
    task = _task("a2")
    agent_tasks._tasks[task.task_id] = task
    agent_tasks._pending_notifications["parent"] = [(task.task_id, "payload")]
    seen: list[str] = []

    async def submit(payload: str) -> None:
        seen.append(payload)

    submitted = await agent_tasks.submit_parent_notifications("parent", submit)

    assert submitted == 1
    assert seen == ["payload"]
    assert task.injected is True
    assert "parent" not in agent_tasks._pending_notifications
