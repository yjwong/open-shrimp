"""Telegram command handlers (/context, /clear, /status, /cancel, /model,
/resume, /review, /mcp).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from open_udang.client_manager import (
    AgentSession,
    close_session,
    get_session,
)
from open_udang.config import Config
from open_udang.db import ChatScope, delete_session, get_session_id, set_session_id
from open_udang.handlers.state import (
    _MCP_STATUS_EMOJI,
    _RESUME_LIST_LIMIT,
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
        # List contexts
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
            lines = ["*Available contexts:*\n"]
            for name, ctx in config.contexts.items():
                marker = " \\(active\\)" if name == current else ""
                escaped_name = _escape_mdv2(name)
                escaped_desc = _escape_mdv2(ctx.description)
                lines.append(f"\u2022 `{escaped_name}` \\- {escaped_desc}{marker}")
            await message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
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
    await message.reply_text(
        f"Switched to context `{target_escaped}` \\- {desc}",
        parse_mode="MarkdownV2",
    )
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
        f"*Model:* `{ctx.model}`" + (" \\(override\\)" if scope in _model_overrides else ""),
        f"*Session:* {'`' + session_id[:12] + '...' + '`' if session_id else 'None'}",
        f"*Running:* {'Yes' if running else 'No'}",
        f"*Injectable:* {'Yes' if injectable else 'No'}",
        f"*Setup queued:* {setup_queued}",
    ]
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
                f"*Context default:* `{ctx_default_model}`\n\n"
                f"Use `/model reset` to revert\\."
            )
        else:
            text = f"*Model:* `{ctx_default_model}` \\(context default\\)"
        for ch in ".-/":
            text = text.replace(ch, f"\\{ch}")
        await message.reply_text(text, parse_mode="MarkdownV2")
        return

    target = args[1]

    if target == "reset":
        if current_override:
            del _model_overrides[scope]
            await close_session(scope)
            model_escaped = _escape_mdv2(ctx_default_model)
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


# ── /resume ──


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume command: list recent sessions or resume a specific one.

    Usage:
        /resume          - Show recent sessions for the current context
        /resume <id>     - Resume a session by ID (prefix match supported)
    """
    from claude_agent_sdk import list_sessions

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
        sessions = await asyncio.to_thread(list_sessions, directory=ctx.directory)
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

    # List recent sessions for the current context
    sessions = await asyncio.to_thread(
        list_sessions, directory=ctx.directory, limit=_RESUME_LIST_LIMIT
    )

    if not sessions:
        await message.reply_text(
            f"No sessions found for context `{_escape_mdv2(ctx_name)}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    current_session_id = await get_session_id(db, scope, ctx_name)

    buttons: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        summary = s.summary or "No summary"
        # Truncate long summaries for button text
        if len(summary) > 60:
            summary = summary[:57] + "..."
        sid_short = s.session_id[:8]
        marker = " (current)" if s.session_id == current_session_id else ""
        label = f"{sid_short} - {summary}{marker}"

        cb_data = f"resume:{s.session_id}"
        _resume_selections[cb_data] = s.session_id
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])

    keyboard = InlineKeyboardMarkup(buttons)
    await message.reply_text(
        f"*Recent sessions for* `{_escape_mdv2(ctx_name)}`*:*",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ── /resume callback handler ──


async def handle_resume_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /resume session selection callback. Returns True if handled."""
    if not data.startswith("resume:"):
        return False

    db: aiosqlite.Connection = context.bot_data["db"]
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

    if len(dirs) == 1:
        app_url = f"{base_url}/app/?chat_id={scope.chat_id}"
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
            app_url = f"{base_url}/app/?chat_id={scope.chat_id}&dir={i}"
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
