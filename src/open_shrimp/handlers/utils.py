"""Shared utility functions used across all handler modules."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from telegram import Bot, Message, Update
from telegram.error import BadRequest

from open_shrimp.config import Config, ContextConfig
from open_shrimp.db import (
    ChatScope,
    get_active_context,
    get_pinned_message_id,
    set_active_context,
    set_pinned_message_id,
)
from open_shrimp.handlers.state import (
    _DEFAULT_CONTEXT_LIMIT,
    _additional_dir_cache,
    _effort_overrides,
    _model_overrides,
)
from open_shrimp.opencode_client import split_provider_model
from open_shrimp.opencode_client._http import get_json

logger = logging.getLogger(__name__)

_context_limit_cache: dict[tuple[str, str, str], int] = {}


def chat_scope_from_message(message: Message) -> ChatScope:
    """Extract a ChatScope from a Telegram Message object."""
    thread_id = getattr(message, "message_thread_id", None)
    return ChatScope(chat_id=message.chat_id, thread_id=thread_id)


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


async def _get_context_name(scope: ChatScope, config: Config, db: aiosqlite.Connection) -> str:
    """Get the active context name for a scope (persisted in DB)."""
    # If locked, always use that context regardless of what's saved
    locked = _get_locked_context(scope.chat_id, config)
    if locked:
        await set_active_context(db, scope, locked)
        return locked

    saved = await get_active_context(db, scope)
    if saved and saved in config.contexts:
        return saved

    # Check if this chat has a default context configured
    for name, ctx in config.contexts.items():
        if scope.chat_id in ctx.default_for_chats:
            await set_active_context(db, scope, name)
            return name

    await set_active_context(db, scope, config.default_context)
    return config.default_context


async def _get_context(
    scope: ChatScope, config: Config, db: aiosqlite.Connection
) -> tuple[str, ContextConfig]:
    """Get context name and config for a scope.

    If a per-scope model or effort override is active (via ``/model`` or
    ``/effort``), returns a shallow copy of the context config with the
    overridden value.  Runtime additional directories (via ``/add_dir``)
    are merged in.
    """
    from dataclasses import replace

    name = await _get_context_name(scope, config, db)
    ctx = config.contexts[name]

    model_override = _model_overrides.get(scope)
    effort_override = _effort_overrides.get(scope)

    # Merge runtime additional directories from DB cache.
    extra_dirs = await _get_runtime_dirs(scope, name, db)

    if model_override or effort_override or extra_dirs:
        kwargs: dict[str, Any] = {}
        if model_override:
            kwargs["model"] = model_override
        if effort_override:
            kwargs["effort"] = effort_override
        if extra_dirs:
            kwargs["additional_directories"] = list(ctx.additional_directories) + extra_dirs
        ctx = replace(ctx, **kwargs)

    return name, ctx


async def _get_runtime_dirs(
    scope: ChatScope, context_name: str, db: aiosqlite.Connection,
) -> list[str]:
    """Return runtime additional directories, loading from DB on first access."""
    from open_shrimp.db import get_additional_directories

    key = (scope, context_name)
    if key not in _additional_dir_cache:
        _additional_dir_cache[key] = await get_additional_directories(db, scope, context_name)
    return _additional_dir_cache[key]


def _is_authorized(user_id: int | None, config: Config) -> bool:
    """Check if a user is in the allowlist."""
    return user_id is not None and user_id in config.allowed_users


def _is_bot_addressed(update: Update, bot_username: str) -> bool:
    """Check if the bot is @mentioned or replied to in a group chat.

    In private chats, always returns True.
    In forum topics, always returns True (treat as private-chat-like).
    """
    message = update.effective_message
    if message is None:
        return False

    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return True

    # In forum topics, respond to all messages (like private chat behavior).
    if getattr(chat, "is_forum", False) and getattr(message, "message_thread_id", None):
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


def _format_token_count(count: int) -> str:
    """Format a token count as a human-readable string (e.g. 12.3k, 1.2M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


_TODO_STATUS_EMOJI: dict[str, str] = {
    "completed": "\u2705",
    "in_progress": "\U0001f504",
    "pending": "\u2b1c",
    "cancelled": "\u274c",
}


def _build_status_text(
    ctx_name: str,
    ctx: ContextConfig,
    model_usage: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    context_window: int | None = None,
    todos: list[dict[str, Any]] | None = None,
) -> str:
    """Build the pinned status message text in MarkdownV2."""
    escaped_name = _escape_mdv2(ctx_name)
    escaped_desc = _escape_mdv2(ctx.description)
    escaped_dir = _escape_mdv2(ctx.directory)
    escaped_model = _escape_mdv2(ctx.model or "CLI default")
    lines = [
        f"\U0001f4cc *Active context:* `{escaped_name}`",
        f"{escaped_desc}",
        "",
        f"\U0001f4c1 `{escaped_dir}`",
        f"\U0001f916 `{escaped_model}`",
    ]
    if ctx.effort:
        lines.append(f"\U0001f9e0 *Effort:* `{_escape_mdv2(ctx.effort)}`")

    # Context window usage from per-turn API usage (the last assistant
    # message). OpenCode-native shape — see
    # ``opencode_client/events.py`` for the schema.
    if turn_usage:
        if not context_window:
            context_window = _DEFAULT_CONTEXT_LIMIT

        cache = turn_usage.get("cache") or {}
        total_tokens = (
            turn_usage.get("input", 0)
            + cache.get("write", 0)
            + cache.get("read", 0)
        )

        total_str = _escape_mdv2(_format_token_count(total_tokens))
        limit_str = _escape_mdv2(_format_token_count(context_window))
        pct = min(total_tokens / context_window * 100, 100) if context_window > 0 else 0
        pct_str = _escape_mdv2(f"{pct:.0f}%")

        lines.append("")
        lines.append(f"\U0001f4ca *Context:* {total_str} / {limit_str} \\({pct_str}\\)")

    if model_usage:
        total_cost = sum(m.get("cost", 0) for m in model_usage.values())
        if total_cost > 0:
            cost_str = _escape_mdv2(f"${total_cost:.4f}")
            lines.append(f"\U0001f4b0 *Cost:* {cost_str}")

    if todos:
        lines.append("")
        lines.append("\U0001f4dd *Tasks:*")
        # Cap at 15 items to avoid hitting Telegram's message length limit.
        display_todos = todos[:15]
        for todo in display_todos:
            status = todo.get("status", "pending")
            emoji = _TODO_STATUS_EMOJI.get(status, "\u2b1c")
            content = _escape_mdv2(todo.get("content", ""))
            if status == "completed":
                lines.append(f"{emoji} ~{content}~")
            else:
                lines.append(f"{emoji} {content}")
        remaining = len(todos) - len(display_todos)
        if remaining > 0:
            lines.append(_escape_mdv2(f"   ...and {remaining} more"))

    return "\n".join(lines)


async def _resolve_context_window(
    ctx: ContextConfig,
    opencode_client: Any | None = None,
) -> int | None:
    """Return the active model's context limit from OpenCode's model catalog."""
    try:
        provider_id, model_id = split_provider_model(ctx.model)
    except ValueError:
        return None

    cache_key = (ctx.directory, provider_id, model_id)
    cached = _context_limit_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        if opencode_client is not None:
            models = await opencode_client.get_models()
        else:
            payload = await get_json(
                "/api/model", params={"directory": ctx.directory},
            )
            if not isinstance(payload, list):
                return None
            models = [item for item in payload if isinstance(item, dict)]
    except Exception:
        logger.debug(
            "Failed to fetch OpenCode model catalog for %s/%s",
            provider_id,
            model_id,
            exc_info=True,
        )
        return None

    for model in models:
        if model.get("providerID") != provider_id:
            continue
        if model.get("id") != model_id and model.get("apiID") != model_id:
            continue
        limit = model.get("limit")
        if not isinstance(limit, dict):
            return None
        context = limit.get("context")
        if isinstance(context, (int, float)) and context > 0:
            value = int(context)
            _context_limit_cache[cache_key] = value
            return value
        return None

    return None


def _thread_kwargs(scope: ChatScope) -> dict[str, Any]:
    """Build message_thread_id kwargs for Telegram send methods."""
    if scope.thread_id is not None:
        return {"message_thread_id": scope.thread_id}
    return {}


async def _update_pinned_status(
    bot: Bot,
    scope: ChatScope,
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    model_usage: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    todos: list[dict[str, Any]] | None = None,
    opencode_client: Any | None = None,
) -> None:
    """Send or update the pinned status message for a scope."""
    context_window = None
    if turn_usage:
        context_window = await _resolve_context_window(ctx, opencode_client)
    text = _build_status_text(
        ctx_name, ctx, model_usage=model_usage, turn_usage=turn_usage,
        context_window=context_window, todos=todos,
    )
    existing_msg_id = await get_pinned_message_id(db, scope)

    # Try to edit the existing pinned message
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=scope.chat_id,
                message_id=existing_msg_id,
                text=text,
                parse_mode="MarkdownV2",
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.debug(
                "Could not edit pinned message %d in scope %s, will send new one",
                existing_msg_id,
                scope,
            )
        except Exception:
            logger.debug(
                "Could not edit pinned message %d in scope %s, will send new one",
                existing_msg_id,
                scope,
            )

    # Send a new message and pin it
    try:
        msg = await bot.send_message(
            chat_id=scope.chat_id,
            text=text,
            parse_mode="MarkdownV2",
            **_thread_kwargs(scope),
        )
        await set_pinned_message_id(db, scope, msg.message_id)
        await bot.pin_chat_message(
            chat_id=scope.chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("Failed to send/pin status message in scope %s", scope)
