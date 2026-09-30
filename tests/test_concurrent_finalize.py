"""Two finalizers on one draft send its text once.

A permission callback finalizes the draft from its own task.  A background
agent's tool call can reach one while the stream is still sending the turn's
last message, and before the lock both read the buffer ahead of either
clearing it: the same answer landed twice, back to back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend.types import ResultMessage, TextDeltaEvent
from open_shrimp.stream import _DraftState, finalize_and_reset, stream_response


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every body sent as a real message; each send yields to other tasks."""
    bodies: list[str] = []

    async def slow_send_rich(bot: Any, chat_id: int, text: str, **kwargs: Any) -> Any:
        await asyncio.sleep(0.01)
        bodies.append(text)
        return SimpleNamespace(message_id=len(bodies))

    monkeypatch.setattr("open_shrimp.stream.send_rich", slow_send_rich)
    return bodies


@pytest.mark.asyncio
async def test_two_out_of_band_finalizers_send_once(sent: list[str]) -> None:
    state = _DraftState(chat_id=1)
    state.append_gfm("The probe is running.")

    await asyncio.gather(
        finalize_and_reset(AsyncMock(), state),
        finalize_and_reset(AsyncMock(), state),
    )

    assert sent == ["The probe is running."]


@pytest.mark.asyncio
async def test_a_callback_finalizing_during_the_turn_end_send(
    sent: list[str],
) -> None:
    state = _DraftState(chat_id=1)

    async def events() -> Any:
        yield TextDeltaEvent(text="The probe is running.")
        yield ResultMessage(session_id="s")

    async def callback() -> None:
        # Lands while the turn-end send is waiting on Telegram.
        while not state.finalize_lock.locked():
            await asyncio.sleep(0)
        await finalize_and_reset(AsyncMock(), state)

    await asyncio.gather(
        stream_response(
            bot=AsyncMock(), chat_id=1, events=events(), draft_state=state,
        ),
        callback(),
    )

    assert sent == ["The probe is running."]
