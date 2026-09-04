"""Tests for the two-phase Bash message in stream.py.

The header goes out when the agent issues the command and is edited in place
when the result arrives, so a slow command is visible while it runs.
"""

from __future__ import annotations

from typing import Any
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
    """A bot that records rich send/edit calls in the order they were made.

    Every send goes through ``do_api_request``: ``sendRichMessage`` is not in
    python-telegram-bot, so there is no typed method to patch.  The recorded
    kwargs are flattened to the shape the assertions care about — the Markdown
    body and the keyboard.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 100

    async def do_api_request(
        self, endpoint: str, api_kwargs: dict[str, Any], **_: Any,
    ) -> Any:
        recorded = dict(api_kwargs)
        recorded["text"] = api_kwargs.get("rich_message", {}).get("markdown", "")
        if endpoint == "sendRichMessage":
            self.calls.append(("send", recorded))
            self._next_id += 1
            return type("_Msg", (), {"message_id": self._next_id})()
        if endpoint == "editMessageText":
            self.calls.append(("edit", recorded))
            return None
        return None

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("plain", kwargs))
        self._next_id += 1
        return type("_Msg", (), {"message_id": self._next_id})()


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
    # Open while it runs: the command is readable without a tap.
    assert bot.calls[0][1]["text"].startswith("<details open>")


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
    # Collapsed once the result is in: one row, output a tap away.
    assert edited["text"].startswith("<details>")
    assert edited["chat_id"] == sent["chat_id"]
    # Output that fits sits in the card body, so there is nothing to reveal.
    assert "hi" in edited["text"]
    assert edited.get("reply_markup") is None


@pytest.mark.asyncio
async def test_truncated_output_keeps_the_show_output_button() -> None:
    """The card holds the tail; the button reveals what it cut."""
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use(),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content="\n".join(f"line {i}" for i in range(200)),
                ),
            ],
        ),
        ResultMessage(session_id="sess-1"),
    )

    edited = bot.calls[1][1]
    assert "…(truncated)" in edited["text"]
    keyboard = edited["reply_markup"]
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
    assert bot.calls[1][1].get("reply_markup") is None


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
    real_request = bot.do_api_request
    calls = {"n": 0}

    async def flaky_request(endpoint: str, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Telegram said no")
        return await real_request(endpoint, **kwargs)

    bot.do_api_request = flaky_request  # type: ignore[method-assign]

    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi")]),
        ResultMessage(session_id="sess-1"),
    )

    # The header never landed, so the result is posted rather than edited.
    assert [kind for kind, _ in bot.calls] == ["send"]
    assert "sleep 10" in bot.calls[0][1]["text"]
    assert "hi" in bot.calls[0][1]["text"]


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


def _host_bash_use(
    command: str = "systemctl --user restart open-shrimp",
    tool_id: str = "h1",
) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id=tool_id,
                name="mcp__openshrimp__host_bash",
                input={"command": command, "description": "Restart the bot"},
            ),
        ],
        session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_host_bash_costs_one_message() -> None:
    """The approval prompt shows the command while it waits, so the stream
    adds a card only once there is a result to put in it."""
    bot = _RecordingBot()
    await _run(
        bot,
        _host_bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="h1", content="ok")]),
        ResultMessage(session_id="sess-1"),
    )

    assert [kind for kind, _ in bot.calls] == ["send"]
    card = bot.calls[0][1]["text"]
    # Collapsed, and the elapsed time survived the missing header.
    assert card.startswith("<details><summary>")
    assert "Restart the bot" in card
    assert "0.0s" in card
    assert "ok" in card


@pytest.mark.asyncio
async def test_interrupted_host_bash_still_gets_a_card() -> None:
    bot = _RecordingBot()
    await _run(bot, _host_bash_use())

    assert [kind for kind, _ in bot.calls] == ["send"]
    assert "Interrupted" in bot.calls[0][1]["text"]


@pytest.mark.asyncio
async def test_a_bash_call_adds_no_summary_row() -> None:
    """The card says what the row would, so the row is not sent."""
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi")]),
        _host_bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="h1", content="ok")]),
        ResultMessage(session_id="sess-1"),
    )

    assert all("🔧" not in kw.get("text", "") for _, kw in bot.calls)
