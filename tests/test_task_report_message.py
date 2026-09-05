"""What a finished background task posts into the chat.

``TaskStartedMessage`` finalizes the message its "🤖 Agent" row sits in, so by
the time the report lands there is no card left to fold it into and the
notification renders its own.  These tests pin what ``stream.py`` owns: which
description the card is built from, and one message per chunk.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.types import (
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
)
from open_shrimp.db import ChatScope
from open_shrimp.stream import stream_response


async def _events(*items: Any) -> Any:
    for item in items:
        yield item


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every body handed to ``send_rich``, in send order."""
    bodies: list[str] = []

    async def fake_send_rich(bot: Any, chat_id: int, text: str, **kwargs: Any) -> Any:
        bodies.append(text)
        return None

    monkeypatch.setattr("open_shrimp.stream.send_rich", fake_send_rich)
    return bodies


async def _run(summary: str | None) -> None:
    events = [
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="task-1",
            tool_use_id="call-1",
            description="Review efficiency",
            task_type="local_agent",
            session_id="sess-1",
        ),
        TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-1",
            tool_use_id="call-1",
            status="completed",
            summary=summary,
            session_id="sess-1",
        ),
        ResultMessage(session_id="sess-1"),
    ]
    await stream_response(
        bot=AsyncMock(),
        chat_id=1,
        events=_events(*events),
        scope=ChatScope(chat_id=1, thread_id=None),
        policy=ClaudeSdkPolicy(),
    )


@pytest.mark.asyncio
async def test_the_card_is_built_from_the_started_description(
    sent: list[str],
) -> None:
    await _run("## Findings\n\n`store.waiting` mirrors the column.")

    assert sent[0] == "⏳ Review efficiency"
    assert sent[1].startswith("<details><summary>📋 **Review efficiency** — ")
    assert "### Findings" in sent[1]


@pytest.mark.asyncio
async def test_a_report_over_the_message_limit_spans_messages(
    sent: list[str],
) -> None:
    await _run("word " * 12000)

    assert len(sent) > 2, "the report should span several messages"
    assert sent[1].endswith("</details>")
    assert sent[2].startswith("<details><summary>📋 **Review efficiency**")
    assert "(cont.)" in sent[2]
