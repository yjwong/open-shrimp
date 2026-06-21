"""Lookup order for the assistant-error rendering site in ``stream.py``.

``_handle_assistant_error`` looks up the rendered message body in three
layers, in priority order:

1. The active backend's ``BackendCopy.assistant_error_messages``.
2. The shared neutral defaults in ``stream.py``
   (``_DEFAULT_ASSISTANT_ERROR_MESSAGES`` — ``rate_limit`` / ``unknown``).
3. The generic ``⚠️ Error: <code>`` fallback.

Asserted here so future refactors of that lookup stay vendor-neutral.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp.backend import BackendCopy
from open_shrimp.stream import (
    _DEFAULT_ASSISTANT_ERROR_MESSAGES,
    _DraftState,
    _handle_assistant_error,
)


def _bot() -> Any:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


def _state() -> _DraftState:
    return _DraftState(chat_id=1)


def _sent_text(bot: Any) -> str:
    """The MarkdownV2-escaped body the bot was asked to send."""
    bot.send_message.assert_awaited_once()
    return bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_backend_override_wins_over_default() -> None:
    bot = _bot()
    copy = BackendCopy(
        assistant_error_messages={"rate_limit": "BACKENDOVERRIDE"},
    )
    await _handle_assistant_error(bot, _state(), "rate_limit", copy=copy)
    assert "BACKENDOVERRIDE" in _sent_text(bot)


@pytest.mark.asyncio
async def test_falls_back_to_shared_default_when_backend_silent() -> None:
    bot = _bot()
    copy = BackendCopy(assistant_error_messages={})
    await _handle_assistant_error(bot, _state(), "rate_limit", copy=copy)
    # Markdown bolding escapes ``*`` in the rendered text, so match on the
    # neutral phrasing rather than the GFM form.
    assert "Rate limited" in _sent_text(bot)


@pytest.mark.asyncio
async def test_falls_back_to_generic_for_unknown_code() -> None:
    bot = _bot()
    await _handle_assistant_error(
        bot, _state(), "novelcode", copy=None,
    )
    sent = _sent_text(bot)
    assert "novelcode" in sent
    # No vendor names ever leak through the generic fallback.
    assert "Claude" not in sent
    assert "Anthropic" not in sent


@pytest.mark.asyncio
async def test_neutral_defaults_table_has_no_vendor_names() -> None:
    """The shared defaults table must stay backend-neutral — that is the
    whole reason the vendor-named entries moved to ``BackendCopy``."""
    for code, body in _DEFAULT_ASSISTANT_ERROR_MESSAGES.items():
        assert "Claude" not in body, code
        assert "Anthropic" not in body, code
