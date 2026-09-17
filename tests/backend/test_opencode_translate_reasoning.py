"""Translation tests for the OpenCode adapter's reasoning-part handling.

Reasoning parts share the ``message.part.delta``/``field:"text"`` wire shape
with real text parts; the adapter learns the type from
``message.part.updated`` and must route matching deltas to
``ThinkingDeltaEvent`` (draft-only) instead of ``TextDeltaEvent``, with a
paragraph break between back-to-back reasoning parts and no re-emission from
the completed part.
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


def _reasoning_open(part_id: str) -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"type": "reasoning", "id": part_id},
        },
    }


def _delta(part_id: str, text: str) -> dict:
    return {
        "type": "message.part.delta",
        "properties": {
            "sessionID": "sess-1",
            "partID": part_id,
            "field": "text",
            "delta": text,
        },
    }


def _text_part_open(part_id: str) -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"type": "text", "id": part_id},
        },
    }


def _tool_part(status: str, **state_extra) -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call-1",
                "state": {"status": status, **state_extra},
            },
        },
    }


_IDLE = {"type": "session.idle", "properties": {"sessionID": "sess-1"}}


async def _collect(events: list[dict]) -> list[bt.Message]:
    queue = _FakeQueue(events)
    out: list[bt.Message] = []
    async for msg in _iter_response(queue, "sess-1", None, None, None):
        out.append(msg)
    return out


def _thinking_texts(out: list[bt.Message]) -> list[str]:
    return [m.text for m in out if isinstance(m, bt.ThinkingDeltaEvent)]


def _text_delta_texts(out: list[bt.Message]) -> list[str]:
    return [m.text for m in out if isinstance(m, bt.TextDeltaEvent)]


@pytest.mark.asyncio
async def test_reasoning_deltas_become_thinking_events():
    out = await _collect([
        _reasoning_open("r-1"),
        _delta("r-1", "weighing "),
        _delta("r-1", "two approaches"),
        _IDLE,
    ])
    assert "".join(_thinking_texts(out)) == "weighing two approaches"
    assert _text_delta_texts(out) == []
    texts = [
        b.text
        for m in out if isinstance(m, bt.AssistantMessage)
        for b in m.content if isinstance(b, bt.TextBlock)
    ]
    assert texts == []


@pytest.mark.asyncio
async def test_plain_text_parts_stay_text_events():
    out = await _collect([
        _text_part_open("t-1"),
        _delta("t-1", "the answer"),
        _IDLE,
    ])
    assert _thinking_texts(out) == []
    assert _text_delta_texts(out) == ["the answer"]


@pytest.mark.asyncio
async def test_back_to_back_reasoning_parts_get_a_paragraph_break():
    out = await _collect([
        _reasoning_open("r-1"),
        _delta("r-1", "first block"),
        _reasoning_open("r-2"),
        _delta("r-2", "second block"),
        _IDLE,
    ])
    assert _thinking_texts(out) == [
        "first block", "\n\n", "second block",
    ]


@pytest.mark.asyncio
async def test_text_between_reasoning_parts_suppresses_the_break():
    """Answer text clears the draft's thinking over in stream.py, so a break
    before the next reasoning part would be dead weight."""
    out = await _collect([
        _reasoning_open("r-1"),
        _delta("r-1", "checking"),
        _text_part_open("t-1"),
        _delta("t-1", "the answer"),
        _reasoning_open("r-2"),
        _delta("r-2", "more thinking"),
        _IDLE,
    ])
    assert _thinking_texts(out) == ["checking", "more thinking"]


@pytest.mark.asyncio
async def test_tool_between_reasoning_parts_suppresses_the_break():
    """stream.py ends the reasoning block at the ToolUseBlock row."""
    out = await _collect([
        _reasoning_open("r-1"),
        _delta("r-1", "before"),
        _tool_part("running", input={"command": "ls"}),
        _reasoning_open("r-2"),
        _delta("r-2", "after"),
        _IDLE,
    ])
    assert _thinking_texts(out) == ["before", "after"]


@pytest.mark.asyncio
async def test_completed_reasoning_part_does_not_reemit():
    out = await _collect([
        _reasoning_open("r-1"),
        _delta("r-1", "thought"),
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {
                    "type": "reasoning",
                    "id": "r-1",
                    "text": "thought",
                    "time": {"start": 1, "end": 2},
                },
            },
        },
        _IDLE,
    ])
    assert _thinking_texts(out) == ["thought"]
