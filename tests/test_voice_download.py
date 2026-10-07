"""Tests for _download_telegram_voice's timeout retry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, TimedOut

from open_shrimp.handlers.messages import (
    _VOICE_READ_TIMEOUT,
    _download_telegram_voice,
)


def _message() -> SimpleNamespace:
    return SimpleNamespace(voice=SimpleNamespace(file_id="f1"), video_note=None)


def _file(data: bytes) -> MagicMock:
    file = MagicMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    return file


@pytest.mark.asyncio
async def test_retries_once_after_get_file_timeout() -> None:
    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=[TimedOut(), _file(b"ogg")])

    assert await _download_telegram_voice(_message(), bot) == b"ogg"
    assert bot.get_file.await_count == 2
    bot.get_file.assert_awaited_with("f1", read_timeout=_VOICE_READ_TIMEOUT)


@pytest.mark.asyncio
async def test_raises_after_second_timeout() -> None:
    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=[TimedOut(), TimedOut()])

    with pytest.raises(TimedOut):
        await _download_telegram_voice(_message(), bot)
    assert bot.get_file.await_count == 2


@pytest.mark.asyncio
async def test_does_not_retry_other_telegram_errors() -> None:
    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=BadRequest("File is too big"))

    with pytest.raises(BadRequest):
        await _download_telegram_voice(_message(), bot)
    assert bot.get_file.await_count == 1


@pytest.mark.asyncio
async def test_no_voice_returns_none() -> None:
    bot = MagicMock()
    bot.get_file = AsyncMock()

    message = SimpleNamespace(voice=None, video_note=None)
    assert await _download_telegram_voice(message, bot) is None
    bot.get_file.assert_not_awaited()
