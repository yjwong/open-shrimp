"""Telegram command handlers (/start, /context, /clear, /status, /cancel, /model,
/effort, /resume, /review, /mcp, /tasks, /usage, /login).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from open_shrimp.mini_app import reply_mini_app
from open_shrimp.web_url import phone_websocket_base, public_base
from telegram.ext import ContextTypes

from open_shrimp.client_manager import (
    AgentSession,
    close_session,
    get_session,
)
from open_shrimp.config import (
    Config,
    ContextConfig,
    effective_backend,
    is_sandboxed,
    sandbox_backend,
)
from open_shrimp.markdown import escape_rich, escape_rich_inline
from open_shrimp.rich_message import (
    edit_message_rich,
    reply_rich,
    send_rich,
)
from open_shrimp.db import ChatScope, get_session_id, set_session_id
from open_shrimp.backend.factory import default_model_label, get_backend_by_name
from open_shrimp.android_companion import (
    create_pairing_code,
    get_or_create_server_id,
    list_android_devices,
    revoke_android_device,
)
from open_shrimp.handlers.state import (
    _MCP_STATUS_EMOJI,
    _RESUME_LIST_LIMIT,
    _active_bg_tasks,
    _effort_overrides,
    _injectable_sessions,
    _model_overrides,
    _resume_page_cache,
    _resume_selections,
    _resume_session_cache,
    _running_tasks,
    _setup_queues,
    clear_session_approvals,
    reset_scope,
)
from open_shrimp.handlers.utils import (
    _cancel_running,
    _get_context,
    _get_context_name,
    _get_locked_context,
    _is_authorized,
    _update_pinned_status,
    answer_no_context,
    chat_scope_from_message,
    get_backend_for_scope,
    reply_no_context,
    require_context,
)
from open_shrimp.supervisor import resolve_context, selectable_contexts
from open_shrimp.android_push import get_push_sender
from open_shrimp.security_key.api import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_SESSION_LIFETIME_SECONDS,
    create_security_key_session,
    get_or_create_registry,
    phone_url,
    security_key_destination_label,
)
from open_shrimp.security_key.bootstrap import start_vm_helper
from open_shrimp.security_key.db import get_security_key_session_record

logger = logging.getLogger(__name__)


def _is_private_chat(update: Update) -> bool:
    """Return True if this update is from a private (1:1) chat."""
    chat = update.effective_chat
    return chat is not None and chat.type == chat.PRIVATE


# ── /start ──


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command: welcome message for first-time users.

    A deep-link start payload is read by nobody here, and must stay that way.
    Enrollment lives in the setup wizard, which owns the poll while the core is
    stopped; a payload interpreted by the running bot would be an
    unauthenticated enrollment endpoint on a public username, permanently open.
    ``allowed_users`` is the only auth boundary in front of a bot that runs
    shell commands, so nothing reachable by a stranger may ever add to it.
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    resolved = await _get_context(scope, config, db)

    if resolved is None:
        # Setup writes no default_context, so an install with projects reaches
        # this card unbound; "none set up" would deny the project it has.
        working_in = (
            "**No project picked yet.** Choose one with /context."
            if config.contexts
            else "**No project set up yet.** Add one with /context."
        )
    else:
        ctx_name, ctx = resolved
        working_in = f"**Working in:** `{ctx_name}` → `{ctx.directory}`"

    lines = [
        "👋 **Welcome to OpenShrimp**",
        "",
        "You're connected to Claude. Just send a message (or voice note) — no command needed.",
        "",
        working_in,
        "",
        "**Commands worth knowing:**",
        "• /context — switch working directory",
        "• /clear — start a fresh session",
        "• /status — show current state",
    ]
    text = "\n".join(lines)
    await reply_rich(message, text)


# ── /context ──

_CONTEXT_PAGE_SIZE = 6


def _build_context_page(
    config: Config, current: str | None, page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a page of context buttons with optional pagination.

    Offers the supervisor alongside the configured projects — picking it is
    the only way into it, and on an install with no projects it is the only
    entry the picker has.
    """
    selectable = selectable_contexts(config)
    names = list(selectable.keys())
    total = len(names)
    total_pages = max(1, (total + _CONTEXT_PAGE_SIZE - 1) // _CONTEXT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _CONTEXT_PAGE_SIZE
    page_names = names[start : start + _CONTEXT_PAGE_SIZE]

    buttons: list[list[InlineKeyboardButton]] = []
    for name in page_names:
        ctx = selectable[name]
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

    text = "**Select a context:**"
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
            await edit_message_rich(query.message, text, reply_markup=markup)
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

        if ctx_name is not None and target == ctx_name:
            await reset_scope(scope, ctx_name, db)

        ctx = resolve_context(config, target)
        desc = escape_rich(ctx.description) if ctx else ""
        target_escaped = target
        try:
            await edit_message_rich(query.message, f"Switched to context `{target_escaped}` - {desc}\n_Started fresh session._", reply_markup=None)
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

        ctx = resolve_context(config, target)
        if ctx is None:
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

        # Selecting a context is how a scope leaves the unbound state, so this
        # path must complete even when there is nothing to clear.
        if current is not None:
            clear_session_approvals(scope, current)
        _model_overrides.pop(scope, None)
        _effort_overrides.pop(scope, None)
        await close_session(scope)

        from open_shrimp.db import set_active_context

        await set_active_context(db, scope, target)
        desc = escape_rich(ctx.description)
        target_escaped = target

        existing_session = await get_session_id(db, scope, target)
        if existing_session:
            text = f"Switched to context `{target_escaped}` - {desc}\n_Resuming existing session._"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Clear session", callback_data=f"ctx_clear:{target}"),
            ]])
        else:
            text = f"Switched to context `{target_escaped}` - {desc}"
            markup = None

        try:
            await edit_message_rich(query.message, text, reply_markup=markup)
        except Exception:
            logger.exception("Failed to update context message")

        await query.answer(f"Switched to {target}")
        await _update_pinned_status(context.bot, scope, target, ctx, db, config)
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
            escaped_name = locked
            escaped_desc = escape_rich(ctx.description)
            await reply_rich(message, f"This chat is locked to context `{escaped_name}` - {escaped_desc}")
        else:
            text, markup = _build_context_page(config, current, page=0)
            await reply_rich(message, text, reply_markup=markup)
        return

    # Switch context.  The supervisor is offered alongside the projects, so
    # the name list is never empty even on an install with no projects.
    selectable = selectable_contexts(config)
    target = args[1]
    if target not in selectable:
        names = ", ".join(f"`{n}`" for n in selectable)
        await reply_rich(message, f"Unknown context: `{target}`. Available: {names}")
        return

    locked = _get_locked_context(scope.chat_id, config)
    if locked:
        await reply_rich(message, f"This chat is locked to context `{locked}`.")
        return

    old_ctx_name = await _get_context_name(scope, config, db)
    # Switching in is how a scope leaves the unbound state; there is simply
    # nothing to clear when it was not bound before.
    if old_ctx_name is not None:
        clear_session_approvals(scope, old_ctx_name)
    _model_overrides.pop(scope, None)
    _effort_overrides.pop(scope, None)
    await close_session(scope)

    from open_shrimp.db import set_active_context

    await set_active_context(db, scope, target)
    ctx = selectable[target]
    desc = escape_rich(ctx.description)
    target_escaped = target

    existing_session = await get_session_id(db, scope, target)
    if existing_session:
        text = f"Switched to context `{target_escaped}` - {desc}\n_Resuming existing session._"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Clear session", callback_data=f"ctx_clear:{target}"),
        ]])
    else:
        text = f"Switched to context `{target_escaped}` - {desc}"
        markup = None

    await reply_rich(message, text, reply_markup=markup)
    await _update_pinned_status(context.bot, scope, target, ctx, db, config)


# ── /clear ──


async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command: start fresh session."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    # Sessions are keyed by context name, so an unbound scope has none.
    resolved = await require_context(message, scope, config, db)
    if resolved is None:
        return
    ctx_name, ctx = resolved

    await reset_scope(scope, ctx_name, db)

    if is_sandboxed(ctx):
        sandbox_managers = context.bot_data.get("sandbox_managers") or {}
        manager = sandbox_managers.get(sandbox_backend(ctx))
        if manager is not None:
            active = manager.get_active_sandbox(ctx_name)
            if active is not None and active.supports_port_forwarding():
                try:
                    await asyncio.to_thread(
                        active.cleanup_port_forwards, scope.key,
                    )
                except Exception:
                    logger.exception(
                        "Failed to clean up port forwards on /clear for %s",
                        ctx_name,
                    )

    await reply_rich(message, f"Started fresh session in context `{ctx_name}`.")
    await _update_pinned_status(context.bot, scope, ctx_name, ctx, db, config)


# ── /status ──


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command: show current state."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    resolved = await require_context(message, scope, config, db)
    if resolved is None:
        return
    ctx_name, ctx = resolved
    session_id = await get_session_id(db, scope, ctx_name)
    running = scope in _running_tasks and not _running_tasks[scope].done()
    injectable = scope in _injectable_sessions
    setup_queued = len(_setup_queues.get(scope, []))

    backend_name = effective_backend(ctx, config)
    model = ctx.model or default_model_label(backend_name)
    rows = [
        ("Context", f"`{ctx_name}`"),
        ("Directory", f"`{ctx.directory}`"),
        ("Backend", f"`{backend_name}`"),
        ("Model", f"`{model}`"
                  + (" (override)" if scope in _model_overrides else "")),
        ("Effort", f"`{ctx.effort or 'default'}`"
                   + (" (override)" if scope in _effort_overrides else "")),
        ("Session", f"`{session_id[:12]}...`" if session_id else "None"),
        ("Running", "Yes" if running else "No"),
        ("Injectable", "Yes" if injectable else "No"),
        ("Setup queued", str(setup_queued)),
    ]
    lines = ["| | |", "| :--- | :--- |"]
    lines.extend(f"| **{key}** | {value} |" for key, value in rows)

    scope_tasks = _active_bg_tasks.get(scope, {})
    if scope_tasks:
        now = time.monotonic()
        lines.append("")
        lines.append(f"**Background tasks ({len(scope_tasks)})**")
        lines.append("")
        lines.append("| Id | Type | Description | Elapsed |")
        lines.append("| :--- | :--- | :--- | ---: |")
        for task in scope_tasks.values():
            elapsed = int(now - task.started_at)
            minutes, seconds = divmod(elapsed, 60)
            duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
            lines.append(
                f"| `{task.task_id[:12]}` "
                f"| {escape_rich_inline(task.task_type or 'unknown')} "
                f"| {escape_rich_inline(task.description or 'N/A')} "
                f"| {duration} |"
            )
    await reply_rich(message, "\n".join(lines))


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
        text = ". ".join(parts) + "."
        await reply_rich(message, text)
    else:
        await reply_rich(message, "Nothing running.")


# ── /model ──


def _build_model_page(
    backend: Any, ctx_default_model: str | None, current_override: str | None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render the /model status text plus a picker for the backend's catalog.

    Backends with an empty catalog get text only — there is nothing to offer
    as a button, so the picker is absent rather than empty.
    """
    unpinned = default_model_label(backend.name)
    in_effect = current_override or ctx_default_model or unpinned
    label = "override" if current_override else "context default"
    lines = [f"**Model:** `{in_effect}` ({label})"]
    if current_override:
        lines.append(
            "**Context default:** "
            f"`{ctx_default_model or unpinned}`"
        )

    catalog = backend.model_catalog()
    if not catalog:
        lines.append("")
        lines.append("`/model <id>` to override, `/model reset` to revert.")
        return "\n".join(lines), None

    buttons: list[list[InlineKeyboardButton]] = []
    for choice in catalog:
        selected = in_effect in (choice.alias, choice.model_id)
        buttons.append([
            InlineKeyboardButton(
                f"{'• ' if selected else ''}{choice.alias} — {choice.model_id}",
                callback_data=f"model:{choice.alias}",
            )
        ])
    if current_override:
        buttons.append([
            InlineKeyboardButton(
                "↩ Revert to context default", callback_data="model_reset"
            )
        ])

    lines.append("")
    lines.append("_Pick a model, or `/model <id>` for one not listed._")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def handle_model_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /model picker presses. Returns True if handled."""
    if not (data.startswith("model:") or data == "model_reset"):
        return False

    db: aiosqlite.Connection = context.bot_data["db"]
    if not query.message:
        await query.answer("Cannot determine chat.")
        return True

    scope = chat_scope_from_message(query.message)
    ctx_name = await _get_context_name(scope, config, db)
    ctx = resolve_context(config, ctx_name)
    if ctx is None:
        await answer_no_context(query, config)
        return True
    backend = get_backend_by_name(effective_backend(ctx, config))

    if data == "model_reset":
        _model_overrides.pop(scope, None)
        answer = "Reverted to context default"
    else:
        # Store the canonical name, same as the text path.
        alias = data[len("model:"):]
        _model_overrides[scope] = backend.normalize_model(alias) or alias
        answer = f"Model set to {alias}"

    await close_session(scope)

    text, markup = _build_model_page(
        backend, ctx.model, _model_overrides.get(scope)
    )
    try:
        await edit_message_rich(query.message, text, reply_markup=markup)
    except Exception:
        logger.exception("Failed to update model message")

    await query.answer(answer)
    return True


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

    if not _is_private_chat(update):
        await reply_rich(message, "This command can only be used in private chats.")
        return

    scope = chat_scope_from_message(message)
    ctx_name = await _get_context_name(scope, config, db)
    # The configured default, deliberately read unmerged: _get_context folds
    # any active /model override into its copy, which is the value this
    # command exists to show separately from.
    ctx = resolve_context(config, ctx_name)
    if ctx is None:
        await reply_no_context(message, config)
        return
    ctx_default_model = ctx.model
    backend = get_backend_by_name(effective_backend(ctx, config))
    current_override = _model_overrides.get(scope)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        text, markup = _build_model_page(
            backend, ctx_default_model, current_override
        )
        await reply_rich(message, text, reply_markup=markup)
        return

    target = args[1]

    if target == "reset":
        if current_override:
            del _model_overrides[scope]
            await close_session(scope)
            model_escaped = ctx_default_model or default_model_label(backend.name)
            await reply_rich(message, f"Model override cleared. Using context default: `{model_escaped}`")
        else:
            await reply_rich(message, "No model override active.")
        return

    # Store the canonical name so the serving binary's own alias table never
    # gets a say in which model this scope runs.  An unrecognised value is
    # still honoured — the backend gates the warning, not the override.
    resolved = backend.normalize_model(target) or target
    _model_overrides[scope] = resolved
    await close_session(scope)

    shown = resolved
    if resolved != target:
        shown = f"`{target}` → `{shown}`"
    else:
        shown = f"`{shown}`"
    warning = (
        ""
        if backend.is_known_model(target)
        else "\n⚠️ Not a known alias or model ID — passing through as-is."
    )
    await reply_rich(message, f"Model overridden to {shown}.{warning} Use `/model reset` to revert.")


# ── /effort ──


_VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


async def effort_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /effort command: show or override the thinking effort level.

    Usage:
        /effort              -- show current effort level (and override if active)
        /effort <level>      -- override for this chat (low, medium, high, xhigh, max)
        /effort reset        -- clear the override, revert to context default
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await reply_rich(message, "This command can only be used in private chats.")
        return

    scope = chat_scope_from_message(message)
    ctx_name = await _get_context_name(scope, config, db)
    # Unmerged for the same reason as /model: _get_context would fold an
    # active /effort override into the value shown as the context default.
    ctx_unmerged = resolve_context(config, ctx_name)
    if ctx_unmerged is None:
        await reply_no_context(message, config)
        return
    ctx_default_effort = ctx_unmerged.effort
    current_override = _effort_overrides.get(scope)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # Show current effort
        if current_override:
            text = (
                f"**Effort:** `{current_override}` (override)\n"
                f"**Context default:** `{ctx_default_effort or 'default'}`\n\n"
                f"Use `/effort reset` to revert."
            )
        else:
            text = f"**Effort:** `{ctx_default_effort or 'default'}` (context default)"
        text += "\n\nLevels: `low`, `medium`, `high`, `xhigh`, `max`"
        await reply_rich(message, text)
        return

    target = args[1].lower()

    if target == "reset":
        if current_override:
            del _effort_overrides[scope]
            await close_session(scope)
            effort_escaped = ctx_default_effort or "default"
            await reply_rich(message, f"Effort override cleared. Using context default: `{effort_escaped}`")
        else:
            await reply_rich(message, "No effort override active.")
        return

    if target not in _VALID_EFFORT_LEVELS:
        levels = ", ".join(f"`{lvl}`" for lvl in _VALID_EFFORT_LEVELS)
        await reply_rich(message, f"Invalid effort level: `{target}`. Valid: {levels}")
        return

    # Set override
    _effort_overrides[scope] = target
    await close_session(scope)
    effort_escaped = target
    await reply_rich(message, f"Effort overridden to `{effort_escaped}`. "
        f"Use `/effort reset` to revert.")


# ── /add_dir ──


async def add_dir_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add_dir command: add, remove, or list runtime additional directories.

    Usage:
        /add_dir                    -- list current additional directories
        /add_dir <path>             -- add a directory
        /add_dir remove <path>      -- remove a previously added directory
    """
    import os

    from open_shrimp.config import is_sandboxed
    from open_shrimp.db import (
        add_additional_directory,
        get_additional_directories,
        remove_additional_directory,
    )
    from open_shrimp.handlers.state import _additional_dir_cache

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await reply_rich(message, "This command can only be used in private chats.")
        return

    scope = chat_scope_from_message(message)
    resolved = await require_context(message, scope, config, db)
    if resolved is None:
        return
    ctx_name, ctx = resolved
    ctx_dir = ctx.directory

    # Parse: strip the /add_dir command, then check for "remove" prefix.
    # Join remaining tokens to support paths with spaces.
    raw = (message.text or "").strip()
    # Remove the /add_dir (or /add_dir@botname) prefix.
    rest = raw.split(None, 1)[1].strip() if " " in raw else ""

    if not rest:
        # List directories.  Read unmerged: ``ctx`` already folds in the
        # runtime ``/add_dir`` entries this branch reports separately.
        unmerged = resolve_context(config, ctx_name)
        base_dirs = unmerged.additional_directories if unmerged else []
        runtime_dirs = await get_additional_directories(db, scope, ctx_name)

        lines: list[str] = []
        if base_dirs:
            lines.append("**Config directories:**")
            for d in base_dirs:
                lines.append(f"  `{d}`")
        if runtime_dirs:
            if lines:
                lines.append("")
            lines.append("**Runtime directories (/add_dir):**")
            for d in runtime_dirs:
                lines.append(f"  `{d}`")
        if not lines:
            lines.append("No additional directories configured.")
        else:
            lines.append("")
            lines.append("Use `/add_dir <path>` to add, `/add_dir remove <path>` to remove.")

        await reply_rich(message, "\n".join(lines))
        return

    # Check for "remove" subcommand
    rest_parts = rest.split(None, 1)
    if rest_parts[0] == "remove":
        remove_path = rest_parts[1].strip() if len(rest_parts) > 1 else ""
        if not remove_path:
            await reply_rich(message, "Usage: `/add_dir remove <path>`")
            return
        target = os.path.expanduser(remove_path)
        removed = await remove_additional_directory(db, scope, ctx_name, target)
        if not removed:
            await reply_rich(message, f"Directory not found in runtime list: `{target}`")
            return

        # Update cache
        _additional_dir_cache.pop((scope, ctx_name), None)

        # Reconnect session and invalidate sandbox
        await _reconnect_after_dir_change(scope, ctx_name, ctx, context)

        await reply_rich(message, f"Removed `{target}`. Session will reconnect on next message.")
        return

    # Add directory — resolve relative paths against the context directory.
    target = os.path.expanduser(rest)
    if not os.path.isabs(target):
        target = os.path.join(ctx_dir, target)
    target = os.path.realpath(target)

    if not os.path.isdir(target):
        await reply_rich(message, f"Directory does not exist: `{target}`")
        return

    # Check for duplicates against context dir, config dirs, and runtime dirs.
    # Canonicalize everything so symlinks don't bypass the check.
    canonical_existing = {os.path.realpath(d) for d in ctx.additional_directories}
    canonical_existing.add(os.path.realpath(ctx_dir))
    if target in canonical_existing:
        await reply_rich(message, f"`{target}` is already included.")
        return

    # Store pending add and show inline keyboard with short callback keys.
    import uuid

    from open_shrimp.handlers.state import _pending_add_dirs

    key = uuid.uuid4().hex[:12]
    _pending_add_dirs[key] = (scope, ctx_name, target)

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("This session", callback_data=f"adddir_s:{key}"),
            InlineKeyboardButton("Remember", callback_data=f"adddir_r:{key}"),
        ],
    ])
    await reply_rich(message, f"Add `{target}` to *{escape_rich(ctx_name)}*?", reply_markup=markup)


async def handle_add_dir_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /add_dir inline keyboard callbacks. Returns True if handled."""
    if not data.startswith(("adddir_s:", "adddir_r:")):
        return False

    from pathlib import Path

    from open_shrimp.config import load_config, load_raw_yaml, write_raw_yaml
    from open_shrimp.db import add_additional_directory
    from open_shrimp.handlers.state import _additional_dir_cache, _pending_add_dirs

    db: aiosqlite.Connection = context.bot_data["db"]

    # Parse: "adddir_{s|r}:{key}"
    prefix, key = data.split(":", 1)
    action = "session" if prefix == "adddir_s" else "remember"

    pending = _pending_add_dirs.pop(key, None)
    if pending is None:
        await query.answer("This action has expired.")
        return True

    scope, ctx_name, target = pending
    ctx = config.contexts.get(ctx_name)
    if ctx is None:
        await query.answer("Context no longer exists.")
        return True

    if action == "session":
        # Store in DB only — persists across messages but not bot restarts.
        await add_additional_directory(db, scope, ctx_name, target)
        _additional_dir_cache.pop((scope, ctx_name), None)
        await _reconnect_after_dir_change(scope, ctx_name, ctx, context)

        try:
            await edit_message_rich(query.message, f"Added `{target}` to *{escape_rich(ctx_name)}* "
                f"(this session).\n"
                f"Session will reconnect on next message.", reply_markup=None)
        except Exception:
            pass
        await query.answer()
        return True

    if action == "remember":
        # Write to config.yaml so it persists across restarts.
        config_path_str: str | None = context.bot_data.get("config_path")
        if not config_path_str:
            from open_shrimp.config import DEFAULT_CONFIG_PATH
            config_path_str = str(DEFAULT_CONFIG_PATH)

        config_path = Path(config_path_str)
        try:
            raw = load_raw_yaml(config_path)
            ctx_raw = raw.get("contexts", {}).get(ctx_name, {})
            dirs = ctx_raw.get("additional_directories", [])
            if target not in dirs:
                dirs.append(target)
                ctx_raw["additional_directories"] = dirs
            write_raw_yaml(config_path, raw)
            # Reload config eagerly (hot-reload watcher will also fire).
            new_config = load_config(config_path_str)
            context.bot_data["config"] = new_config
        except Exception:
            logger.exception("Failed to write config for /add_dir remember")
            await query.answer("Failed to update config file.")
            return True

        # No DB entry needed — it's in the config now.
        _additional_dir_cache.pop((scope, ctx_name), None)

        updated_ctx = new_config.contexts.get(ctx_name, ctx)
        await _reconnect_after_dir_change(scope, ctx_name, updated_ctx, context)

        try:
            await edit_message_rich(query.message, f"Added `{target}` to *{escape_rich(ctx_name)}* "
                f"(saved to config).\n"
                f"Session will reconnect on next message.", reply_markup=None)
        except Exception:
            pass
        await query.answer()
        return True

    return False


async def _reconnect_after_dir_change(
    scope: ChatScope,
    ctx_name: str,
    ctx: ContextConfig,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Close session and invalidate sandbox after directory changes."""
    from open_shrimp.config import is_sandboxed
    from open_shrimp.handlers.messages import _select_sandbox_manager

    await close_session(scope)

    if is_sandboxed(ctx):
        manager = _select_sandbox_manager(context.bot_data, ctx)
        if manager is not None:
            manager.invalidate_sandbox(ctx_name)


# ── /resume ──


def _relative_time(epoch_ms: int | None) -> str:
    """Format an epoch-millisecond timestamp as a human-readable relative time."""
    if not epoch_ms:
        return "unknown"
    delta = time.time() - epoch_ms / 1000
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h}h ago"
    if delta < 604800:
        d = int(delta / 86400)
        return f"{d}d ago"
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.strftime("%b %d")


async def _build_resume_page(
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    scope: ChatScope,
    page: int,
    sandbox_managers: "dict[str, Any] | None" = None,
    backend: Any = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build a single page of the resume session list.

    Returns ``(text, keyboard)`` where *keyboard* is ``None`` when there are
    no sessions at all.
    """
    from open_shrimp.client_manager import resolve_backend

    backend = resolve_backend(backend, context=ctx)
    per_page = _RESUME_LIST_LIMIT
    offset = page * per_page
    # Fetch one extra to detect whether a next page exists.
    sessions = await backend.list_sessions(
        ctx.directory,
        limit=per_page + 1,
        offset=offset,
        ctx=ctx,
        ctx_name=ctx_name,
        sandbox_managers=sandbox_managers,
    )

    if not sessions:
        if page == 0:
            return (
                f"No sessions found for context `{ctx_name}`.",
                None,
            )
        # Edge case: page beyond last – go back.
        return await _build_resume_page(
            ctx_name, ctx, db, scope, page - 1,
            sandbox_managers=sandbox_managers,
            backend=backend,
        )

    has_next = len(sessions) > per_page
    sessions = sessions[:per_page]

    current_session_id = await get_session_id(db, scope, ctx_name)

    buttons: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        summary = s.summary or "No summary"
        rel = _relative_time(s.last_modified)
        marker = " \u2713" if s.session_id == current_session_id else ""
        # Truncate summary to fit button with timestamp and marker.
        max_summary = 44
        if len(summary) > max_summary:
            summary = summary[:max_summary - 3] + "..."
        label = f"{rel} - {summary}{marker}"
        resume_data = f"resume:{s.session_id}"
        info_data = f"resume_info:{s.session_id}"
        _resume_selections[resume_data] = s.session_id
        _resume_session_cache[s.session_id] = s
        _resume_page_cache[s.session_id] = (ctx_name, page)
        buttons.append([
            InlineKeyboardButton(label, callback_data=resume_data),
            InlineKeyboardButton("\u2139\ufe0f", callback_data=info_data),
        ])

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

    page_label = f" (page {page + 1})" if page > 0 or has_next else ""
    text = f"**Recent sessions for** `{ctx_name}`**:**{page_label}"
    return text, InlineKeyboardMarkup(buttons)


def _build_resume_detail(
    session_id: str,
    ctx_name: str,
    current_session_id: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the detail view for a single session."""
    s = _resume_session_cache.get(session_id)
    if not s:
        text = "Session info has expired. Use /resume to list again."
        keyboard = InlineKeyboardMarkup([])
        return text, keyboard

    lines: list[str] = []
    lines.append(f"**Session details**\n")

    if s.custom_title:
        lines.append(f"**Title:** {escape_rich(s.custom_title)}")
    lines.append(f"**Summary:** {escape_rich(s.summary or 'No summary')}")

    if s.first_prompt:
        prompt = s.first_prompt
        if len(prompt) > 200:
            prompt = prompt[:197] + "..."
        lines.append(f"**First prompt:** {escape_rich(prompt)}")

    if s.git_branch:
        lines.append(f"**Branch:** `{s.git_branch}`")

    lines.append(f"**Created:** {escape_rich(_relative_time(s.created_at))}")
    lines.append(f"**Last active:** {escape_rich(_relative_time(s.last_modified))}")

    if s.file_size:
        size_kb = s.file_size / 1024
        if size_kb >= 1024:
            size_str = f"{size_kb / 1024:.1f} MB"
        else:
            size_str = f"{size_kb:.0f} KB"
        lines.append(f"**Size:** {escape_rich(size_str)}")

    lines.append(f"**ID:** `{s.session_id}`")

    if s.session_id == current_session_id:
        lines.append("\n_This is the current session._")

    text = "\n".join(lines)

    resume_data = f"resume:{s.session_id}"
    _resume_selections[resume_data] = s.session_id
    ctx_name_cached, page = _resume_page_cache.get(s.session_id, (ctx_name, 0))
    back_data = f"resume_page:{ctx_name_cached}:{page}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u25b6\ufe0f Resume", callback_data=resume_data)],
        [InlineKeyboardButton("\u25c0 Back to list", callback_data=back_data)],
    ])
    return text, keyboard


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume command: list recent sessions or resume a specific one.

    Usage:
        /resume          - Show recent sessions for the current context
        /resume <id>     - Resume a session by ID (prefix match supported)
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    sandbox_managers = context.bot_data.get("sandbox_managers")
    backend = context.bot_data.get("backend")
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    resolved = await require_context(message, scope, config, db)
    if resolved is None:
        return
    ctx_name, ctx = resolved

    args = message.text.split() if message.text else []

    if len(args) >= 2:
        from open_shrimp.client_manager import resolve_backend

        # Direct resume by session ID (or prefix).
        target = args[1]
        sessions = await resolve_backend(backend, context=ctx).list_sessions(
            ctx.directory,
            ctx=ctx,
            ctx_name=ctx_name,
            sandbox_managers=sandbox_managers,
        )
        match = None
        for s in sessions:
            if s.session_id == target or s.session_id.startswith(target):
                match = s
                break

        if not match:
            await reply_rich(message, f"No session matching `{target}` found in context `{ctx_name}`.")
            return

        await close_session(scope)
        await set_session_id(db, scope, ctx_name, match.session_id)
        summary = escape_rich(match.summary or "No summary")
        await reply_rich(message, f"Resumed session `{match.session_id[:12]}...`\n_{summary}_")
        await _update_pinned_status(context.bot, scope, ctx_name, ctx, db, config)
        return

    # List recent sessions for the current context (page 0)
    text, keyboard = await _build_resume_page(
        ctx_name, ctx, db, scope, page=0,
        sandbox_managers=sandbox_managers,
        backend=backend,
    )

    if keyboard is None:
        await reply_rich(message, text)
        return

    await reply_rich(message, text, reply_markup=keyboard)


# ── /resume callback handler ──


async def handle_resume_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /resume session selection callback. Returns True if handled."""
    prefixes = ("resume:", "resume_page:", "resume_info:")
    if not any(data.startswith(p) for p in prefixes):
        return False

    db: aiosqlite.Connection = context.bot_data["db"]
    sandbox_managers = context.bot_data.get("sandbox_managers")
    backend = context.bot_data.get("backend")

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
        # The keyboard carries the context it was rendered for; if that
        # context is gone there is no meaningful page to redraw.
        ctx = resolve_context(config, ctx_name_req)
        if ctx is None:
            await query.answer("Context no longer exists.")
            return True
        text, keyboard = await _build_resume_page(
            ctx_name_req, ctx, db, scope, page,
            sandbox_managers=sandbox_managers,
            backend=backend,
        )
        await query.answer()
        try:
            await edit_message_rich(query.message, text=text, reply_markup=keyboard)
        except Exception:
            logger.exception("Failed to update resume page")
        return True

    # Handle session detail view
    if data.startswith("resume_info:"):
        session_id = data[len("resume_info:"):]
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True
        scope = chat_scope_from_message(query.message)
        # Only the name is needed, so this skips _get_context's runtime-dirs
        # lookup and override merge.
        ctx_name = await _get_context_name(scope, config, db)
        if ctx_name is None:
            await answer_no_context(query, config)
            return True
        current_session_id = await get_session_id(db, scope, ctx_name)
        text, keyboard = _build_resume_detail(
            session_id, ctx_name, current_session_id,
        )
        await query.answer()
        try:
            await edit_message_rich(query.message, text=text, reply_markup=keyboard)
        except Exception:
            logger.exception("Failed to show session detail")
        return True

    # Handle session resume
    session_id = _resume_selections.pop(data, None)
    if not session_id:
        await query.answer("This selection has expired.")
        return True

    if not query.message:
        await query.answer("Cannot determine chat.")
        return True

    scope = chat_scope_from_message(query.message)

    resolved = await _get_context(scope, config, db)
    if resolved is None:
        await answer_no_context(query, config)
        return True
    ctx_name, ctx = resolved
    await close_session(scope)
    await set_session_id(db, scope, ctx_name, session_id)
    await query.answer(f"Resumed session {session_id[:8]}...")

    try:
        summary_text = f"\u2705 Resumed session `{session_id[:12]}...`"
        await edit_message_rich(query.message, text=summary_text, reply_markup=None)
    except Exception:
        logger.exception("Failed to update resume message")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("Failed to remove resume keyboard")

    await _update_pinned_status(
        context.bot, scope, ctx_name, ctx, db, config
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

    resolved = await require_context(update.message, scope, config, db)
    if resolved is None:
        return
    context_name, ctx = resolved

    escaped_context = escape_rich(context_name)
    dirs = [ctx.directory] + (ctx.additional_directories or [])
    thread_param = f"&thread_id={scope.thread_id}" if scope.thread_id is not None else ""

    if len(dirs) == 1:
        buttons = [(
            "\U0001f4dd Open Review",
            f"/app/?chat_id={scope.chat_id}{thread_param}",
        )]
        escaped_dir = ctx.directory
        text = (
            f"Review changes in *{escaped_context}*\n"
            f"\U0001f4c1 `{escaped_dir}`"
        )
    else:
        # Multiple directories: one button per directory.
        buttons = [
            (
                f"\U0001f4c1 {d.rstrip('/').rsplit('/', 1)[-1]}",
                f"/app/?chat_id={scope.chat_id}&dir={i}{thread_param}",
            )
            for i, d in enumerate(dirs)
        ]
        text = f"Review changes in *{escaped_context}*"

    await reply_mini_app(
        update.message,
        text=text,
        buttons=buttons,
        config=config,
        user_id=update.effective_user.id,
        is_private_chat=_is_private_chat(update),
        opens="the page that shows what I've changed",
        still_works=(
            "Chatting with me here still works, and so does everything I do "
            "to your files — you just can't see the changes side by side. Ask "
            "me what I changed and I'll describe it in the chat."
        ),
    )


# ── /vnc ──


async def _open_vnc_viewer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    phone: bool,
) -> None:
    """Shared body for /vnc and /phone: open the VNC Mini App.

    Both open the same whole-desktop viewer; ``phone`` only changes the
    capability gate (phone_use vs computer_use) and the button/message label,
    since a phone-use context shows Android inside the same labwc desktop.
    """
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)

    if not _is_authorized(update.effective_user.id, config):
        return

    resolved = await require_context(update.message, scope, config, db)
    if resolved is None:
        return
    context_name, ctx = resolved

    # The screen the user sees, which is not the capability's name: a
    # phone-use context shows Android inside the same labwc desktop, and the
    # config key gating a desktop is ``computer_use``.
    noun = "phone" if phone else "desktop"
    if phone:
        enabled = ctx.sandbox is not None and ctx.sandbox.phone_use
        capability = "phone use"
    else:
        enabled = ctx.sandbox is not None and ctx.sandbox.computer_use
        capability = "computer use"
    if not enabled:
        await reply_rich(update.message, f"Context `{context_name}` does not have "
            f"{capability} enabled.")
        return

    escaped_context = escape_rich(context_name)
    await reply_mini_app(
        update.message,
        text=f"{noun.capitalize()} for *{escaped_context}*",
        buttons=[(f"View {noun}", f"/vnc/?context={context_name}")],
        config=config,
        user_id=update.effective_user.id,
        is_private_chat=_is_private_chat(update),
        opens=f"the live view of the {noun}",
        still_works=(
            f"I can still use the {noun} myself, and I can take a "
            "screenshot and send it to you here whenever you ask — you just "
            "can't watch or click on it live."
        ),
    )


async def vnc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /vnc -- open the VNC Mini App for the current context's desktop."""
    await _open_vnc_viewer(update, context, phone=False)


async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /phone -- open the VNC Mini App for a phone-use context."""
    await _open_vnc_viewer(update, context, phone=True)


# ── /security_key ──


def _get_sandbox_for_security_key(
    context_name: str,
    ctx: ContextConfig,
    sandbox_managers: dict[str, object] | None,
) -> object | None:
    if ctx.sandbox is None or not ctx.sandbox.computer_use:
        return None
    manager = (sandbox_managers or {}).get(ctx.sandbox.backend)
    create = getattr(manager, "create_sandbox", None) if manager is not None else None
    if create is None:
        return None
    return create(context_name, ctx)


def _push_status_text(push_status: object) -> str:
    status = push_status if isinstance(push_status, str) else None
    if status == "sent":
        return r"Push notification sent to the paired Android device."
    if status == "no_device":
        return (
            "No paired Android device with push is available; open the Android app "
            r"and use Find pending session."
        )
    if status == "not_configured":
        return r"Push is not configured; open the Android app and use Find pending session."
    if status in {"failed", "missing_token", "unsupported_provider"}:
        return (
            rf"Push delivery failed (`{status}`); open the Android app "
            r"and use Find pending session."
        )
    return (
        "Push status is pending; open the Android app and use Find pending session "
        r"if no notification arrives."
    )


async def security_key_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /security_key -- create a short-lived manual forwarding session."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    if not _is_authorized(update.effective_user.id, config):
        return
    show_manual_fallback = any(
        arg.lower() in {"debug", "advanced", "manual"}
        for arg in (context.args or [])
    )

    scope = chat_scope_from_message(update.message)
    resolved = await require_context(update.message, scope, config, db)
    if resolved is None:
        return
    context_name, ctx = resolved
    if ctx.sandbox is None or not ctx.sandbox.computer_use:
        await reply_rich(update.message, rf"Context `{context_name}` does not have computer use enabled.")
        return

    registry = get_or_create_registry(context.bot_data)
    sandbox = await asyncio.to_thread(
        _get_sandbox_for_security_key,
        context_name,
        ctx,
        context.bot_data.get("sandbox_managers"),
    )
    sandbox_id = context_name
    session = await create_security_key_session(
        db,
        registry=registry,
        config=config,
        push_sender=get_push_sender(context.bot_data, config),
        scope=scope,
        context_name=context_name,
        sandbox_id=sandbox_id,
        lifetime_seconds=DEFAULT_SESSION_LIFETIME_SECONDS,
        idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
    )

    session_phone_url = phone_url(config, session)
    if sandbox is not None:
        relay_base = f"ws://{sandbox.host_address}:{config.review.port}"
    else:
        relay_base = phone_websocket_base(config)
    helper_cmd = (
        "sudo openshrimp-security-key-vm-helper "
        f"--relay-url {relay_base} "
        f"--session-id {session.id} "
        f"--token {session.vm_token}"
    )
    helper_result = None
    if sandbox is not None:
        helper_result = await start_vm_helper(
            sandbox,
            relay_url=relay_base,
            session_id=session.id,
            token=session.vm_token,
            context_name=context_name,
            logger=logger,
        )
    helper_started = helper_result.started if helper_result is not None else False
    helper_error = helper_result.error if helper_result is not None else None

    helper_status = (
        r"VM helper started automatically. Fallback command:"
        if helper_started
        else r"VM helper was not started automatically. Run this in the computer-use VM:"
    )
    record = await get_security_key_session_record(db, session_id=session.id)
    destination_label = security_key_destination_label(config, context_name, sandbox_id)
    manual_fallback_lines = (
        [
            r"Manual phone URL (advanced debug fallback):",
            f"`{session_phone_url}`",
        ]
        if show_manual_fallback
        else [
            r"Manual phone URL is hidden by default. Use `/security_key debug` "
            r"only if paired Android claim is unavailable.",
        ]
    )

    text = "\n".join(
        [
            r"Security key forwarding request created.",
            f"Destination: `{destination_label}`",
            "",
            rf"Session expires in `{DEFAULT_SESSION_LIFETIME_SECONDS}s`; idle timeout is `{DEFAULT_IDLE_TIMEOUT_SECONDS}s`.",
            _push_status_text(record["push_status"] if record is not None else None),
            *manual_fallback_lines,
            "",
            helper_status,
            f"`{helper_cmd}`",
            *(
                [
                    "",
                    rf"Auto-start error: `{helper_error}`",
                ]
                if helper_error
                else []
            ),
            "",
            r"The Android app must still require fresh local device approval before forwarding.",
        ]
    )
    await reply_rich(update.message, text)


async def pair_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pair -- manage Android companion pairing."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    if not _is_authorized(update.effective_user.id, config):
        return

    args = context.args or []
    action = args[0].lower() if args else "code"
    if action in {"code", "new"}:
        pairing = await create_pairing_code(db)
        server_id = await get_or_create_server_id(db)
        base = public_base(config)
        pairing_url = f"openshrimp://pair?base_url={base}&code={pairing['code']}"
        text = "\n".join(
            [
                r"Android companion pairing code created.",
                "",
                f"Code: `{pairing['code']}`",
                f"Server: `{server_id}`",
                f"Base URL: `{base}`",
                f"Deep link: `{pairing_url}`",
                "",
                r"The code expires in `10 minutes` and can be used once.",
            ]
        )
        await reply_rich(update.message, text)
        return

    if action in {"list", "devices"}:
        devices = await list_android_devices(db)
        if not devices:
            await reply_rich(update.message, r"No Android companion devices are paired.")
            return
        lines = ["Android companion devices:", ""]
        for device in devices:
            if device["revoked_at"] is not None:
                status = "revoked"
            elif device["active"]:
                status = "active"
            else:
                status = "inactive"
            push = device["push_provider"] or "no push"
            last_seen = (
                datetime.fromtimestamp(
                    device["last_seen_at"], tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                if device["last_seen_at"] is not None
                else "never"
            )
            lines.append(
                f"• `{device['device_id']}` — "
                f"{escape_rich(device['display_name'])} "
                rf"({escape_rich(status)}, {escape_rich(push)}, "
                rf"last seen {escape_rich(last_seen)})"
            )
        lines.extend(
            [
                "",
                "Only one Android companion can be active in this release; "
                r"pairing a new phone deactivates the previous one.",
                r"Revoke with `/pair revoke <device_id>`.",
            ]
        )
        await reply_rich(update.message, "\n".join(lines))
        return

    if action == "revoke" and len(args) >= 2:
        device_id = args[1]
        if await revoke_android_device(db, device_id):
            await reply_rich(update.message, r"Android companion device revoked. It can no longer claim pending sessions or receive new push requests.")
        else:
            await reply_rich(update.message, r"No matching active Android companion device found.")
        return

    await reply_rich(update.message, r"Usage: `/pair`, `/pair list`, or `/pair revoke <device_id>`.")


# ── /login ──


async def login_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /login -- open the login Mini App to re-authenticate."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]

    if not _is_authorized(update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await reply_rich(update.message, "This command can only be used in private chats.")
        return

    scope = chat_scope_from_message(update.message)
    backend = get_backend_for_scope(context.bot_data, scope)
    if backend is not None and "login" not in backend.command_capabilities():
        await reply_rich(update.message, f"/login is not available on the `{backend.name}` backend.")
        return

    body = "Re-authenticate"
    if backend is not None:
        body = backend.copy().login_mini_app_body or body

    # The sign-in page is the only way to authenticate from Telegram, so its
    # explanation names that consequence rather than leaving the user to
    # discover it as a failed turn later.
    await reply_mini_app(
        update.message,
        text=escape_rich(body),
        buttons=[("Open login", "/terminal/?mode=login")],
        config=config,
        user_id=update.effective_user.id,
        is_private_chat=_is_private_chat(update),
        opens="the sign-in page",
        still_works=(
            "This is the only way to sign in from Telegram, so until it's "
            "fixed I can't answer anything that needs an account — if I've "
            "been telling you authentication failed, this is why. Everything "
            "else about the bot is fine."
        ),
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
    backend = get_backend_for_scope(context.bot_data, scope)
    if backend is not None and "mcp" not in backend.command_capabilities():
        await reply_rich(message, f"/mcp is not available on the `{backend.name}` backend.")
        return

    session = get_session(scope)
    if session is None:
        await reply_rich(message, "No active session. Send a message first to start a session, "
            "then use /mcp to manage MCP servers.")
        return

    args = message.text.split() if message.text else []
    subcommand = args[1] if len(args) >= 2 else None
    server_name = " ".join(args[2:]) if len(args) >= 3 else None

    if subcommand is None:
        # List all MCP servers
        await _mcp_list(message, session)
    elif subcommand == "reset":
        if not server_name:
            await reply_rich(message, "Usage: `/mcp reset <server-name>`")
            return
        await _mcp_reconnect(message, session, server_name)
    elif subcommand in ("enable", "disable"):
        if not server_name:
            await reply_rich(message, f"Usage: `/mcp {subcommand} <server-name>`")
            return
        await _mcp_toggle(message, session, server_name, enabled=(subcommand == "enable"))
    else:
        await reply_rich(message, "Unknown subcommand. Usage:\n"
            "`/mcp` \u2014 list servers\n"
            "`/mcp reset <name>` \u2014 reconnect a server\n"
            "`/mcp enable <name>` \u2014 enable a server\n"
            "`/mcp disable <name>` \u2014 disable a server")


async def _mcp_list(message: Any, session: AgentSession) -> None:
    """Fetch and display MCP server status."""
    try:
        status_resp = await session.client.get_mcp_status()
    except Exception:
        logger.exception("Failed to get MCP status")
        await reply_rich(message, "Failed to retrieve MCP server status.")
        return

    servers = status_resp.get("mcpServers", [])
    if not servers:
        await reply_rich(message, "No MCP servers configured.")
        return

    lines: list[str] = ["**MCP Servers**\n"]
    for srv in servers:
        name = srv.get("name", "unknown")
        status = srv.get("status", "unknown")
        emoji = _MCP_STATUS_EMOJI.get(status, "\u2753")
        scope = srv.get("scope", "")

        line = f"{emoji} *{escape_rich(name)}*"
        if scope:
            line += f" ({escape_rich(scope)})"
        line += f" \u2014 {escape_rich(status)}"

        # Show server info (version) when connected
        server_info = srv.get("serverInfo")
        if server_info:
            version = server_info.get("version", "")
            if version:
                line += f" v{escape_rich(version)}"

        # Show error message for failed servers
        error = srv.get("error")
        if error:
            # Truncate long errors
            if len(error) > 120:
                error = error[:117] + "..."
            line += f"\n    \u26a0\ufe0f {escape_rich(error)}"

        # Show tool count when connected
        tools = srv.get("tools", [])
        if tools:
            line += f"\n    \U0001f527 {len(tools)} tool{'s' if len(tools) != 1 else ''}"

        lines.append(line)

    text = "\n".join(lines)
    await reply_rich(message, text)


async def _mcp_reconnect(message: Any, session: AgentSession, server_name: str) -> None:
    """Reconnect a failed or disconnected MCP server.

    Kill the proxy's stdio subprocess first (sandboxed contexts) so the
    agent's reconnect respawns it fresh instead of re-attaching to a wedged
    process.
    """
    try:
        if session.mcp_proxy is not None:
            await session.mcp_proxy.restart_stdio_server(
                session.context_name, server_name
            )
        await session.client.reconnect_mcp_server(server_name)
    except Exception:
        logger.exception("Failed to reconnect MCP server %s", server_name)
        await reply_rich(message, f"Failed to reconnect `{server_name}`.")
        return

    escaped = server_name
    await reply_rich(message, f"Reconnecting `{escaped}`... Use /mcp to check status.")


async def _mcp_toggle(message: Any, session: AgentSession, server_name: str, *, enabled: bool) -> None:
    """Enable or disable an MCP server."""
    action = "enable" if enabled else "disable"
    try:
        await session.client.toggle_mcp_server(server_name, enabled=enabled)
    except Exception:
        logger.exception("Failed to %s MCP server %s", action, server_name)
        await reply_rich(message, f"Failed to {escape_rich(action)} `{server_name}`.")
        return

    past = "enabled" if enabled else "disabled"
    escaped = server_name
    emoji = "\U0001f7e2" if enabled else "\u26aa"
    await reply_rich(message, f"{emoji} `{escaped}` {past}.")


# ── /schedule ──


async def schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schedule command: list and manage scheduled tasks.

    Usage:
        /schedule           -- list all scheduled tasks
        /schedule delete <n> -- delete a scheduled task by name
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    args = message.text.split() if message.text else []

    if len(args) >= 3 and args[1] == "delete":
        # Delete a task by name.
        task_name = " ".join(args[2:])
        from open_shrimp.db import (
            delete_event_topic,
            delete_scheduled_task,
            list_scheduled_tasks,
        )
        from open_shrimp.events.schedule import get_active_runner, topic_key

        # Find task ID for JobQueue and topic-mapping removal.
        tasks = await list_scheduled_tasks(db)
        task_id = None
        for t in tasks:
            if t.name == task_name:
                task_id = t.id
                break

        deleted = await delete_scheduled_task(db, task_name)
        if deleted:
            if task_id is not None:
                runner = get_active_runner()
                if runner is not None:
                    runner.unregister_task(task_id)
                # The topic itself stays in Telegram as a record.
                await delete_event_topic(db, topic_key(task_id))

            escaped = task_name
            await reply_rich(message, f"Deleted scheduled task `{escaped}`.")
        else:
            escaped = task_name
            await reply_rich(message, f"No scheduled task named `{escaped}` found.")
        return

    # List all tasks.
    from open_shrimp.db import list_scheduled_tasks

    tasks = await list_scheduled_tasks(db)
    if not tasks:
        await reply_rich(message, "No scheduled tasks. Ask Claude to create one!")
        return

    lines = [
        f"**Scheduled tasks ({len(tasks)})**",
        "",
        "| Name | When | Context | Prompt |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for t in tasks:
        when = {
            "interval": f"every {t.schedule_expr}",
            "cron": f"cron: {t.schedule_expr}",
            "once": f"at {t.schedule_expr}",
        }.get(t.schedule_type, t.schedule_expr)
        prompt = t.prompt[:50] + ("..." if len(t.prompt) > 50 else "")
        lines.append(
            f"| **{escape_rich_inline(t.name)}** "
            f"| {escape_rich_inline(when)} "
            f"| `{t.context_name}` "
            f"| {escape_rich_inline(prompt)} |"
        )

    await reply_rich(message, "\n".join(lines))


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

    from open_shrimp import host_monitor

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
            await reply_rich(message, f"No active task matching `{target}`.")
            return

        # Host monitors are host-side processes invisible to the CLI task
        # registry; stopping one goes through host_monitor, whose _finalize
        # unregisters it from _active_bg_tasks.
        if matched_task.task_type == host_monitor.TASK_TYPE:
            stopped = await host_monitor.stop_monitor(matched_task.task_id)
            if stopped:
                await reply_rich(message, f"Stopped host monitor "
                    f"`{matched_task.task_id}`.")
            else:
                await reply_rich(message, "Failed to stop host monitor (already gone).")
            return

        from open_shrimp.client_manager import stop_background_task

        success = await stop_background_task(scope, matched_task.task_id)
        if success:
            # Remove from tracking immediately — the TaskNotificationMessage
            # may arrive later when the stream is next consumed, but we
            # don't want the task to linger in /tasks output.
            scope_tasks.pop(matched_task.task_id, None)
            if not scope_tasks:
                _active_bg_tasks.pop(scope, None)
            tid_short = matched_task.task_id[:12]
            await reply_rich(message, f"Stopped task `{tid_short}`.")
        else:
            await reply_rich(message, "Failed to stop task (no active session).")
        return

    # ── /tasks (list) ──
    # Host monitors register as transient tasks (task_type "host_monitor"),
    # so they render through the normal _active_bg_tasks path below.
    scope_tasks = _active_bg_tasks.get(scope, {})
    if not scope_tasks:
        await reply_rich(message, "No active background tasks.")
        return

    now = time.monotonic()
    lines = [
        f"**Active background tasks ({len(scope_tasks)})**",
        "",
        "| Id | Type | Description | Last tool | Elapsed |",
        "| :--- | :--- | :--- | :--- | ---: |",
    ]
    for task in scope_tasks.values():
        elapsed = int(now - task.started_at)
        minutes, seconds = divmod(elapsed, 60)
        duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
        lines.append(
            f"| `{task.task_id[:12]}` "
            f"| {escape_rich_inline(task.task_type or 'unknown')} "
            f"| {escape_rich_inline(task.description or 'No description')} "
            f"| {escape_rich_inline(task.last_tool_name or '—')} "
            f"| {duration} |"
        )

    lines.append("")
    lines.append("Use `/tasks stop <id>` to stop a task.")
    await reply_rich(message, "\n".join(lines))


# ── /usage ──


async def usage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage command: show operator quota/usage stats.

    Queries every configured backend that declares the ``"usage"``
    capability and renders a union of their reports.  A single backend
    with data produces today's flat output; multiple backends produce
    one section per backend.
    """
    from open_shrimp.backend.usage import UsageReport
    from open_shrimp.handlers.usage_render import render_usage_reports

    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    backends = context.bot_data.get("backends") or []
    capable = [b for b in backends if "usage" in b.command_capabilities()]
    if not capable:
        # Mirrors the legacy single-backend "not available on <name>" message,
        # generalised over the configured set (empty list → "this install").
        if backends:
            names = ", ".join(f"`{b.name}`" for b in backends)
        else:
            names = "this install"
        await reply_rich(message, f"/usage is not available on {names}.")
        return

    reports: list[tuple[str, UsageReport]] = []
    for backend in capable:
        report = await backend.usage()
        if report is not None and (report.tiers or report.extra):
            reports.append((backend.name, report))

    if not reports:
        await reply_rich(message, "Usage data unavailable. OAuth credentials not found or endpoint unreachable.")
        return

    text = render_usage_reports(reports)
    await reply_rich(message, text)


# ── /restart ──


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command: restart the bot process."""
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await reply_rich(message, "This command can only be used in private chats.")
        return

    import os

    from open_shrimp.main import request_restart, request_shutdown

    await reply_rich(message, "Restarting...")

    # Pass the chat scope via env vars so the new process can send a
    # confirmation message after startup.
    os.environ["OPENSHRIMP_RESTART_CHAT_ID"] = str(message.chat_id)
    thread_id = message.message_thread_id
    if thread_id is not None:
        os.environ["OPENSHRIMP_RESTART_THREAD_ID"] = str(thread_id)
    else:
        os.environ.pop("OPENSHRIMP_RESTART_THREAD_ID", None)

    request_restart()
    # Trigger shutdown in-process rather than via os.kill(SIGTERM):
    # on Windows that would be an unconditional TerminateProcess.
    request_shutdown()


# ── /config ──


async def config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config -- open the config Mini App."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    message = update.message

    if not _is_authorized(update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await reply_rich(message, "This command can only be used in private chats.")
        return

    await reply_mini_app(
        message,
        text="OpenShrimp configuration",
        buttons=[("\u2699\ufe0f Edit Configuration", "/config/")],
        config=config,
        user_id=update.effective_user.id,
        is_private_chat=_is_private_chat(update),
        opens="the settings page",
        still_works=(
            "Chatting with me here still works, and every setting is also "
            "kept in a file called config.yaml on the machine I run on, so "
            "whoever set me up can change things there in the meantime."
        ),
    )
