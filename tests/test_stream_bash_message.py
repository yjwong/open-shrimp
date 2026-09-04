"""Tests for the two-phase Bash message in stream.py.

The header goes out when the agent issues the command and is edited in place
when the result arrives, so a slow command is visible while it runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.types import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from open_shrimp.stream import stream_response


class _RecordingBot:
    """A bot that records send/edit calls in the order they were made."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 100
        self.do_api_request = AsyncMock()

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("send", kwargs))
        self._next_id += 1
        return type("_Msg", (), {"message_id": self._next_id})()

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.calls.append(("edit", kwargs))
        return None


async def _events(*items: Any) -> Any:
    for item in items:
        yield item


def _bash_use(command: str = "sleep 10", tool_id: str = "t1") -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(id=tool_id, name="Bash", input={"command": command}),
        ],
        session_id="sess-1",
    )


async def _run(bot: _RecordingBot, *events: Any) -> None:
    await stream_response(
        events=_events(*events),
        bot=bot,
        chat_id=1,
        policy=ClaudeSdkPolicy(),
    )


@pytest.mark.asyncio
async def test_header_sent_before_the_result_arrives() -> None:
    bot = _RecordingBot()
    await _run(bot, _bash_use())

    # The invocation alone produced a message: the command is on screen
    # while it runs, not only once it finishes.
    assert [kind for kind, _ in bot.calls[:1]] == ["send"]
    assert "sleep 10" in bot.calls[0][1]["text"]
    # No button yet — nothing has been produced to show.
    assert bot.calls[0][1].get("reply_markup") is None


@pytest.mark.asyncio
async def test_result_edits_the_header_in_place() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi")]),
        ResultMessage(session_id="sess-1"),
    )

    assert [kind for kind, _ in bot.calls] == ["send", "edit"]
    sent, edited = bot.calls[0][1], bot.calls[1][1]
    assert edited["message_id"] == 101
    assert edited["chat_id"] == sent["chat_id"]
    # The edit attaches "Show output" rather than posting a second message.
    keyboard = edited["reply_markup"]
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].callback_data.startswith("show_bash:")


@pytest.mark.asyncio
async def test_empty_output_is_noted_on_the_header() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="")]),
        ResultMessage(session_id="sess-1"),
    )

    assert [kind for kind, _ in bot.calls] == ["send", "edit"]
    assert "No output" in bot.calls[1][1]["text"]
    assert bot.calls[1][1]["reply_markup"] is None


@pytest.mark.asyncio
async def test_header_without_a_result_is_marked_interrupted() -> None:
    bot = _RecordingBot()
    await _run(bot, _bash_use())

    assert [kind for kind, _ in bot.calls] == ["send", "edit"]
    assert "Interrupted" in bot.calls[1][1]["text"]


@pytest.mark.asyncio
async def test_result_for_an_unseen_invocation_renders_nothing() -> None:
    """Without the invocation there is no command to caption the output."""
    bot = _RecordingBot()
    await stream_response(
        events=_events(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="hi")],
            ),
            ResultMessage(session_id="sess-1"),
        ),
        bot=bot,
        chat_id=1,
        policy=ClaudeSdkPolicy(),
    )
    assert bot.calls == []


@pytest.mark.asyncio
async def test_failed_header_send_falls_back_to_one_message() -> None:
    """A dropped header must not cost the user the output button."""
    bot = _RecordingBot()
    real_send = bot.send_message
    calls = {"n": 0}

    async def flaky_send(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Telegram said no")
        return await real_send(**kwargs)

    bot.send_message = flaky_send  # type: ignore[method-assign]

    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi")]),
        ResultMessage(session_id="sess-1"),
    )

    # The header never landed, so the result is posted rather than edited.
    assert [kind for kind, _ in bot.calls] == ["send"]
    assert "sleep 10" in bot.calls[0][1]["text"]
    assert bot.calls[0][1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_each_command_gets_its_own_message() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use("echo one", "t1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="one")]),
        _bash_use("echo two", "t2"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="two")]),
        ResultMessage(session_id="sess-1"),
    )

    assert [kind for kind, _ in bot.calls] == ["send", "edit", "send", "edit"]
    assert "echo one" in bot.calls[0][1]["text"]
    assert bot.calls[1][1]["message_id"] == 101
    assert "echo two" in bot.calls[2][1]["text"]
    assert bot.calls[3][1]["message_id"] == 102
