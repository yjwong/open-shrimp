"""Telegram command handlers (/context, /clear, /status, /cancel, /model,
/resume, /review, /mcp, /tasks, /usage).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from open_udang.client_manager import (
    AgentSession,
    close_session,
    get_session,
)
from open_udang.config import Config, ContextConfig
from open_udang.db import ChatScope, delete_session, get_session_id, set_session_id
from open_udang.handlers.state import (
    _MCP_STATUS_EMOJI,
    _RESUME_LIST_LIMIT,
    _active_bg_tasks,
    _edit_approved_sessions,
    _injectable_sessions,
    _model_overrides,
    _resume_selections,
    _running_tasks,
    _tool_approved_sessions,
    _setup_queues,
)
from open_udang.handlers.utils import (
    _cancel_running,
    _escape_mdv2,
    _get_context,
    _get_context_name,
    _get_locked_context,
    _is_authorized,
    _update_pinned_status,
    chat_scope_from_message,
)

logger = logging.getLogger(__name__)


# ── /context ──

_CONTEXT_PAGE_SIZE = 6


def _build_context_page(
    config: Config, current: str, page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a page of context buttons with optional pagination."""
    names = list(config.contexts.keys())
    total = len(names)
    total_pages = max(1, (total + _CONTEXT_PAGE_SIZE - 1) // _CONTEXT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _CONTEXT_PAGE_SIZE
    page_names = names[start : start + _CONTEXT_PAGE_SIZE]

    buttons: list[list[InlineKeyboardButton]] = []
    for name in page_names:
        ctx = config.contexts[name]
        label = f"{'• ' if name == current else ''}{name} — {ctx.description}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ctx:{name}")])

    # Pagination row
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"ctx_page:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ctx_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"ctx_page:{page + 1}"))
        buttons.append(nav)

    text = "*Select a context:*"
    return text, InlineKeyboardMarkup(buttons)


async def handle_context_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle context selection and pagination callbacks. Returns True if handled."""
    if data == "ctx_noop":
        await query.answer()
        return True

    if data.startswith("ctx_page:"):
        # Pagination
        page = int(data.split(":", 1)[1])
        db: aiosqlite.Connection = context.bot_data["db"]
        if not query.message:
            await query.answer()
            return True
        scope = chat_scope_from_message(query.message)
        current = await _get_context_name(scope, config, db)
        text, markup = _build_context_page(config, current, page)
        try:
            await query.message.edit_text(text, parse_mode="MarkdownV2", reply_markup=markup)
        except Exception:
            pass
        await query.answer()
        return True

    if data.startswith("ctx_clear:"):
        # Clear session for a context (from the "Clear session" button after switch)
        target = data[len("ctx_clear:"):]
        db = context.bot_data["db"]
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True

        scope = chat_scope_from_message(query.message)
        ctx_name = await _get_context_name(scope, config, db)

        if target == ctx_name:
            await _cancel_running(scope)
            _injectable_sessions.pop(scope, None)
            _setup_queues.pop(scope, None)
            await close_session(scope)
            await delete_session(db, scope, ctx_name)
            _edit_approved_sessions.discard((scope, ctx_name))
            _tool_approved_sessions.pop((scope, ctx_name), None)
            _active_bg_tasks.pop(scope, None)

        ctx = config.contexts.get(target)
        desc = _escape_mdv2(ctx.description) if ctx else ""
        target_escaped = _escape_mdv2(target)
        try:
            await query.message.edit_text(
                f"Switched to context `{target_escaped}` \\- {desc}\n_Started fresh session\\._",
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception:
            logger.exception("Failed to update context message")

        await query.answer("Session cleared")
        return True

    if data.startswith("ctx:"):
        # Context selection
        target = data[4:]
        db = context.bot_data["db"]
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True

        scope = chat_scope_from_message(query.message)

        if target not in config.contexts:
            await query.answer("Context no longer exists.")
            return True

        locked = _get_locked_context(scope.chat_id, config)
        if locked:
            await query.answer(f"Chat is locked to context {locked}.")
            return True

        current = await _get_context_name(scope, config, db)
        if target == current:
            await query.answer(f"Already on {target}.")
            return True

        _edit_approved_sessions.discard((scope, current))
        _tool_approved_sessions.pop((scope, current), None)
        _model_overrides.pop(scope, None)
        await close_session(scope)

        from open_udang.db import set_active_context

        await set_active_context(db, scope, target)
        ctx = config.contexts[target]
        desc = _escape_mdv2(ctx.description)
        target_escaped = _escape_mdv2(target)

        existing_session = await get_session_id(db, scope, target)
        if existing_session:
            text = f"Switched to context `{target_escaped}` \\- {desc}\n_Resuming existing session\\._"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Clear session", callback_data=f"ctx_clear:{target}"),
            ]])
        else:
            text = f"Switched to context `{target_escaped}` \\- {desc}"
            markup = None

        try:
            await query.message.edit_text(
                text,
                parse_mode="MarkdownV2",
                reply_markup=markup,
            )
        except Exception:
            logger.exception("Failed to update context message")

        await query.answer(f"Switched to {target}")
        await _update_pinned_status(context.bot, scope, target, ctx, db)
        return True

    return False


async def context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /context command: list or switch contexts."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # List contexts as inline keyboard
        current = await _get_context_name(scope, config, db)
        locked = _get_locked_context(scope.chat_id, config)
        if locked:
            ctx = config.contexts[locked]
            escaped_name = _escape_mdv2(locked)
            escaped_desc = _escape_mdv2(ctx.description)
            await message.reply_text(
                f"This chat is locked to context `{escaped_name}` \\- {escaped_desc}",
                parse_mode="MarkdownV2",
            )
        else:
            text, markup = _build_context_page(config, current, page=0)
            await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=markup)
        return

    # Switch context
    target = args[1]
    if target not in config.contexts:
        names = ", ".join(f"`{n}`" for n in config.contexts)
        await message.reply_text(
            f"Unknown context: `{target}`\\. Available: {names}",
            parse_mode="MarkdownV2",
        )
        return

    locked = _get_locked_context(scope.chat_id, config)
    if locked:
        await message.reply_text(
            f"This chat is locked to context `{locked}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    old_ctx_name = await _get_context_name(scope, config, db)
    _edit_approved_sessions.discard((scope, old_ctx_name))
    _tool_approved_sessions.pop((scope, old_ctx_name), None)
    _model_overrides.pop(scope, None)
    await close_session(scope)

    from open_udang.db import set_active_context

    await set_active_context(db, scope, target)
    ctx = config.contexts[target]
    desc = _escape_mdv2(ctx.description)
    target_escaped = _escape_mdv2(target)

    existing_session = await get_session_id(db, scope, target)
    if existing_session:
        text = f"Switched to context `{target_escaped}` \\- {desc}\n_Resuming existing session\\._"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Clear session", callback_data=f"ctx_clear:{target}"),
        ]])
    else:
        text = f"Switched to context `{target_escaped}` \\- {desc}"
        markup = None

    await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=markup)
    await _update_pinned_status(context.bot, scope, target, ctx, db)


# ── /clear ──


async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command: start fresh session."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)

    await _cancel_running(scope)
    _injectable_sessions.pop(scope, None)
    _setup_queues.pop(scope, None)
    await close_session(scope)
    await delete_session(db, scope, ctx_name)
    _edit_approved_sessions.discard((scope, ctx_name))
    _tool_approved_sessions.pop((scope, ctx_name), None)
    _model_overrides.pop(scope, None)
    _active_bg_tasks.pop(scope, None)
    await message.reply_text(f"Started fresh session in context `{ctx_name}`\\.", parse_mode="MarkdownV2")
    await _update_pinned_status(context.bot, scope, ctx_name, ctx, db)


# ── /status ──


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command: show current state."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)
    session_id = await get_session_id(db, scope, ctx_name)
    running = scope in _running_tasks and not _running_tasks[scope].done()
    injectable = scope in _injectable_sessions
    setup_queued = len(_setup_queues.get(scope, []))

    lines = [
        f"*Context:* `{ctx_name}`",
        f"*Directory:* `{ctx.directory}`",
        f"*Model:* `{ctx.model or 'CLI default'}`" + (" \\(override\\)" if scope in _model_overrides else ""),
        f"*Session:* {'`' + session_id[:12] + '...' + '`' if session_id else 'None'}",
        f"*Running:* {'Yes' if running else 'No'}",
        f"*Injectable:* {'Yes' if injectable else 'No'}",
        f"*Setup queued:* {setup_queued}",
    ]
    # Background tasks.
    scope_tasks = _active_bg_tasks.get(scope, {})
    if scope_tasks:
        lines.append(f"*Background tasks:* {len(scope_tasks)}")
        now = time.monotonic()
        for task in scope_tasks.values():
            elapsed = int(now - task.started_at)
            minutes, seconds = divmod(elapsed, 60)
            duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
            tid_short = task.task_id[:12]
            ttype = task.task_type or "unknown"
            lines.append(
                f"  • `{tid_short}` {ttype}: "
                f"{task.description or 'N/A'} ({duration})"
            )
    # Escape dots and dashes for MarkdownV2
    text = "\n".join(lines)
    for ch in ".-/":
        text = text.replace(ch, f"\\{ch}")
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /cancel ──


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command: abort running Claude invocation."""
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    had_running = scope in _running_tasks and not _running_tasks[scope].done()
    setup_queued = len(_setup_queues.pop(scope, []))

    if had_running:
        _injectable_sessions.pop(scope, None)
        await _cancel_running(scope)

    if had_running:
        parts = ["Cancelled running task"]
        if setup_queued:
            parts.append(f"cleared {setup_queued} queued message{'s' if setup_queued != 1 else ''}")
        text = "\\. ".join(parts) + "\\."
        await message.reply_text(text, parse_mode="MarkdownV2")
    else:
        await message.reply_text("Nothing running\\.", parse_mode="MarkdownV2")


# ── /model ──


async def model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command: show or override the model for this chat.

    Usage:
        /model              -- show current model (and override if active)
        /model <name>       -- override the model for this chat session
        /model reset        -- clear the override, revert to context default
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name = await _get_context_name(scope, config, db)
    ctx_default_model = config.contexts[ctx_name].model
    current_override = _model_overrides.get(scope)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # Show current model
        if current_override:
            text = (
                f"*Model:* `{current_override}` \\(override\\)\n"
                f"*Context default:* `{ctx_default_model or 'CLI default'}`\n\n"
                f"Use `/model reset` to revert\\."
            )
        else:
            text = f"*Model:* `{ctx_default_model or 'CLI default'}` \\(context default\\)"
        for ch in ".-/":
            text = text.replace(ch, f"\\{ch}")
        await message.reply_text(text, parse_mode="MarkdownV2")
        return

    target = args[1]

    if target == "reset":
        if current_override:
            del _model_overrides[scope]
            await close_session(scope)
            model_escaped = _escape_mdv2(ctx_default_model or "CLI default")
            await message.reply_text(
                f"Model override cleared\\. Using context default: `{model_escaped}`",
                parse_mode="MarkdownV2",
            )
        else:
            await message.reply_text(
                "No model override active\\.",
                parse_mode="MarkdownV2",
            )
        return

    # Set override
    _model_overrides[scope] = target
    await close_session(scope)
    model_escaped = _escape_mdv2(target)
    await message.reply_text(
        f"Model overridden to `{model_escaped}`\\. "
        f"Use `/model reset` to revert\\.",
        parse_mode="MarkdownV2",
    )


def _list_sessions_for_context(
    ctx_name: str, ctx: ContextConfig, **kwargs: Any,
) -> "list[Any]":
    """Call ``list_sessions`` respecting containerized session storage.

    For containerized contexts the session ``.jsonl`` files live under the
    per-context state directory (bind-mounted as ``~/.claude`` inside the
    container), not the host's ``~/.claude``.  We scan that directory
    directly using the SDK's internal helpers to avoid mutating global
    process state (``CLAUDE_CONFIG_DIR``).
    """
    from claude_agent_sdk import list_sessions
    from claude_agent_sdk._internal.sessions import (
        MAX_SANITIZED_LENGTH,
        _apply_sort_limit_offset,
        _canonicalize_path,
        _read_sessions_from_dir,
        _sanitize_path,
    )
    from platformdirs import user_data_path

    if ctx.container is not None and ctx.container.enabled:
        state_dir = user_data_path("openudang") / "containers" / ctx_name
        projects_dir = state_dir / "projects"
        canonical = _canonicalize_path(ctx.directory)
        sanitized = _sanitize_path(canonical)
        candidate = projects_dir / sanitized
        project_dir = None
        if candidate.is_dir():
            project_dir = candidate
        elif len(sanitized) > MAX_SANITIZED_LENGTH:
            # Prefix scan for long paths (hash mismatch tolerance).
            prefix = sanitized[:MAX_SANITIZED_LENGTH]
            try:
                for entry in projects_dir.iterdir():
                    if entry.is_dir() and entry.name.startswith(prefix + "-"):
                        project_dir = entry
                        break
            except OSError:
                pass

        if project_dir is None:
            return []
        sessions = _read_sessions_from_dir(project_dir, canonical)
        return _apply_sort_limit_offset(
            sessions, kwargs.get("limit"), kwargs.get("offset", 0),
        )

    return list_sessions(directory=ctx.directory, **kwargs)


# ── /resume ──


async def _build_resume_page(
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    scope: ChatScope,
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build a single page of the resume session list.

    Returns ``(text, keyboard)`` where *keyboard* is ``None`` when there are
    no sessions at all.
    """
    per_page = _RESUME_LIST_LIMIT
    offset = page * per_page
    # Fetch one extra to detect whether a next page exists.
    sessions = await asyncio.to_thread(
        _list_sessions_for_context, ctx_name, ctx,
        limit=per_page + 1, offset=offset,
    )

    if not sessions:
        if page == 0:
            return (
                f"No sessions found for context `{_escape_mdv2(ctx_name)}`\\.",
                None,
            )
        # Edge case: page beyond last – go back.
        return await _build_resume_page(ctx_name, ctx, db, scope, page - 1)

    has_next = len(sessions) > per_page
    sessions = sessions[:per_page]

    current_session_id = await get_session_id(db, scope, ctx_name)

    buttons: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        summary = s.summary or "No summary"
        if len(summary) > 60:
            summary = summary[:57] + "..."
        sid_short = s.session_id[:8]
        marker = " (current)" if s.session_id == current_session_id else ""
        label = f"{sid_short} - {summary}{marker}"
        cb_data = f"resume:{s.session_id}"
        _resume_selections[cb_data] = s.session_id
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])

    # Navigation row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "\u25c0 Prev", callback_data=f"resume_page:{ctx_name}:{page - 1}",
        ))
    if has_next:
        nav.append(InlineKeyboardButton(
            "Next \u25b6", callback_data=f"resume_page:{ctx_name}:{page + 1}",
        ))
    if nav:
        buttons.append(nav)

    page_label = f" \\(page {page + 1}\\)" if page > 0 or has_next else ""
    text = f"*Recent sessions for* `{_escape_mdv2(ctx_name)}`*:*{page_label}"
    return text, InlineKeyboardMarkup(buttons)


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume command: list recent sessions or resume a specific one.

    Usage:
        /resume          - Show recent sessions for the current context
        /resume <id>     - Resume a session by ID (prefix match supported)
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)

    args = message.text.split() if message.text else []

    if len(args) >= 2:
        # Direct resume by session ID (or prefix)
        target = args[1]
        sessions = await asyncio.to_thread(
            _list_sessions_for_context, ctx_name, ctx,
        )
        match = None
        for s in sessions:
            if s.session_id == target or s.session_id.startswith(target):
                match = s
                break

        if not match:
            await message.reply_text(
                f"No session matching `{_escape_mdv2(target)}` found in context `{_escape_mdv2(ctx_name)}`\\.",
                parse_mode="MarkdownV2",
            )
            return

        await close_session(scope)
        await set_session_id(db, scope, ctx_name, match.session_id)
        summary = _escape_mdv2(match.summary or "No summary")
        await message.reply_text(
            f"Resumed session `{_escape_mdv2(match.session_id[:12])}...`\n_{summary}_",
            parse_mode="MarkdownV2",
        )
        await _update_pinned_status(context.bot, scope, ctx_name, ctx, db)
        return

    # List recent sessions for the current context (page 0)
    text, keyboard = await _build_resume_page(ctx_name, ctx, db, scope, page=0)

    if keyboard is None:
        await message.reply_text(text, parse_mode="MarkdownV2")
        return

    await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


# ── /resume callback handler ──


async def handle_resume_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /resume session selection callback. Returns True if handled."""
    if not data.startswith("resume:") and not data.startswith("resume_page:"):
        return False

    db: aiosqlite.Connection = context.bot_data["db"]

    # Handle pagination
    if data.startswith("resume_page:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Invalid page data.")
            return True
        ctx_name_req, page_str = parts[1], parts[2]
        try:
            page = int(page_str)
        except ValueError:
            await query.answer("Invalid page number.")
            return True
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True
        scope = chat_scope_from_message(query.message)
        _, ctx = await _get_context(scope, config, db)
        # Use the context name from the callback to stay consistent
        ctx = config.contexts.get(ctx_name_req, ctx)
        text, keyboard = await _build_resume_page(
            ctx_name_req, ctx, db, scope, page,
        )
        await query.answer()
        try:
            await query.message.edit_text(
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to update resume page")
        return True

    session_id = _resume_selections.pop(data, None)
    if not session_id:
        await query.answer("This selection has expired.")
        return True

    if not query.message:
        await query.answer("Cannot determine chat.")
        return True

    scope = chat_scope_from_message(query.message)

    ctx_name, ctx = await _get_context(scope, config, db)
    await close_session(scope)
    await set_session_id(db, scope, ctx_name, session_id)
    await query.answer(f"Resumed session {session_id[:8]}...")

    try:
        summary_text = f"\u2705 Resumed session `{_escape_mdv2(session_id[:12])}\\.\\.\\.`"
        await query.message.edit_text(
            text=summary_text,
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception:
        logger.exception("Failed to update resume message")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("Failed to remove resume keyboard")

    await _update_pinned_status(
        context.bot, scope, ctx_name, ctx, db
    )
    # Clean up remaining selections from this listing
    expired = [k for k in _resume_selections if k.startswith("resume:")]
    for k in expired:
        _resume_selections.pop(k, None)
    return True


# ── /review ──


async def review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review -- open the review Mini App for the current context."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)

    if not _is_authorized(update.effective_user.id, config):
        return

    context_name, ctx = await _get_context(scope, config, db)

    # Build the Mini App URL.
    # Use the configured public URL if available, otherwise build from
    # host:port.  For production behind a reverse proxy the user should
    # set review.public_url in config.
    if config.review.public_url:
        base_url = config.review.public_url.rstrip("/")
    else:
        base_url = f"https://{config.review.host}:{config.review.port}"

    # Telegram Mini App (web_app) buttons only work in private chats.
    # The review app also relies on Telegram.WebApp.initData for auth,
    # which is unavailable outside the Mini App WebView.
    chat_type = update.effective_chat.type if update.effective_chat else "private"
    if chat_type != "private":
        await update.message.reply_text(
            "The review Mini App is only available in private chats\\. "
            "Send /review to me in a DM instead\\.",
            parse_mode="MarkdownV2",
        )
        return

    escaped_context = _escape_mdv2(context_name)
    dirs = [ctx.directory] + (ctx.additional_directories or [])
    thread_param = f"&thread_id={scope.thread_id}" if scope.thread_id is not None else ""

    if len(dirs) == 1:
        app_url = f"{base_url}/app/?chat_id={scope.chat_id}{thread_param}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text="\U0001f4dd Open Review",
                web_app=WebAppInfo(url=app_url),
            )]
        ])
        escaped_dir = _escape_mdv2(ctx.directory)
        text = (
            f"Review changes in *{escaped_context}*\n"
            f"\U0001f4c1 `{escaped_dir}`"
        )
    else:
        # Multiple directories: one button per directory.
        rows = []
        for i, d in enumerate(dirs):
            app_url = f"{base_url}/app/?chat_id={scope.chat_id}&dir={i}{thread_param}"
            basename = d.rstrip("/").rsplit("/", 1)[-1]
            rows.append([InlineKeyboardButton(
                text=f"\U0001f4c1 {basename}",
                web_app=WebAppInfo(url=app_url),
            )])
        keyboard = InlineKeyboardMarkup(rows)
        text = f"Review changes in *{escaped_context}*"

    await update.message.reply_text(
        text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ── /vnc ──


async def vnc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /vnc -- open the VNC Mini App for the current context's desktop."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)

    if not _is_authorized(update.effective_user.id, config):
        return

    context_name, ctx = await _get_context(scope, config, db)

    # Check that the context has computer_use enabled.
    if ctx.container is None or not ctx.container.computer_use:
        await update.message.reply_text(
            f"Context `{_escape_mdv2(context_name)}` does not have computer use enabled\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Build the Mini App URL.
    if config.review.public_url:
        base_url = config.review.public_url.rstrip("/")
    else:
        base_url = f"https://{config.review.host}:{config.review.port}"

    vnc_url = f"{base_url}/vnc/?context={context_name}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            text="View desktop",
            web_app=WebAppInfo(url=vnc_url),
        )]
    ])

    escaped_context = _escape_mdv2(context_name)
    await update.message.reply_text(
        f"Desktop for *{escaped_context}*",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ── /mcp ──


async def mcp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mcp command: list, reconnect, enable, or disable MCP servers.

    Usage:
        /mcp                    -- list all MCP servers and their status
        /mcp reset <name>       -- reconnect a failed/disconnected server
        /mcp enable <name>      -- enable a server
        /mcp disable <name>     -- disable a server
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)

    session = get_session(scope)
    if session is None:
        await message.reply_text(
            "No active session\\. Send a message first to start a session, "
            "then use /mcp to manage MCP servers\\.",
            parse_mode="MarkdownV2",
        )
        return

    args = message.text.split() if message.text else []
    subcommand = args[1] if len(args) >= 2 else None
    server_name = " ".join(args[2:]) if len(args) >= 3 else None

    if subcommand is None:
        # List all MCP servers
        await _mcp_list(message, session)
    elif subcommand == "reset":
        if not server_name:
            await message.reply_text(
                "Usage: `/mcp reset <server\\-name>`",
                parse_mode="MarkdownV2",
            )
            return
        await _mcp_reconnect(message, session, server_name)
    elif subcommand in ("enable", "disable"):
        if not server_name:
            await message.reply_text(
                f"Usage: `/mcp {subcommand} <server\\-name>`",
                parse_mode="MarkdownV2",
            )
            return
        await _mcp_toggle(message, session, server_name, enabled=(subcommand == "enable"))
    else:
        await message.reply_text(
            "Unknown subcommand\\. Usage:\n"
            "`/mcp` \u2014 list servers\n"
            "`/mcp reset <name>` \u2014 reconnect a server\n"
            "`/mcp enable <name>` \u2014 enable a server\n"
            "`/mcp disable <name>` \u2014 disable a server",
            parse_mode="MarkdownV2",
        )


async def _mcp_list(message: Any, session: AgentSession) -> None:
    """Fetch and display MCP server status."""
    try:
        status_resp = await session.client.get_mcp_status()
    except Exception:
        logger.exception("Failed to get MCP status")
        await message.reply_text("Failed to retrieve MCP server status\\.", parse_mode="MarkdownV2")
        return

    servers = status_resp.get("mcpServers", [])
    if not servers:
        await message.reply_text("No MCP servers configured\\.", parse_mode="MarkdownV2")
        return

    lines: list[str] = ["*MCP Servers*\n"]
    for srv in servers:
        name = srv.get("name", "unknown")
        status = srv.get("status", "unknown")
        emoji = _MCP_STATUS_EMOJI.get(status, "\u2753")
        scope = srv.get("scope", "")

        line = f"{emoji} *{_escape_mdv2(name)}*"
        if scope:
            line += f" \\({_escape_mdv2(scope)}\\)"
        line += f" \u2014 {_escape_mdv2(status)}"

        # Show server info (version) when connected
        server_info = srv.get("serverInfo")
        if server_info:
            version = server_info.get("version", "")
            if version:
                line += f" v{_escape_mdv2(version)}"

        # Show error message for failed servers
        error = srv.get("error")
        if error:
            # Truncate long errors
            if len(error) > 120:
                error = error[:117] + "..."
            line += f"\n    \u26a0\ufe0f {_escape_mdv2(error)}"

        # Show tool count when connected
        tools = srv.get("tools", [])
        if tools:
            line += f"\n    \U0001f527 {len(tools)} tool{'s' if len(tools) != 1 else ''}"

        lines.append(line)

    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")


async def _mcp_reconnect(message: Any, session: AgentSession, server_name: str) -> None:
    """Reconnect a failed or disconnected MCP server."""
    try:
        await session.client.reconnect_mcp_server(server_name)
    except Exception:
        logger.exception("Failed to reconnect MCP server %s", server_name)
        await message.reply_text(
            f"Failed to reconnect `{_escape_mdv2(server_name)}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    escaped = _escape_mdv2(server_name)
    await message.reply_text(
        f"Reconnecting `{escaped}`\\.\\.\\. Use /mcp to check status\\.",
        parse_mode="MarkdownV2",
    )


async def _mcp_toggle(message: Any, session: AgentSession, server_name: str, *, enabled: bool) -> None:
    """Enable or disable an MCP server."""
    action = "enable" if enabled else "disable"
    try:
        await session.client.toggle_mcp_server(server_name, enabled=enabled)
    except Exception:
        logger.exception("Failed to %s MCP server %s", action, server_name)
        await message.reply_text(
            f"Failed to {_escape_mdv2(action)} `{_escape_mdv2(server_name)}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    past = "enabled" if enabled else "disabled"
    escaped = _escape_mdv2(server_name)
    emoji = "\U0001f7e2" if enabled else "\u26aa"
    await message.reply_text(
        f"{emoji} `{escaped}` {past}\\.",
        parse_mode="MarkdownV2",
    )


# ── /schedule ──


async def schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schedule command: list and manage scheduled tasks.

    Usage:
        /schedule           -- list all scheduled tasks for this chat
        /schedule delete <n> -- delete a scheduled task by name
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    args = message.text.split() if message.text else []

    if len(args) >= 3 and args[1] == "delete":
        # Delete a task by name.
        task_name = " ".join(args[2:])
        from open_udang.db import delete_scheduled_task, list_scheduled_tasks

        # Find task ID for JobQueue removal.
        tasks = await list_scheduled_tasks(db, scope)
        task_id = None
        for t in tasks:
            if t.name == task_name:
                task_id = t.id
                break

        deleted = await delete_scheduled_task(db, scope, task_name)
        if deleted:
            # Remove from JobQueue.
            if task_id is not None and context.job_queue:
                job_name = f"scheduled_task_{task_id}"
                for j in context.job_queue.get_jobs_by_name(job_name):
                    j.schedule_removal()

            escaped = _escape_mdv2(task_name)
            await message.reply_text(
                f"Deleted scheduled task `{escaped}`\\.",
                parse_mode="MarkdownV2",
            )
        else:
            escaped = _escape_mdv2(task_name)
            await message.reply_text(
                f"No scheduled task named `{escaped}` found\\.",
                parse_mode="MarkdownV2",
            )
        return

    # List all tasks.
    from open_udang.db import list_scheduled_tasks

    tasks = await list_scheduled_tasks(db, scope)
    if not tasks:
        await message.reply_text(
            "No scheduled tasks\\. Ask Claude to create one\\!",
            parse_mode="MarkdownV2",
        )
        return

    lines = [f"*Scheduled tasks \\({len(tasks)}\\):*\n"]
    for t in tasks:
        type_desc = {
            "interval": f"every {t.schedule_expr}",
            "cron": f"cron: {t.schedule_expr}",
            "once": f"at {t.schedule_expr}",
        }.get(t.schedule_type, t.schedule_expr)

        prompt_preview = t.prompt[:50] + ("..." if len(t.prompt) > 50 else "")
        name_escaped = _escape_mdv2(t.name)
        desc_escaped = _escape_mdv2(type_desc)
        prompt_escaped = _escape_mdv2(prompt_preview)
        ctx_escaped = _escape_mdv2(t.context_name)

        lines.append(
            f"• *{name_escaped}*\n"
            f"  📅 {desc_escaped}\n"
            f"  📁 `{ctx_escaped}`\n"
            f"  💬 _{prompt_escaped}_"
        )

    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /tasks ──


async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tasks command: list active background tasks or stop one.

    Usage:
        /tasks              -- list active background tasks
        /tasks stop <id>    -- stop a background task by ID (prefix match)
    """
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(
        update.effective_user and update.effective_user.id, config
    ):
        return

    scope = chat_scope_from_message(message)
    args = message.text.split() if message.text else []

    # ── /tasks stop <id> ──
    if len(args) >= 3 and args[1] == "stop":
        target = args[2]
        scope_tasks = _active_bg_tasks.get(scope, {})

        # Find by exact match or prefix.
        matched_task = None
        for tid, task in scope_tasks.items():
            if tid == target or tid.startswith(target):
                matched_task = task
                break

        if not matched_task:
            await message.reply_text(
                f"No active task matching `{_escape_mdv2(target)}`\\.",
                parse_mode="MarkdownV2",
            )
            return

        from open_udang.client_manager import stop_background_task

        success = await stop_background_task(scope, matched_task.task_id)
        if success:
            # Remove from tracking immediately — the TaskNotificationMessage
            # may arrive later when the stream is next consumed, but we
            # don't want the task to linger in /tasks output.
            scope_tasks.pop(matched_task.task_id, None)
            if not scope_tasks:
                _active_bg_tasks.pop(scope, None)
            tid_short = _escape_mdv2(matched_task.task_id[:12])
            await message.reply_text(
                f"Stopped task `{tid_short}`\\.",
                parse_mode="MarkdownV2",
            )
        else:
            await message.reply_text(
                "Failed to stop task \\(no active session\\)\\.",
                parse_mode="MarkdownV2",
            )
        return

    # ── /tasks (list) ──
    scope_tasks = _active_bg_tasks.get(scope, {})
    if not scope_tasks:
        await message.reply_text(
            "No active background tasks\\.", parse_mode="MarkdownV2"
        )
        return

    now = time.monotonic()
    lines = [f"*Active background tasks \\({len(scope_tasks)}\\):*\n"]
    for task in scope_tasks.values():
        elapsed = int(now - task.started_at)
        minutes, seconds = divmod(elapsed, 60)
        duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"

        tid_short = _escape_mdv2(task.task_id[:12])
        desc_escaped = _escape_mdv2(task.description or "No description")
        type_escaped = _escape_mdv2(task.task_type or "unknown")

        line = (
            f"• `{tid_short}` \\- {desc_escaped}\n"
            f"  Type: {type_escaped} \\| Duration: {_escape_mdv2(duration)}"
        )
        if task.last_tool_name:
            line += f" \\| Last tool: {_escape_mdv2(task.last_tool_name)}"
        lines.append(line)

    lines.append(f"\nUse `/tasks stop <id>` to stop a task\\.")
    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /usage ──

# Cache: (timestamp, response_dict)
_usage_cache: tuple[float, dict[str, Any]] | None = None
_USAGE_CACHE_TTL = 60  # seconds


async def _fetch_usage() -> dict[str, Any] | None:
    """Fetch usage data from the Anthropic OAuth usage endpoint.

    Returns the parsed JSON response, or None if unavailable.
    Uses a 60-second cache to avoid hitting the rate limit.
    """
    global _usage_cache
    now = time.monotonic()
    if _usage_cache and now - _usage_cache[0] < _USAGE_CACHE_TTL:
        return _usage_cache[1]

    credentials_path = Path.home() / ".claude" / ".credentials.json"
    if not credentials_path.exists():
        return None

    try:
        creds = _json.loads(credentials_path.read_text())
        token = creds["claudeAiOauth"]["accessToken"]
    except (KeyError, _json.JSONDecodeError, OSError):
        return None

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            _usage_cache = (now, data)
            return data
    except (httpx.HTTPError, _json.JSONDecodeError):
        return None


def _format_tier(name: str, tier: dict[str, Any] | None) -> str | None:
    """Format a single usage tier line. Returns None if tier is absent."""
    if not tier or tier.get("utilization") is None:
        return None
    util = tier["utilization"]
    remaining = max(0, 100 - util)
    bar = _usage_bar(remaining)
    line = f"*{_escape_mdv2(name)}:* {bar} {_escape_mdv2(f'{remaining:.0f}% remaining')}"
    resets_at = tier.get("resets_at")
    if resets_at:
        try:
            reset_dt = datetime.fromisoformat(resets_at)
            delta = reset_dt - datetime.now(timezone.utc)
            total_seconds = int(delta.total_seconds())
            if total_seconds > 0:
                hours, remainder = divmod(total_seconds, 3600)
                minutes = remainder // 60
                if hours > 0:
                    line += _escape_mdv2(f" (resets in {hours}h{minutes}m)")
                else:
                    line += _escape_mdv2(f" (resets in {minutes}m)")
        except (ValueError, TypeError):
            pass
    return line


def _usage_bar(remaining: float) -> str:
    """Build a small text-based usage bar (10 segments)."""
    filled = round(remaining / 10)
    return _escape_mdv2("[" + "█" * filled + "░" * (10 - filled) + "]")


async def usage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage command: show Claude quota/usage stats."""
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    data = await _fetch_usage()
    if data is None:
        await message.reply_text(
            "Usage data unavailable\\. OAuth credentials not found or endpoint unreachable\\.",
            parse_mode="MarkdownV2",
        )
        return

    lines: list[str] = []
    for label, key in [
        ("5-hour session", "five_hour"),
        ("7-day overall", "seven_day"),
        ("7-day Sonnet", "seven_day_sonnet"),
    ]:
        line = _format_tier(label, data.get(key))
        if line:
            lines.append(line)

    # Extra usage (overuse billing)
    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used = (extra.get("used_credits") or 0) / 100
        limit = (extra.get("monthly_limit") or 0) / 100
        if limit > 0:
            pct = min(100, used / limit * 100)
            lines.append(
                f"*Extra usage:* {_escape_mdv2(f'${used:.2f} / ${limit:.2f} ({pct:.0f}%)')}"
            )

    if not lines:
        await message.reply_text("No usage data available\\.", parse_mode="MarkdownV2")
        return

    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")
