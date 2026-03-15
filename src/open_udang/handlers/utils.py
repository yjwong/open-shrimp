"""Shared utility functions used across all handler modules."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from telegram import Bot, Update

from open_udang.config import Config, ContextConfig
from open_udang.db import (
    get_active_context,
    get_pinned_message_id,
    set_active_context,
    set_pinned_message_id,
)
from open_udang.handlers.state import (
    _DEFAULT_CONTEXT_LIMIT,
    _model_overrides,
)

logger = logging.getLogger(__name__)


def _escape_mdv2(text: str) -> str:
    """Escape MarkdownV2 special characters in plain text."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _get_locked_context(chat_id: int, config: Config) -> str | None:
    """Return the context name this chat is locked to, or None."""
    for name, ctx in config.contexts.items():
        if chat_id in ctx.locked_for_chats:
            return name
    return None


async def _get_context_name(chat_id: int, config: Config, db: aiosqlite.Connection) -> str:
    """Get the active context name for a chat (persisted in DB)."""
    # If locked, always use that context regardless of what's saved
    locked = _get_locked_context(chat_id, config)
    if locked:
        await set_active_context(db, chat_id, locked)
        return locked

    saved = await get_active_context(db, chat_id)
    if saved and saved in config.contexts:
        return saved

    # Check if this chat has a default context configured
    for name, ctx in config.contexts.items():
        if chat_id in ctx.default_for_chats:
            await set_active_context(db, chat_id, name)
            return name

    await set_active_context(db, chat_id, config.default_context)
    return config.default_context


async def _get_context(
    chat_id: int, config: Config, db: aiosqlite.Connection
) -> tuple[str, ContextConfig]:
    """Get context name and config for a chat.

    If a per-chat model override is active (via ``/model``), returns a
    shallow copy of the context config with the overridden model.
    """
    name = await _get_context_name(chat_id, config, db)
    ctx = config.contexts[name]
    override = _model_overrides.get(chat_id)
    if override:
        from dataclasses import replace
        ctx = replace(ctx, model=override)
    return name, ctx


def _is_authorized(user_id: int | None, config: Config) -> bool:
    """Check if a user is in the allowlist."""
    return user_id is not None and user_id in config.allowed_users


def _is_bot_addressed(update: Update, bot_username: str) -> bool:
    """Check if the bot is @mentioned or replied to in a group chat.

    In private chats, always returns True.
    """
    message = update.effective_message
    if message is None:
        return False

    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return True

    # Check if replying to the bot
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.username == bot_username:
            return True

    # Check for @mention in entities (text messages) or caption_entities (photos)
    entities = message.entities or message.caption_entities or []
    text = message.text or message.caption or ""
    for entity in entities:
        if entity.type == "mention":
            mention = text[entity.offset : entity.offset + entity.length]
            if mention.lower() == f"@{bot_username.lower()}":
                return True

    return False


def _strip_mention(text: str, bot_username: str) -> str:
    """Remove @bot_username from message text."""
    mention = f"@{bot_username}"
    # Case-insensitive removal
    idx = text.lower().find(mention.lower())
    if idx != -1:
        text = text[:idx] + text[idx + len(mention) :]
    return text.strip()


async def _cancel_running(chat_id: int) -> None:
    """Cancel any running agent task for a chat.

    Sends an interrupt to the persistent CLI client (if any) so it stops
    processing, then cancels the asyncio task.  The persistent client
    stays alive for reuse by the next message.
    """
    import asyncio

    from open_udang.client_manager import get_session
    from open_udang.handlers.state import _running_tasks

    session = get_session(chat_id)
    if session is not None:
        try:
            await session.client.interrupt()
            logger.info("Sent interrupt to CLI for chat %d", chat_id)
        except Exception:
            logger.debug(
                "Failed to send interrupt for chat %d", chat_id, exc_info=True
            )

    task = _running_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Cancelled running task for chat %d", chat_id)


def _format_token_count(count: int) -> str:
    """Format a token count as a human-readable string (e.g. 12.3k, 1.2M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _build_status_text(
    ctx_name: str,
    ctx: ContextConfig,
    usage: dict[str, int] | None = None,
    total_cost_usd: float | None = None,
) -> str:
    """Build the pinned status message text in MarkdownV2."""
    escaped_name = _escape_mdv2(ctx_name)
    escaped_desc = _escape_mdv2(ctx.description)
    escaped_dir = _escape_mdv2(ctx.directory)
    escaped_model = _escape_mdv2(ctx.model)
    lines = [
        f"\U0001f4cc *Active context:* `{escaped_name}`",
        f"{escaped_desc}",
        "",
        f"\U0001f4c1 `{escaped_dir}`",
        f"\U0001f916 `{escaped_model}`",
    ]

    if usage:
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)

        total_tokens = input_tokens + output_tokens + cache_read + cache_creation
        total_str = _escape_mdv2(_format_token_count(total_tokens))
        limit_str = _escape_mdv2(_format_token_count(_DEFAULT_CONTEXT_LIMIT))
        pct = min(total_tokens / _DEFAULT_CONTEXT_LIMIT * 100, 100)
        pct_str = _escape_mdv2(f"{pct:.0f}%")

        lines.append("")
        lines.append(f"\U0001f4ca *Context:* {total_str} / {limit_str} \\({pct_str}\\)")

        if total_cost_usd is not None:
            cost_str = _escape_mdv2(f"${total_cost_usd:.4f}")
            lines.append(f"\U0001f4b0 *Cost:* {cost_str}")

    return "\n".join(lines)


async def _update_pinned_status(
    bot: Bot,
    chat_id: int,
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    usage: dict[str, int] | None = None,
    total_cost_usd: float | None = None,
) -> None:
    """Send or update the pinned status message for a chat."""
    text = _build_status_text(ctx_name, ctx, usage=usage, total_cost_usd=total_cost_usd)
    existing_msg_id = await get_pinned_message_id(db, chat_id)

    # Try to edit the existing pinned message
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                parse_mode="MarkdownV2",
            )
            return
        except Exception:
            logger.debug(
                "Could not edit pinned message %d in chat %d, will send new one",
                existing_msg_id,
                chat_id,
            )

    # Send a new message and pin it
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
        )
        await set_pinned_message_id(db, chat_id, msg.message_id)
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("Failed to send/pin status message in chat %d", chat_id)
