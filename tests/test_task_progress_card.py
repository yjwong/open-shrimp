"""A running task's ⏳ card rewritten with the progress its policy renders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.opencode.policy import OpenCodePolicy
from open_shrimp.backend.types import (
    ResultMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
)
from open_shrimp.db import ChatScope
from open_shrimp.stream import stream_response


async def _events(*items: Any) -> Any:
    for item in items:
        yield item


@pytest.fixture
def edits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every body the ⏳ card is rewritten to, in order."""
    bodies: list[str] = []

    async def fake_send_rich(bot: Any, chat_id: int, text: str, **kwargs: Any) -> Any:
        return SimpleNamespace(message_id=77)

    async def fake_edit(
        bot: Any, chat_id: int, message_id: int, text: str, **kwargs: Any,
    ) -> bool:
        assert message_id == 77
        bodies.append(text)
        return True

    monkeypatch.setattr("open_shrimp.stream.send_rich", fake_send_rich)
    monkeypatch.setattr("open_shrimp.stream.edit_rich_unchanged_ok", fake_edit)
    return bodies


def _progress(state: str) -> TaskProgressMessage:
    return TaskProgressMessage(
        subtype="task_progress",
        data={"workflow_progress": [
            {"type": "workflow_phase", "index": 1, "title": "Review"},
            {"type": "workflow_agent", "index": 1, "phaseIndex": 1,
             "label": "review:bugs", "state": state},
        ]},
        task_id="w1",
    )


async def _run(policy: Any, *progress: TaskProgressMessage) -> None:
    await stream_response(
        bot=AsyncMock(),
        chat_id=1,
        events=_events(
            TaskStartedMessage(
                subtype="task_started", data={}, task_id="w1",
                description="review-changes", task_type="local_workflow",
            ),
            *progress,
            TaskNotificationMessage(
                subtype="task_notification", data={}, task_id="w1",
                status="completed", summary="done",
            ),
            ResultMessage(session_id="s"),
        ),
        scope=ChatScope(chat_id=5, thread_id=None),
        policy=policy,
    )


@pytest.mark.asyncio
async def test_progress_rewrites_the_card(edits: list[str]) -> None:
    await _run(ClaudeSdkPolicy(), _progress("start"))

    assert edits == [
        "⏳ review-changes\n\n⏳ **Review** — 0/1 agent done<br>↳ review:bugs",
    ]


@pytest.mark.asyncio
async def test_a_held_back_update_lands_when_the_task_finishes(
    edits: list[str],
) -> None:
    # The second snapshot arrives inside the edit interval, so only the
    # task's end can put it on the card.
    await _run(ClaudeSdkPolicy(), _progress("start"), _progress("done"))

    assert len(edits) == 2
    assert edits[-1].endswith("✅ **Review** — 1/1 agent done")


@pytest.mark.asyncio
async def test_a_policy_without_progress_leaves_the_card_alone(
    edits: list[str],
) -> None:
    await _run(OpenCodePolicy(), _progress("start"))

    assert edits == []
