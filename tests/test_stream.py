from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from open_shrimp.stream import _DraftState, finalize_and_reset


class _BlockingBot:
    def __init__(self) -> None:
        self._message_id = 0
        self.sent_texts: list[str] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self._message_id += 1
        await asyncio.sleep(0)
        self.sent_texts.append(str(kwargs.get("text", "")))
        return SimpleNamespace(message_id=self._message_id)


@pytest.mark.asyncio
async def test_concurrent_finalize_and_reset_sends_buffered_text_once() -> None:
    state = _DraftState(chat_id=1)
    state.raw_text = "Running two parallel ls calls now."
    bot = _BlockingBot()

    await asyncio.wait_for(
        asyncio.gather(
            finalize_and_reset(bot, state),  # type: ignore[arg-type]
            finalize_and_reset(bot, state),  # type: ignore[arg-type]
        ),
        timeout=1,
    )

    assert bot.sent_texts == ["Running two parallel ls calls now\\."]
