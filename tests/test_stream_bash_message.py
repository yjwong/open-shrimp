"""Tests for the folded Bash card in stream.py.

A Bash call costs a row in the turn's message, not a message: the card opens
in the draft when the agent issues the command and collapses onto its output
in place, so a turn with three commands still sends one message.
"""

from __future__ import annotations

from typing import Any
import pytest

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.types import (
    AssistantMessage,
    ResultMessage,
    TextDeltaEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from open_shrimp.stream import (
    DRAFT_INTERVAL_SECONDS,
    _DraftState,
    _open_bash_card,
    stream_response,
)


class _RecordingBot:
    """A bot that records rich send/edit/draft calls in order.

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
        if endpoint == "sendRichMessageDraft":
            self.calls.append(("draft", recorded))
            return None
        return None

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("plain", kwargs))
        self._next_id += 1
        return type("_Msg", (), {"message_id": self._next_id})()

    @property
    def sends(self) -> list[dict[str, Any]]:
        return [kw for kind, kw in self.calls if kind == "send"]


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
async def test_a_command_and_its_result_cost_one_message() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi")]),
        ResultMessage(session_id="sess-1"),
    )

    assert len(bot.sends) == 1
    card = bot.sends[0]["text"]
    # Collapsed once the result is in: one row, output a tap away.
    assert card.startswith("<details><summary>")
    assert "sleep 10" in card
    assert "hi" in card


@pytest.mark.asyncio
async def test_three_commands_share_one_message() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use("echo one", "t1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="one")]),
        _bash_use("echo two", "t2"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="two")]),
        _bash_use("echo three", "t3"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t3", content="three")]),
        ResultMessage(session_id="sess-1"),
    )

    assert len(bot.sends) == 1
    body = bot.sends[0]["text"]
    for command in ("echo one", "echo two", "echo three"):
        assert command in body
    assert body.count("<details>") == 3


@pytest.mark.asyncio
async def test_the_answer_and_its_commands_share_one_message() -> None:
    """The prose is not cut in two by the command that follows it."""
    bot = _RecordingBot()
    await _run(
        bot,
        TextDeltaEvent(session_id="sess-1", text="Checking the schema."),
        _bash_use("sqlite3 db .schema", "t1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        ResultMessage(session_id="sess-1"),
    )

    assert len(bot.sends) == 1
    body = bot.sends[0]["text"]
    assert "Checking the schema." in body
    assert "sqlite3 db .schema" in body


def test_the_running_command_sits_open_in_the_draft() -> None:
    """A command that takes a minute is on screen at second zero.

    Asserted on the buffer rather than through a stream: the draft flushes on
    a half-second timer, and a stream of canned events is over long before the
    first tick.
    """
    state = _DraftState(chat_id=1)
    _open_bash_card(state, "t1", {"command": "sleep 10"})

    assert state.dirty
    assert state.tool_cards["t1"] == 0
    assert state.buffer[0].text.startswith("<details open>")
    assert "sleep 10" in state.buffer[0].text


@pytest.mark.asyncio
async def test_truncated_output_gets_no_button() -> None:
    """One message, one keyboard — the card carries the tail and stops there."""
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

    assert len(bot.sends) == 1
    assert "…(truncated)" in bot.sends[0]["text"]
    assert bot.sends[0].get("reply_markup") is None


@pytest.mark.asyncio
async def test_empty_output_is_noted_on_the_card() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="")]),
        ResultMessage(session_id="sess-1"),
    )

    assert "No output" in bot.sends[0]["text"]


@pytest.mark.asyncio
async def test_a_command_without_a_result_is_marked_interrupted() -> None:
    bot = _RecordingBot()
    await _run(bot, _bash_use())

    assert len(bot.sends) == 1
    card = bot.sends[0]["text"]
    assert "Interrupted" in card
    # Collapsed, not left hanging open from the invocation.
    assert card.startswith("<details><summary>")


@pytest.mark.asyncio
async def test_result_for_an_unseen_invocation_renders_nothing() -> None:
    """Without the invocation there is no command to caption the output."""
    bot = _RecordingBot()
    await _run(
        bot,
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hi")]),
        ResultMessage(session_id="sess-1"),
    )
    assert bot.sends == []


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
async def test_host_bash_adds_no_row_until_its_result() -> None:
    """The approval prompt shows the command while it waits, so the stream
    adds a card only once there is a result to put in it."""
    bot = _RecordingBot()
    await _run(
        bot,
        _host_bash_use(),
        UserMessage(content=[ToolResultBlock(tool_use_id="h1", content="ok")]),
        ResultMessage(session_id="sess-1"),
    )

    drafts = [kw["text"] for kind, kw in bot.calls if kind == "draft"]
    assert not any("<details open>" in text for text in drafts)

    assert len(bot.sends) == 1
    card = bot.sends[0]["text"]
    assert card.startswith("<details><summary>")
    assert "Restart the bot" in card
    assert "0.0s" in card
    assert "ok" in card


@pytest.mark.asyncio
async def test_interrupted_host_bash_still_gets_a_card() -> None:
    bot = _RecordingBot()
    await _run(bot, _host_bash_use())

    assert len(bot.sends) == 1
    assert "Interrupted" in bot.sends[0]["text"]


@pytest.mark.asyncio
async def test_a_bash_call_adds_no_summary_row() -> None:
    """The card says what the row would, so the row is not added."""
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


def _read_use(path: str = "src/open_shrimp/db.py") -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(id="r1", name="Read", input={"file_path": path}),
        ],
        session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_a_read_stays_a_row() -> None:
    """The row names the file; folding the file under it says it again."""
    bot = _RecordingBot()
    await _run(
        bot,
        _read_use(),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="r1",
                    content="\n".join(f"{i}: line" for i in range(200)),
                ),
            ],
        ),
        ResultMessage(session_id="sess-1"),
    )

    body = bot.sends[0]["text"]
    assert "db.py" in body
    assert "<details>" not in body
    assert "199: line" not in body


@pytest.mark.asyncio
async def test_a_failed_read_folds_its_reason() -> None:
    bot = _RecordingBot()
    await _run(
        bot,
        _read_use("/nope.py"),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="r1",
                    content="File does not exist.",
                    is_error=True,
                ),
            ],
        ),
        ResultMessage(session_id="sess-1"),
    )

    body = bot.sends[0]["text"]
    assert "<details>" in body
    assert "File does not exist." in body


@pytest.mark.asyncio
async def test_reasoning_trails_the_rows_it_follows() -> None:
    """Reasoning between two tool calls is newer than the opening paragraph."""
    bot = _RecordingBot()
    state = _DraftState(chat_id=1)
    state.thinking = "Now checking the second file."
    state.append_gfm("Starting the survey.")
    state.tool_cards["t1"] = state.append_rich("<details><summary>row</summary>x</details>")

    from open_shrimp.stream import _send_draft

    await _send_draft(bot, state)

    body = [kw["text"] for kind, kw in bot.calls if kind == "draft"][0]
    assert body.index("Starting the survey.") < body.index("<tg-thinking>")
    assert body.index("<details>") < body.index("<tg-thinking>")


@pytest.mark.asyncio
async def test_reasoning_across_a_tool_call_keeps_a_paragraph_break() -> None:
    """ThinkingDeltaEvent carries no block boundary; the tool call is one."""
    import asyncio

    from open_shrimp.backend.types import ThinkingDeltaEvent

    bot = _RecordingBot()

    async def events() -> Any:
        yield ThinkingDeltaEvent(session_id="sess-1", text="Checked independently.")
        yield _bash_use("ls", "t1")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")])
        yield ThinkingDeltaEvent(session_id="sess-1", text="I'll gather the manifest.")
        # Reasoning reaches a draft and nowhere else, so the flush has to
        # happen while the stream is still open.
        await asyncio.sleep(DRAFT_INTERVAL_SECONDS * 2)
        yield ResultMessage(session_id="sess-1")

    await stream_response(
        events=events(), bot=bot, chat_id=1, policy=ClaudeSdkPolicy(),
    )

    drafts = [kw["text"] for kind, kw in bot.calls if kind == "draft"]
    thinking = [text for text in drafts if "<tg-thinking>" in text]
    assert thinking, drafts
    body = thinking[-1]
    assert "Checked independently." in body
    assert "I'll gather the manifest." in body
    assert "independently.I'll" not in body
