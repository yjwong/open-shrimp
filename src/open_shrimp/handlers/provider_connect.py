"""Telegram-native provider connection flow backed by OpenCode HTTP APIs."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from open_shrimp.client_manager import close_sessions_for_context
from open_shrimp.config import Config, ContextConfig, is_sandboxed
from open_shrimp.db import ChatScope
from open_shrimp.handlers.utils import (
    _escape_mdv2,
    _get_context,
    _is_authorized,
    chat_scope_from_message,
)
from open_shrimp.opencode_client import OpenCodeClient, OpenCodeOptions, split_provider_model

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "pc:"
_PAGE_SIZE = 12
_COMMON_PROVIDERS = [
    "opencode",
    "openai",
    "github-copilot",
    "google",
    "anthropic",
    "openrouter",
    "vercel",
    "xai",
    "digitalocean",
    "azure",
    "cloudflare-ai-gateway",
]


@dataclass
class ConnectState:
    user_id: int
    scope: ChatScope
    context_name: str
    provider_id: str
    method_index: int
    method: dict[str, Any]
    prompts: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    prompt_index: int = 0
    waiting_for: str | None = None


_states: dict[ChatScope, ConnectState] = {}


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == chat.PRIVATE


async def connect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect with Telegram-native provider auth."""
    if not update.effective_user or not update.message:
        return
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        return
    if not _is_private(update):
        await update.message.reply_text(
            "Provider connection can only be used in private chats\.",
            parse_mode="MarkdownV2",
        )
        return

    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)
    context_name, ctx = await _get_context(scope, config, db)
    args = (update.message.text or "").split(maxsplit=1)
    target = args[1].strip() if len(args) == 2 else ""

    if target == "list":
        await _send_provider_list(update.message, ctx)
        return
    if target.startswith("disconnect "):
        provider_id = target.removeprefix("disconnect ").strip()
        await _disconnect_provider(update.message, context, context_name, ctx, provider_id)
        return
    if target:
        await _show_methods(update.message, context, context_name, ctx, target)
        return
    await _show_provider_picker(update.message, ctx, context_name, page=0)


async def handle_connect_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(CALLBACK_PREFIX):
        return False
    config: Config = context.bot_data["config"]
    if not _is_authorized(query.from_user and query.from_user.id, config):
        await query.answer("Unauthorized.")
        return True
    if not query.message:
        await query.answer()
        return True

    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(query.message)
    context_name, ctx = await _get_context(scope, config, db)
    data = query.data

    try:
        if data.startswith("pc:page:"):
            await query.answer()
            await _show_provider_picker(
                query.message,
                ctx,
                context_name,
                page=int(data[8:]),
                edit=True,
            )
            return True
        if data.startswith("pc:p:"):
            await query.answer()
            await _show_methods(query.message, context, context_name, ctx, data[5:], edit=True)
            return True
        if data.startswith("pc:m:"):
            _, _, provider_id, method_text = data.split(":", 3)
            await query.answer()
            await _select_method(
                query.message,
                context,
                scope,
                context_name,
                ctx,
                provider_id,
                int(method_text),
                query.from_user.id,
            )
            return True
        if data.startswith("pc:sel:"):
            await query.answer()
            await _handle_select_prompt(query.message, context, scope, int(data[7:]))
            return True
        if data == "pc:cancel":
            _states.pop(scope, None)
            await query.answer("Cancelled.")
            await query.message.edit_text("Provider connection cancelled\.", parse_mode="MarkdownV2")
            return True
    except Exception:
        logger.exception("Provider connect callback failed")
        await query.message.reply_text(
            "Provider connection failed\. Check logs for details\.",
            parse_mode="MarkdownV2",
        )
        return True

    await query.answer()
    return True


async def maybe_handle_connect_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return False
    scope = chat_scope_from_message(message)
    state = _states.get(scope)
    if state is None or state.user_id != user.id:
        return False
    if state.waiting_for is None:
        return False

    value = message.text.strip()
    if not value:
        await message.reply_text("Please send a non-empty value\.", parse_mode="MarkdownV2")
        return True
    if state.waiting_for == "api_key":
        with suppress(Exception):
            await message.delete()
        await _finish_api_key(message, context, scope, state, value)
        return True
    if state.waiting_for == "oauth_code":
        with suppress(Exception):
            await message.delete()
        await _finish_oauth_code(message, context, scope, state, value)
        return True

    state.inputs[state.waiting_for] = value
    state.waiting_for = None
    state.prompt_index += 1
    await _continue_prompts(message, context, scope, state)
    return True


async def _host_client(ctx: ContextConfig) -> OpenCodeClient:
    provider, model = split_provider_model(ctx.model)
    client = OpenCodeClient(OpenCodeOptions(cwd=ctx.directory, provider=provider, model=model))
    await client.connect_control()
    return client


async def _send_provider_list(message: Message, ctx: ContextConfig) -> None:
    async with _client_context(ctx) as client:
        data = await client.list_providers()
    providers = _ordered_providers(data)
    connected = set(data.get("connected") or [])
    lines = ["*Providers*"]
    for item in providers:
        pid = str(item.get("id") or "")
        if not pid:
            continue
        status = "connected" if pid in connected else "not connected"
        name = str(item.get("name") or pid)
        lines.append(f"`{_escape_mdv2(pid)}` — {_escape_mdv2(name)} \({_escape_mdv2(status)}\)")
    await message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def _show_provider_picker(
    message: Message,
    ctx: ContextConfig,
    context_name: str,
    *,
    page: int,
    edit: bool = False,
) -> None:
    async with _client_context(ctx) as client:
        data = await client.list_providers()
    providers = _ordered_providers(data)
    connected = set(data.get("connected") or [])
    pages = max(1, (len(providers) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = []
    for item in providers[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        pid = str(item.get("id") or "")
        if not pid:
            continue
        name = str(item.get("name") or pid)
        prefix = "🔐 " if pid in connected else ""
        rows.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"pc:p:{pid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Previous", callback_data=f"pc:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"pc:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Cancel", callback_data="pc:cancel")])
    text = (
        f"Connect providers for *{_escape_mdv2(context_name)}*\n"
        f"Choose a provider \(page {page + 1}/{pages}\)\."
    )
    if edit:
        await message.edit_text(text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows))


async def _show_methods(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    context_name: str,
    ctx: ContextConfig,
    provider_id: str,
    *,
    edit: bool = False,
) -> None:
    async with _client_context(ctx) as client:
        providers = await client.list_providers()
        methods = await client.list_provider_auth_methods()
    provider_ids = {str(item.get("id")) for item in providers.get("all", []) if item.get("id")}
    if provider_id not in provider_ids:
        await message.reply_text(
            f"Unknown provider `{_escape_mdv2(provider_id)}`\.",
            parse_mode="MarkdownV2",
        )
        return
    choices = _methods_for_provider(methods, provider_id)
    rows = [
        [InlineKeyboardButton(str(method.get("label") or method.get("type") or "Auth"), callback_data=f"pc:m:{provider_id}:{idx}")]
        for idx, method in enumerate(choices)
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="pc:cancel")])
    text = f"Connect `{_escape_mdv2(provider_id)}` for *{_escape_mdv2(context_name)}*\."
    if edit:
        await message.edit_text(text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows))


async def _select_method(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    context_name: str,
    ctx: ContextConfig,
    provider_id: str,
    method_index: int,
    user_id: int,
) -> None:
    async with _client_context(ctx) as client:
        methods = await client.list_provider_auth_methods()
    choices = _methods_for_provider(methods, provider_id)
    if method_index < 0 or method_index >= len(choices):
        await message.reply_text("Invalid auth method\.", parse_mode="MarkdownV2")
        return
    method = choices[method_index]
    state = ConnectState(
        user_id=user_id,
        scope=scope,
        context_name=context_name,
        provider_id=provider_id,
        method_index=method_index,
        method=method,
        prompts=list(method.get("prompts") or []),
    )
    _states[scope] = state
    await _continue_prompts(message, context, scope, state)


async def _continue_prompts(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    state: ConnectState,
) -> None:
    while state.prompt_index < len(state.prompts):
        prompt = state.prompts[state.prompt_index]
        if not _prompt_applies(prompt, state.inputs):
            state.prompt_index += 1
            continue
        if prompt.get("type") == "select":
            options = [opt for opt in prompt.get("options") or [] if isinstance(opt, dict)]
            rows = [[InlineKeyboardButton(str(opt.get("label") or opt.get("value")), callback_data=f"pc:sel:{idx}")] for idx, opt in enumerate(options)]
            rows.append([InlineKeyboardButton("Cancel", callback_data="pc:cancel")])
            await message.reply_text(
                _escape_mdv2(str(prompt.get("message") or "Choose a value")),
                parse_mode="MarkdownV2",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        key = str(prompt.get("key") or "")
        if not key:
            state.prompt_index += 1
            continue
        state.waiting_for = key
        await message.reply_text(
            _escape_mdv2(str(prompt.get("message") or "Send the value")),
            parse_mode="MarkdownV2",
        )
        return

    if state.method.get("type") == "api":
        state.waiting_for = "api_key"
        await message.reply_text(
            f"Send the API key for `{_escape_mdv2(state.provider_id)}`\. I will delete the message if Telegram allows it\.",
            parse_mode="MarkdownV2",
        )
        return
    await _start_oauth(message, context, scope, state)


async def _handle_select_prompt(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    option_index: int,
) -> None:
    state = _states.get(scope)
    if state is None or state.prompt_index >= len(state.prompts):
        await message.reply_text("No provider connection is waiting for a selection\.", parse_mode="MarkdownV2")
        return
    prompt = state.prompts[state.prompt_index]
    options = [opt for opt in prompt.get("options") or [] if isinstance(opt, dict)]
    if option_index < 0 or option_index >= len(options):
        await message.reply_text("Invalid selection\.", parse_mode="MarkdownV2")
        return
    state.inputs[str(prompt.get("key"))] = str(options[option_index].get("value") or "")
    state.prompt_index += 1
    await _continue_prompts(message, context, scope, state)


async def _finish_api_key(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    state: ConnectState,
    api_key: str,
) -> None:
    _states.pop(scope, None)
    try:
        config: Config = context.bot_data["config"]
        ctx = config.contexts[state.context_name]
        async with _client_context(ctx) as client:
            ok = await client.set_provider_api_key(state.provider_id, api_key, state.inputs or None)
        await _after_auth_change(context, state.context_name, ctx, state.provider_id)
    except Exception:
        logger.exception("Failed to set provider API key")
        await message.reply_text("Failed to save provider credentials\.", parse_mode="MarkdownV2")
        return
    text = "Provider connected\." if ok else "OpenCode did not confirm provider connection\."
    await message.reply_text(text, parse_mode="MarkdownV2")


async def _start_oauth(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    state: ConnectState,
) -> None:
    config: Config = context.bot_data["config"]
    ctx = config.contexts[state.context_name]
    async with _client_context(ctx) as client:
        auth = await client.authorize_provider(state.provider_id, state.method_index, state.inputs or None)
    if not auth:
        _states.pop(scope, None)
        await message.reply_text("OAuth did not return an authorization URL\.", parse_mode="MarkdownV2")
        return
    url = str(auth.get("url") or "")
    instructions = str(auth.get("instructions") or "Open the URL and authorize access.")
    if auth.get("method") == "code":
        state.waiting_for = "oauth_code"
        await message.reply_text(
            f"{_escape_mdv2(instructions)}\n\n{_escape_mdv2(url)}\n\nPaste the authorization code here\.",
            parse_mode="MarkdownV2",
        )
        return
    await message.reply_text(
        f"{_escape_mdv2(instructions)}\n\n{_escape_mdv2(url)}\n\nWaiting for authorization to complete\.\.\.",
        parse_mode="MarkdownV2",
    )
    asyncio.create_task(_complete_auto_oauth(message, context, scope, state))


async def _complete_auto_oauth(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    state: ConnectState,
) -> None:
    try:
        config: Config = context.bot_data["config"]
        ctx = config.contexts[state.context_name]
        async with _client_context(ctx) as client:
            ok = await client.complete_provider_oauth(state.provider_id, state.method_index)
        await _after_auth_change(context, state.context_name, ctx, state.provider_id)
        text = "Provider connected\." if ok else "OpenCode did not confirm OAuth completion\."
        await message.reply_text(text, parse_mode="MarkdownV2")
    except Exception:
        logger.exception("Failed to complete provider OAuth")
        await message.reply_text("OAuth completion failed\.", parse_mode="MarkdownV2")
    finally:
        _states.pop(scope, None)


async def _finish_oauth_code(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    scope: ChatScope,
    state: ConnectState,
    code: str,
) -> None:
    _states.pop(scope, None)
    try:
        config: Config = context.bot_data["config"]
        ctx = config.contexts[state.context_name]
        async with _client_context(ctx) as client:
            ok = await client.complete_provider_oauth(
                state.provider_id,
                state.method_index,
                code=code,
            )
        await _after_auth_change(context, state.context_name, ctx, state.provider_id)
    except Exception:
        logger.exception("Failed to complete provider OAuth code flow")
        await message.reply_text("OAuth completion failed\.", parse_mode="MarkdownV2")
        return
    text = "Provider connected\." if ok else "OpenCode did not confirm OAuth completion\."
    await message.reply_text(text, parse_mode="MarkdownV2")


async def _disconnect_provider(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    context_name: str,
    ctx: ContextConfig,
    provider_id: str,
) -> None:
    if not provider_id:
        await message.reply_text("Usage: `/connect disconnect <provider>`", parse_mode="MarkdownV2")
        return
    try:
        async with _client_context(ctx) as client:
            ok = await client.remove_provider_auth(provider_id)
        await _after_auth_change(context, context_name, ctx, provider_id)
    except Exception:
        logger.exception("Failed to disconnect provider")
        await message.reply_text("Failed to disconnect provider\.", parse_mode="MarkdownV2")
        return
    text = f"Disconnected `{_escape_mdv2(provider_id)}`\." if ok else "OpenCode did not confirm disconnect\."
    await message.reply_text(text, parse_mode="MarkdownV2")


async def _after_auth_change(
    context: ContextTypes.DEFAULT_TYPE,
    context_name: str,
    ctx: ContextConfig,
    provider_id: str,
) -> None:
    if not is_sandboxed(ctx):
        return
    await close_sessions_for_context(context_name)
    managers = context.bot_data.get("sandbox_managers") or {}
    backend = ctx.sandbox.backend if ctx.sandbox else ""
    manager = managers.get(backend)
    if manager is not None:
        await asyncio.to_thread(manager.invalidate_sandbox, context_name)
    logger.info(
        "Invalidated sandbox context %s after provider auth change for %s",
        context_name,
        provider_id,
    )


def _ordered_providers(data: dict[str, Any]) -> list[dict[str, Any]]:
    providers = [item for item in data.get("all") or [] if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in providers if item.get("id")}
    connected = set(data.get("connected") or [])

    def group(ids: set[str]) -> list[dict[str, Any]]:
        common = [by_id[pid] for pid in _COMMON_PROVIDERS if pid in ids and pid in by_id]
        common_ids = {str(item.get("id")) for item in common}
        rest = [
            item for pid, item in sorted(by_id.items(), key=lambda pair: pair[0])
            if pid in ids and pid not in common_ids
        ]
        return common + rest

    ordered = group(connected)
    ordered.extend(group(set(by_id) - connected))
    return ordered


def _methods_for_provider(
    methods: dict[str, list[dict[str, Any]]],
    provider_id: str,
) -> list[dict[str, Any]]:
    provider_methods = list(methods.get(provider_id) or methods.get(provider_id.rstrip("/")) or [])
    if not any(method.get("type") == "api" for method in provider_methods):
        provider_methods.append({"type": "api", "label": "API key"})
    return provider_methods


def _prompt_applies(prompt: dict[str, Any], inputs: dict[str, str]) -> bool:
    when = prompt.get("when")
    if not isinstance(when, dict):
        return True
    key = str(when.get("key") or "")
    op = when.get("op")
    value = str(when.get("value") or "")
    current = inputs.get(key)
    if op == "eq":
        return current == value
    if op == "neq":
        return current != value
    return True


class _client_context:
    def __init__(self, ctx: ContextConfig) -> None:
        self.ctx = ctx
        self.client: OpenCodeClient | None = None

    async def __aenter__(self) -> OpenCodeClient:
        self.client = await _host_client(self.ctx)
        return self.client

    async def __aexit__(self, *exc: Any) -> None:
        if self.client is not None:
            await self.client.disconnect()
