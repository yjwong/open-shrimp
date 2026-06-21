"""Telegram-side callback store for prompt-suggestion buttons.

A prompt suggestion is a short string of predicted next-user text that
some backends surface as an inline button on the last finalized
Telegram message. Tapping the button dispatches the suggestion as the
user's next message.

Suggestion text can exceed Telegram's 64-byte ``callback_data`` limit,
so the producer stashes the text via :func:`store_suggestion` and only
the short opaque id rides in ``callback_data``; the consumer pops it
via :func:`pop_suggestion` when the button is tapped.

Both ends live here because they are backend-neutral — they are a
Telegram protocol concern, not a Claude/OpenCode concern. The
backend-specific producer plumbing (the SDK monkeypatches that
intercept the CLI's ``prompt_suggestion`` JSONL frame, the per-turn
handler closure that knows which message to edit) lives under the
backend adapter at ``backend/claude_sdk/prompt_suggestion.py``.
"""

from __future__ import annotations

import secrets
from typing import Any

# Inline keyboard callback_data prefix used by both the producer
# (backend/claude_sdk/prompt_suggestion.py builds the button) and the
# consumer (bot.py routes the tap).
CALLBACK_PREFIX = "suggest:"


# Short callback-id -> suggestion text.  Suggestions can exceed
# Telegram's 64-byte callback_data limit, so we round-trip via this
# dict.  Bounded; entries are popped on use, and old entries are
# evicted when the dict grows past _STORE_MAX.
_suggestion_store: dict[str, str] = {}
_STORE_MAX = 1000


def _evict_if_full(d: dict[str, Any]) -> None:
    """Drop the oldest half of *d* once it exceeds _STORE_MAX.

    Keeps unbounded growth in check for long-running bots where some
    callback buttons are never tapped (so suggestions linger).
    """
    if len(d) > _STORE_MAX:
        for k in list(d.keys())[: _STORE_MAX // 2]:
            d.pop(k, None)


def store_suggestion(text: str) -> str:
    """Stash *text* and return a short opaque id for callback_data.

    The id stays inside the ``CALLBACK_PREFIX<id>`` callback_data
    envelope and is popped by :func:`pop_suggestion` when the user
    taps the button.
    """
    _evict_if_full(_suggestion_store)
    suggest_id = secrets.token_urlsafe(8)
    _suggestion_store[suggest_id] = text
    return suggest_id


def pop_suggestion(suggest_id: str) -> str | None:
    return _suggestion_store.pop(suggest_id, None)


__all__ = [
    "CALLBACK_PREFIX",
    "pop_suggestion",
    "store_suggestion",
]
