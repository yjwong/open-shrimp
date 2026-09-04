"""The question overlay, as it rides the FCM data payload."""

from __future__ import annotations

import json
from typing import Any

import pytest

from open_shrimp import agent_status
from open_shrimp.agent_status import encode_question_options, notify_agent_status
from open_shrimp.config import AndroidCompanionConfig, Config, TelegramConfig
from open_shrimp.db import ChatScope


def test_options_keep_label_and_description() -> None:
    encoded = json.loads(
        encode_question_options(
            [
                {"label": "Merge", "description": "Keep both histories"},
                {"label": "Rebase", "description": "Replay onto master"},
            ]
        )
    )
    assert encoded == [
        {"label": "Merge", "description": "Keep both histories"},
        {"label": "Rebase", "description": "Replay onto master"},
    ]


def test_missing_description_encodes_as_empty() -> None:
    encoded = json.loads(encode_question_options([{"label": "Merge"}]))
    assert encoded == [{"label": "Merge", "description": ""}]


def test_oversized_payload_drops_descriptions_before_options() -> None:
    """Every option survives; only its text is allowed to shrink.

    The phone answers with a position in this list, so a dropped option is
    not a smaller notification — it is one the user may have wanted and can
    never choose from the shade.
    """
    options = [
        {"label": f"Option {i}", "description": "x" * 200} for i in range(12)
    ]
    encoded = json.loads(encode_question_options(options))

    assert len(encoded) == len(options)
    assert [e["label"] for e in encoded] == [o["label"] for o in options]
    assert all(e["description"] == "" for e in encoded)


def test_long_options_shrink_into_the_budget() -> None:
    options = [{"label": "L" * 400, "description": "d" * 400} for _ in range(30)]
    payload = encode_question_options(options)

    assert len(payload.encode("utf-8")) <= 2048
    assert len(json.loads(payload)) == 30


# ---------------------------------------------------------------------------
# The overlay on the wire: one field says a wait is happening, and it names it
# ---------------------------------------------------------------------------


class _CapturingSender:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.high_priority = False

    async def send_agent_status(
        self, *, device: dict[str, Any], data: dict[str, str],
        high_priority: bool = False,
    ) -> None:
        self.data = data
        self.high_priority = high_priority


async def _push(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _CapturingSender:
    sender = _CapturingSender()
    monkeypatch.setattr(
        agent_status, "list_active_android_push_devices",
        lambda _db: _devices(),
    )
    monkeypatch.setattr(agent_status, "get_push_sender", lambda *_: sender)
    config = Config(
        telegram=TelegramConfig(token="t"),
        allowed_users=[1],
        contexts={},
        android_companion=AndroidCompanionConfig(push_provider="fcm"),
    )
    await notify_agent_status(
        {"bot_username": "bot"}, config, None, ChatScope(chat_id=7),
        "running", title="ctx", **kwargs,
    )
    return sender


async def _devices() -> list[dict[str, str]]:
    return [{"push_provider": "fcm", "device_id": "d"}]


@pytest.mark.asyncio
async def test_question_overlay_names_its_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = await _push(
        monkeypatch,
        text="Which one?",
        awaiting_kind="question",
        awaiting_id="q1",
        question_options=[{"label": "A"}, {"label": "B"}],
        multi_select=True,
    )

    assert sender.data["awaiting_kind"] == "question"
    assert sender.data["awaiting_id"] == "q1"
    assert sender.data["multi_select"] == "1"
    assert [o["label"] for o in json.loads(sender.data["question_options"])] == ["A", "B"]
    # A wait on the human is time-sensitive; the OS must not defer it.
    assert sender.high_priority is True


@pytest.mark.asyncio
async def test_approval_overlay_carries_no_question_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = await _push(
        monkeypatch,
        awaiting_kind="approval", awaiting_id="tool-1", tool_name="Bash",
    )

    assert sender.data["awaiting_kind"] == "approval"
    assert sender.data["awaiting_id"] == "tool-1"
    assert "question_options" not in sender.data
    assert "multi_select" not in sender.data


@pytest.mark.asyncio
async def test_plain_running_push_says_nothing_is_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``awaiting_kind`` absent is the only signal the wait is over.

    The phone reads it to decide whether to draw actions at all, so a cleared
    overlay that still carried an id would leave buttons resolving nothing.
    """
    sender = await _push(monkeypatch)

    assert "awaiting_kind" not in sender.data
    assert "awaiting_id" not in sender.data
    assert sender.high_priority is False
