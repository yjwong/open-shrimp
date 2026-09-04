"""Shared utility functions used across all handler modules."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from telegram import Bot, Message, Update
from telegram.error import BadRequest

from open_shrimp.backend import default_model_label
from open_shrimp.config import Config, ContextConfig, effective_backend
from open_shrimp.markdown import escape_rich, escape_rich_inline
from open_shrimp.rich_message import edit_rich, reply_rich, send_rich
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
from open_shrimp.supervisor import is_supervisor_context, resolve_context

logger = logging.getLogger(__name__)


def chat_scope_from_message(message: Message) -> ChatScope:
    """Extract a ChatScope from a Telegram Message object."""
    thread_id = getattr(message, "message_thread_id", None)
    return ChatScope(chat_id=message.chat_id, thread_id=thread_id)


def get_backend_for_scope(bot_data: dict[str, Any], scope: ChatScope) -> Any | None:
    """Resolve the active backend for a ``ChatScope``.

    Per-context overrides take precedence: if the scope has a live agent
    session, that session's pinned backend is returned (so the capability
    gate for ``/login``, ``/mcp``, ``/usage`` matches what is actually
    serving the turn).  Otherwise falls back to the process-wide default
    installed by ``run_bot``.  Returns ``None`` when no backend has been
    installed at all.
    """
    from open_shrimp.client_manager import get_session

    existing = get_session(scope)
    if existing is not None and existing.backend is not None:
        return existing.backend
    return bot_data.get("backend")


def _get_locked_context(chat_id: int, config: Config) -> str | None:
    """Return the context name this chat is locked to, or None."""
    for name, ctx in config.contexts.items():
        if chat_id in ctx.locked_for_chats:
            return name
    return None


async def _get_context_name(
    scope: ChatScope, config: Config, db: aiosqlite.Connection
) -> str | None:
    """Get the active context name for a scope (persisted in DB).

    Every branch returns a name :func:`resolve_context` resolves, so a
    caller may look the result up with it.  ``None`` means the scope has no
    project to bind to: nothing is saved, no chat default applies, and
    ``default_context`` is unset or names a context that no longer exists.

    The supervisor is reachable only through the saved binding, which the
    user creates by picking it.  No fallback below can produce it: it is
    not in ``config.contexts``, so no chat default and no
    ``default_context`` can name it.
    """
    # If locked, always use that context regardless of what's saved
    locked = _get_locked_context(scope.chat_id, config)
    if locked:
        await set_active_context(db, scope, locked)
        return locked

    saved = await get_active_context(db, scope)
    if saved and (saved in config.contexts or is_supervisor_context(saved)):
        return saved

    # Check if this chat has a default context configured
    for name, ctx in config.contexts.items():
        if scope.chat_id in ctx.default_for_chats:
            await set_active_context(db, scope, name)
            return name

    default = config.default_context
    if default is None or default not in config.contexts:
        return None

    await set_active_context(db, scope, default)
    return default


async def _get_context(
    scope: ChatScope, config: Config, db: aiosqlite.Connection
) -> tuple[str, ContextConfig] | None:
    """Get context name and config for a scope, or ``None`` if none is bound.

    If a per-scope model or effort override is active (via ``/model`` or
    ``/effort``), returns a shallow copy of the context config with the
    overridden value.  Runtime additional directories (via ``/add_dir``)
    are merged in.
    """
    from dataclasses import replace

    name = await _get_context_name(scope, config, db)
    if name is None:
        return None
    ctx = resolve_context(config, name)
    if ctx is None:
        return None

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


# Said wherever a scope needs a project and has none.  Every branch names the
# route that needs nothing but this chat: the OpenShrimp context writes
# config.yaml, so a user with no project never has to reach the machine the bot
# runs on.  /config is named second because it is a Mini App, and the
# supervisor answers in words on any device the user is already holding.
_NO_PROJECTS_TEXT = (
    "No project is set up yet, so there's nothing for me to work in.\n\n"
    "Open /context, pick OpenShrimp, and tell it which folder to add — "
    "it edits my config for you, and shows you the change before it lands. "
    "You can also add one in /config."
)

# Having no project and having picked none are different problems with
# different remedies, and setup produces the second one on purpose: it writes
# no ``default_context``, because it cannot know which imported project a topic
# should mean.  An install with one project therefore reaches its first message
# unbound, and telling that user nothing is set up is both false and useless —
# it sends them to add a second copy of the project they already have.
_UNBOUND_TEXT = (
    "No project is picked here yet, so there's nothing for me to work in.\n\n"
    "Open /context and choose one — or pick OpenShrimp there to add a folder "
    "I don't know about."
)


# Telegram caps a callback answer at 200 characters, so the alerts carry the
# first sentence and the remedy only.
_NO_PROJECTS_ANSWER = (
    "No project is set up yet — open /context, pick OpenShrimp, and ask it "
    "to add one."
)

_UNBOUND_ANSWER = (
    "No project is picked here yet — open /context and choose one, or pick "
    "OpenShrimp there to add a folder."
)


def no_context_text(config: Config) -> str:
    """What to say to a scope with no project, told apart by which is missing."""
    return _NO_PROJECTS_TEXT if not config.contexts else _UNBOUND_TEXT


def no_context_answer(config: Config) -> str:
    """The callback-alert form of :func:`no_context_text`."""
    return _NO_PROJECTS_ANSWER if not config.contexts else _UNBOUND_ANSWER


async def reply_no_context(message: Message, config: Config) -> None:
    """Tell the user this scope has no project bound, and how to get one."""
    await reply_rich(message, escape_rich(no_context_text(config)))


async def answer_no_context(query: Any, config: Config) -> None:
    """The callback-query form of :func:`reply_no_context`."""
    await query.answer(no_context_answer(config), show_alert=True)


async def send_no_context(bot: Bot, scope: ChatScope, config: Config) -> None:
    """The scope-addressed form, for paths with no message to reply to."""
    await send_rich(
        bot, scope.chat_id, escape_rich(no_context_text(config)),
        thread_id=scope.thread_id,
    )


async def require_context(
    message: Message,
    scope: ChatScope,
    config: Config,
    db: aiosqlite.Connection,
) -> tuple[str, ContextConfig] | None:
    """Resolve the scope's context, telling the user when there is none.

    Returns ``None`` after replying, so callers guard with a bare early
    return and never carry an unbound name into a dict or DB key.
    """
    resolved = await _get_context(scope, config, db)
    if resolved is None:
        await reply_no_context(message, config)
    return resolved


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


async def notify_operators(
    bot: Bot,
    allowed_users: list[int],
    text: str,
) -> None:
    """DM every allowed user *text* (rich Markdown), best effort.

    The write side of the policy :func:`_is_authorized` reads, and it
    lives here for the same reason: a message from this bot confirms a
    live instance, so who may receive one unprompted is one decision and
    not one per caller.  Used by the announcements that belong to the
    process rather than to a conversation — a boot, an update, a config
    reload — which have no chat to answer into.

    Delivery is per user and never raises: one blocked chat must not cost
    the others their message, and no announcement is worth taking down
    the loop that produced it.
    """
    for user_id in allowed_users:
        try:
            await send_rich(bot, user_id, text)
        except Exception:
            logger.warning(
                "Could not reach allowed user %s", user_id, exc_info=True
            )


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


async def _cancel_running(scope: ChatScope) -> None:
    """Cancel any running agent task for a scope.

    Sends an interrupt to the persistent CLI client (if any) so it stops
    processing, then cancels the asyncio task.  The persistent client
    stays alive for reuse by the next message.
    """
    import asyncio

    from open_shrimp.client_manager import get_session
    from open_shrimp.handlers.state import _running_tasks

    session = get_session(scope)
    if session is not None:
        try:
            await session.client.interrupt()
            logger.info("Sent interrupt to CLI for scope %s", scope)
        except Exception:
            logger.debug(
                "Failed to send interrupt for scope %s", scope, exc_info=True
            )

    task = _running_tasks.pop(scope, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Cancelled running task for scope %s", scope)


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
    config: Config,
    model_usage: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    todos: list[dict[str, Any]] | None = None,
) -> str:
    """Build the pinned status message body."""
    model = ctx.model or default_model_label(effective_backend(ctx, config))
    lines = [
        f"\U0001f4cc **Active context:** `{ctx_name}`",
        escape_rich(ctx.description),
        "",
        f"\U0001f4c1 `{ctx.directory}`",
        f"\U0001f916 `{model}`",
    ]
    if ctx.effort:
        lines.append(f"\U0001f9e0 **Effort:** `{ctx.effort}`")

    # Context window usage from per-turn API usage (the last assistant
    # message).  input_tokens + cache tokens = current context size.
    if turn_usage:
        context_window = _DEFAULT_CONTEXT_LIMIT
        if model_usage:
            first_model = next(iter(model_usage.values()))
            context_window = first_model.get("contextWindow", _DEFAULT_CONTEXT_LIMIT)

        # Per-turn usage from the API: input_tokens is non-cached
        # input, plus the two cache buckets = total context size.
        total_tokens = (
            turn_usage.get("input_tokens", 0)
            + turn_usage.get("cache_creation_input_tokens", 0)
            + turn_usage.get("cache_read_input_tokens", 0)
        )

        total_str = _format_token_count(total_tokens)
        limit_str = _format_token_count(context_window)
        pct = min(total_tokens / context_window * 100, 100) if context_window > 0 else 0

        lines.append("")
        lines.append(
            f"\U0001f4ca **Context:** {total_str} / {limit_str} "
            f"({pct:.0f}%)"
        )

    if model_usage:
        total_cost = sum(m.get("costUSD", 0) for m in model_usage.values())
        if total_cost > 0:
            lines.append(f"\U0001f4b0 **Cost:** ${total_cost:.4f}")

    if todos:
        lines.append("")
        lines.append("\U0001f4dd **Tasks:**")
        # Cap at 15 items to avoid hitting Telegram's message length limit.
        display_todos = todos[:15]
        for todo in display_todos:
            status = todo.get("status", "pending")
            content = escape_rich_inline(todo.get("content", ""))
            if status == "completed":
                lines.append(f"- [x] ~~{content}~~")
            elif status == "in_progress":
                lines.append(f"- [ ] **{content}**")
            else:
                lines.append(f"- [ ] {content}")
        remaining = len(todos) - len(display_todos)
        if remaining > 0:
            lines.append(f"\n*...and {remaining} more*")

    return "\n".join(lines)


async def _update_pinned_status(
    bot: Bot,
    scope: ChatScope,
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    config: Config,
    model_usage: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    todos: list[dict[str, Any]] | None = None,
) -> None:
    """Send or update the pinned status message for a scope."""
    text = _build_status_text(
        ctx_name, ctx, config, model_usage=model_usage, turn_usage=turn_usage,
        todos=todos,
    )
    existing_msg_id = await get_pinned_message_id(db, scope)

    # Try to edit the existing pinned message
    if existing_msg_id:
        try:
            await edit_rich(bot, scope.chat_id, existing_msg_id, text)
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
        msg = await send_rich(
            bot, scope.chat_id, text, thread_id=scope.thread_id,
        )
        await set_pinned_message_id(db, scope, msg.message_id)
        await bot.pin_chat_message(
            chat_id=scope.chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("Failed to send/pin status message in scope %s", scope)
