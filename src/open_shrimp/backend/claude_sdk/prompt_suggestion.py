"""SDK-side plumbing for prompt-suggestion buttons.

The Claude Code CLI emits a ``prompt_suggestion`` frame on stream-json
stdout a short while after each turn's ``result`` frame.  The frame
predicts what the user is likely to type next and is normally surfaced
in the CLI as the input field's placeholder text (Tab to accept).

The Python ``claude-agent-sdk`` (0.1.65) does not expose this:
  * ``ClaudeAgentOptions`` has no ``promptSuggestions`` field, so the
    flag never reaches the CLI's initialize control request and the
    feature stays off.
  * ``parse_message`` drops unknown message types, so even if the CLI
    did emit a suggestion the SDK consumer would never see it.

This module patches both gaps without forking the SDK:
  1. ``Query._send_control_request`` is wrapped so the ``initialize``
     request gains ``promptSuggestions: True``.
  2. ``SubprocessCLITransport.read_messages`` is wrapped so
     ``prompt_suggestion`` frames are diverted to a per-session
     callback registered by :func:`register_handler_for_turn`.

The backend-neutral callback-data store (the short-id round-trip used
to ferry suggestions through Telegram's 64-byte ``callback_data``
limit) lives in :mod:`open_shrimp.prompt_suggestion`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from open_shrimp.prompt_suggestion import CALLBACK_PREFIX, store_suggestion

logger = logging.getLogger(__name__)

# Type of a suggestion handler: receives the suggestion text and runs
# whatever side effect the consumer wants (e.g. edit a Telegram message
# to add an inline button).  Async so the consumer can await Telegram.
SuggestionHandler = Callable[[str], Awaitable[None]]


# session_id -> handler.  Last-write wins: each new turn for a given
# session replaces the previous handler so a stale suggestion can never
# fire against a message the user has already moved past.
_handlers: dict[str, SuggestionHandler] = {}
_HANDLERS_MAX = 1000


def _evict_if_full(d: dict[str, Any]) -> None:
    if len(d) > _HANDLERS_MAX:
        for k in list(d.keys())[: _HANDLERS_MAX // 2]:
            d.pop(k, None)


def register_handler(session_id: str, handler: SuggestionHandler) -> None:
    """Register *handler* to receive the next suggestion for *session_id*.

    Called by :func:`register_handler_for_turn` after a turn completes,
    with a closure that knows which Telegram message to edit. Replacing
    an existing handler is fine and expected — only the latest turn's
    suggestion is interesting.
    """
    _evict_if_full(_handlers)
    _handlers[session_id] = handler


def unregister_handler(session_id: str) -> None:
    _handlers.pop(session_id, None)


# ---------------------------------------------------------------------------
# SDK monkey-patches
# ---------------------------------------------------------------------------


_patches_applied = False


def install_patches() -> None:
    """Patch the SDK once.  Idempotent — safe to call from multiple modules.

    The CLI ships with prompt-suggestion on by default; the host may
    still opt out by setting ``CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION``
    to a falsy value, which the CLI honours independently of these
    patches.
    """
    global _patches_applied
    if _patches_applied:
        return

    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    # ------- patch 1: inject promptSuggestions into initialize request -------
    _orig_send_control = Query._send_control_request

    async def _patched_send_control(
        self: Any, request: dict[str, Any], timeout: float | None = None
    ) -> Any:
        if request.get("subtype") == "initialize":
            request["promptSuggestions"] = True
            logger.debug("injected promptSuggestions=True into init request")
        # The SDK's signature accepts an optional timeout; preserve it.
        if timeout is None:
            return await _orig_send_control(self, request)
        return await _orig_send_control(self, request, timeout=timeout)

    Query._send_control_request = _patched_send_control  # type: ignore[method-assign]

    # ------- patch 2: divert prompt_suggestion frames to handlers -------
    _orig_read = SubprocessCLITransport.read_messages

    async def _patched_read(self: Any) -> Any:
        async for msg in _orig_read(self):
            if isinstance(msg, dict) and msg.get("type") == "prompt_suggestion":
                session_id = msg.get("session_id")
                suggestion = msg.get("suggestion")
                if session_id and suggestion:
                    handler = _handlers.pop(session_id, None)
                    if handler is not None:
                        import asyncio

                        asyncio.create_task(_run_handler(handler, suggestion))
            # Yield every frame downstream — the SDK parser drops
            # unknown types as forward-compat, but yielding (instead
            # of skipping) keeps the iterator's flow identical to the
            # unpatched path.
            yield msg

    SubprocessCLITransport.read_messages = _patched_read  # type: ignore[method-assign]

    _patches_applied = True
    logger.info("prompt_suggestion patches installed")


async def _run_handler(handler: SuggestionHandler, suggestion: str) -> None:
    try:
        await handler(suggestion)
    except Exception:
        logger.exception("prompt_suggestion handler failed")


# ---------------------------------------------------------------------------
# Per-turn handler factory (the body of ClaudeSdkBackend.on_turn_end)
# ---------------------------------------------------------------------------


def register_handler_for_turn(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    session_id: str,
) -> None:
    """Arrange for the next ``prompt_suggestion`` frame to add an inline button.

    The CLI emits a ``prompt_suggestion`` frame asynchronously after the
    result frame.  When it arrives, :func:`_patched_read` pops the
    registered handler and runs it; the handler edits ``message_id``
    (the last message of the just-finished turn) to add a single inline
    button labelled with the suggestion.  Tapping the button dispatches
    the suggestion text as the user's next message.
    """

    async def handler(suggestion: str) -> None:
        suggestion = suggestion.strip()
        if not suggestion:
            return
        suggest_id = store_suggestion(suggestion)
        # Telegram caps button labels at 64 bytes UTF-8; truncate with
        # an ellipsis to stay safely under.
        encoded = suggestion.encode("utf-8")
        if len(encoded) > 60:
            label = encoded[:57].decode("utf-8", errors="ignore") + "…"
        else:
            label = suggestion
        button = InlineKeyboardButton(
            f"💡 {label}", callback_data=f"{CALLBACK_PREFIX}{suggest_id}",
        )
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup([[button]]),
            )
        except BadRequest as e:
            # Common: "message is not modified" if a previous edit
            # already attached a keyboard, or "message to edit not
            # found" if the user deleted the message.  Both are benign.
            logger.debug(
                "Skipped suggestion edit for chat %d msg %d: %s",
                chat_id, message_id, e,
            )
        except Exception:
            logger.exception(
                "Failed to attach suggestion button for chat %d msg %d",
                chat_id, message_id,
            )

    register_handler(session_id, handler)


__all__ = [
    "SuggestionHandler",
    "install_patches",
    "register_handler",
    "register_handler_for_turn",
    "unregister_handler",
]
