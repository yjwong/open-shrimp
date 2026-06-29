"""Cross-context query tool (``ask_context``).

Lets an agent running in one context ask a focused question of another
context and get a synchronous answer back, without the user manually
switching contexts.  The answering context runs a *fresh*, read-only
session against its own project tree (its ``directory`` + ``CLAUDE.md`` +
project MCP servers), so it can speak authoritatively about its own domain.

The sub-query is a bare backend client (like ``scheduler._run_scheduled_prompt``):
it loads no OpenShrimp MCP tools, so it can never recurse back into
``ask_context`` (the A->B->A guard is structural, not a flag).  Its base
capabilities are read-only; the target context's own ``allowed_tools`` are
inherited so anything the user already trusts there runs silently, and
everything else routes a normal Approve/Deny prompt into the *originating*
chat via the existing approval machinery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import Bot, InlineKeyboardMarkup

from open_shrimp.web_app_button import make_web_app_button

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from open_shrimp.backend.protocol import (
        Backend,
        BackendClient,
        BackendOptions,
        CanUseTool,
    )
    from open_shrimp.config import Config, ContextConfig
    from open_shrimp.tools import OpenShrimpTool

logger = logging.getLogger(__name__)

# Base read-only tool set, always granted to the answering sub-query.  Bash
# is added only for sandboxed targets (same rule as
# ``scheduler._run_scheduled_prompt``).
_BASE_READ_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]

# Bound concurrent cross-context queries so a fan-out of parallel
# ``ask_context`` calls can't exhaust resources.
_MAX_CONCURRENT = 3
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

# Per-call wall-clock budget.
_DEFAULT_TIMEOUT_SECONDS = 600.0


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """Build a standard MCP tool result (mirrors ``tools._text_result``)."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


def _queryable_contexts(
    config: "Config", current: str,
) -> dict[str, "ContextConfig"]:
    """Return the contexts an ``ask_context`` call may target.

    Every context except the current one.  Defensive against partial
    config stand-ins (tests pass ``SimpleNamespace`` without ``contexts``).
    """
    contexts = getattr(config, "contexts", None)
    if not isinstance(contexts, dict):
        return {}
    return {
        name: ctx
        for name, ctx in contexts.items()
        if name != current
    }


def _build_description(queryable: dict[str, "ContextConfig"]) -> str:
    """Enumerate the queryable contexts and their descriptions for the agent."""
    lines = [
        "Ask a focused question of ANOTHER context and get a synchronous "
        "answer back. The answering context runs a fresh, read-only session "
        "against its own project tree (its directory, CLAUDE.md, and project "
        "MCP servers), so it can answer authoritatively about its own domain "
        "— without you switching contexts. It has no memory of this "
        "conversation, so make the question self-contained. Every call "
        "requires the user's approval. Available contexts:",
    ]
    for name, ctx in sorted(queryable.items()):
        desc = (getattr(ctx, "description", "") or "").strip()
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines)


_ASK_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": (
                "Name of the context to ask. Must be one of the available "
                "contexts listed in this tool's description."
            ),
        },
        "question": {
            "type": "string",
            "description": (
                "A focused, self-contained question. The answering context "
                "has no memory of this conversation, so include all needed "
                "specifics (table names, file paths, run dates, etc.)."
            ),
        },
    },
    "required": ["context", "question"],
}


# ---------------------------------------------------------------------------
# Progress sink: tee the sub-query transcript to a file the Terminal Mini App
# can discover and tail, and register a transient task so the viewer streams
# live (and drains + closes once we unregister it on completion).  The path
# convention and the task registry each have a single owner elsewhere
# (``terminal.log_source`` and ``handlers.state``); this only composes them.
# ---------------------------------------------------------------------------

_TASK_PROJECT = "ask_context"
_TASK_TYPE = "ask_context"


class _ProgressSink:
    """A best-effort transcript file + active-task registration."""

    def __init__(self, task_id: str, chat_id: int, thread_id: int | None,
                 target: str) -> None:
        self.task_id = task_id
        self._fh = None
        self._scope = None

        try:
            from open_shrimp.terminal.log_source import (
                transient_task_output_path,
            )

            self.path = transient_task_output_path(_TASK_PROJECT, task_id)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            logger.debug("Could not open ask_context transcript sink",
                         exc_info=True)

        try:
            from open_shrimp.db import ChatScope
            from open_shrimp.handlers.state import register_transient_task

            self._scope = ChatScope(chat_id=chat_id, thread_id=thread_id)
            register_transient_task(
                self._scope,
                task_id,
                description=f"ask_context → {target}",
                task_type=_TASK_TYPE,
            )
        except Exception:
            logger.debug("Could not register ask_context task", exc_info=True)

    def write(self, text: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(text)
            self._fh.flush()
        except OSError:
            logger.debug("ask_context transcript write failed", exc_info=True)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self._scope is not None:
            try:
                from open_shrimp.handlers.state import (
                    unregister_transient_task,
                )

                unregister_transient_task(self._scope, self.task_id)
            except Exception:
                logger.debug("Could not unregister ask_context task",
                             exc_info=True)
            self._scope = None


def _summary_line(text: str, limit: int = 160) -> str:
    """First non-empty line of *text*, truncated for a status summary."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def build_ask_context_tool(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    config: "Config",
    context_name: str,
    user_id: int = 0,
    is_private_chat: bool = True,
    terminal_base_url: str | None = None,
    sandbox_managers: dict[str, Any] | None = None,
    mcp_proxy: Any | None = None,
) -> "OpenShrimpTool | None":
    """Build the ``ask_context`` tool descriptor, or ``None`` if no targets.

    Returns ``None`` when there is no *other* context to query (the tool
    would be useless) or the config has no contexts map.
    """
    from open_shrimp.tools import OpenShrimpTool

    queryable = _queryable_contexts(config, context_name)
    if not queryable:
        return None

    async def ask_context(args: dict[str, Any]) -> dict[str, Any]:
        target = (args.get("context") or "").strip()
        question = (args.get("question") or "").strip()

        if not question:
            return _text_result("Error: question is required.", is_error=True)

        # Re-resolve the queryable set at call time (config may have changed).
        valid = _queryable_contexts(config, context_name)
        ctx = valid.get(target)
        if ctx is None:
            names = ", ".join(sorted(valid)) or "(none)"
            if target == context_name:
                return _text_result(
                    "Error: cannot ask the current context "
                    f"({context_name!r}).",
                    is_error=True,
                )
            return _text_result(
                f"Error: no queryable context named {target!r}. "
                f"Available: {names}",
                is_error=True,
            )

        async with _semaphore:
            return await _run_query(
                bot=bot,
                chat_id=chat_id,
                thread_id=thread_id,
                config=config,
                target=target,
                ctx=ctx,
                question=question,
                user_id=user_id,
                is_private_chat=is_private_chat,
                terminal_base_url=terminal_base_url,
                sandbox_managers=sandbox_managers,
                mcp_proxy=mcp_proxy,
            )

    return OpenShrimpTool(
        name="ask_context",
        description=_build_description(queryable),
        input_schema=_ASK_CONTEXT_SCHEMA,
        # Not read_only: it spins a sub-execution and must require the
        # standard per-call approval rather than being silently auto-read.
        read_only=False,
        handler=ask_context,
    )


@dataclass
class _SubQueryResult:
    """Outcome of one answering sub-query run.

    ``outcome`` is one of ``"ok"``, ``"timeout"``, ``"error"``.  ``collected``
    is the answering agent's final text (possibly partial on timeout).
    """

    outcome: str
    collected: str
    error_detail: str = ""
    elapsed: float = 0.0
    tool_count: int = 0


@dataclass
class _SandboxLaunch:
    """Sandbox launch details needed by a transient ask_context client."""

    cli_path: str | None = None
    endpoint: Any = None
    cleanup_paths: list[str] = field(default_factory=list)
    host_address: str | None = None


async def _launch_target_sandbox(
    *,
    backend: "Backend",
    ctx: "ContextConfig",
    target: str,
    sandbox_managers: dict[str, Any] | None,
) -> _SandboxLaunch:
    """Start the target context's sandbox and return backend launch options.

    This intentionally fails closed: if a target context is configured as
    sandboxed but no matching manager is available, the sub-query must not fall
    back to a host-local client while permissions think it is isolated.
    """
    from open_shrimp.client_manager import _context_locks

    backend_name = ctx.sandbox.backend if ctx.sandbox is not None else "docker"
    manager = (sandbox_managers or {}).get(backend_name)
    if manager is None:
        raise RuntimeError(
            f"Context {target!r} is sandboxed with backend {backend_name!r}, "
            "but no sandbox manager is available. Refusing to run ask_context "
            "outside the sandbox."
        )

    lock = _context_locks.setdefault(target, asyncio.Lock())
    async with lock:
        runtime = backend.make_runtime(
            manager.agent_home_dir(target),
            context_name=target,
            model=ctx.model,
        )
        sandbox = manager.create_sandbox(target, ctx, runtime=runtime)

        def _start() -> Any:
            sandbox.ensure_environment()
            sandbox.ensure_running()
            sandbox.provision_workspace()
            return sandbox.start_agent(runtime)

        handle = await asyncio.to_thread(_start)
        return _SandboxLaunch(
            cli_path=handle.cli_path,
            endpoint=handle.endpoint,
            cleanup_paths=list(handle.cleanup_paths),
            host_address=sandbox.host_address,
        )


def _proxied_mcp_servers(
    *,
    backend: "Backend",
    ctx: "ContextConfig",
    target: str,
    mcp_proxy: Any,
    host_ip: str,
) -> dict[str, Any]:
    """Register the target context's MCP servers with the proxy.

    Mirrors the sandboxed-context branch in ``client_manager`` (lines that
    inject proxied stdio/HTTP MCP servers): a sandboxed sub-query's CLI runs
    under an isolated home that doesn't carry the user's ``~/.claude.json``
    declarations, so without this the answering context would see no MCP
    servers at all. Returns the sandbox-reachable HTTP endpoint map keyed by
    server name.

    Registration is keyed by the *real* context name and is intentionally not
    torn down afterward: ``register_context`` merges idempotently, so a
    concurrent live session for the same context shares the registration, and
    unregistering here would kill that session's MCP servers.
    """
    mcp_source = backend.mcp_config_source()
    stdio_servers = mcp_source.stdio_servers(ctx)
    http_servers = mcp_source.http_servers(ctx)
    if not stdio_servers and not http_servers:
        return {}

    token = mcp_proxy.register_context(
        target,
        servers=stdio_servers or None,
        http_servers=http_servers or None,
    )
    servers: dict[str, Any] = {}
    for name in stdio_servers:
        servers[name] = {
            "type": "http",
            "url": mcp_proxy.get_proxy_url(target, name, host_ip),
            "headers": {"Authorization": f"Bearer {token}"},
        }
    for name, http_cfg in http_servers.items():
        servers[name] = {
            "type": http_cfg.transport,
            "url": mcp_proxy.get_http_proxy_url(target, name, host_ip),
            "headers": {"Authorization": f"Bearer {token}"},
        }
    logger.info(
        "ask_context: injected %d stdio + %d HTTP proxied MCP server(s) "
        "for sandboxed context '%s': stdio=[%s] http=[%s]",
        len(stdio_servers),
        len(http_servers),
        target,
        ", ".join(stdio_servers),
        ", ".join(http_servers),
    )
    return servers


async def _request_outer_approval(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    target: str,
    question: str,
) -> bool:
    """Render the tailored Approve/Deny card for the outer ask_context call.

    Reuses the standard ``approve:``/``deny:`` callback prefixes (with a
    fresh ``tool_use_id``) so the existing ``handle_approval_callback``
    resolves them and edits the card to ``✅ Approved.`` / ``❌ Denied.``.
    """
    from telegram import InlineKeyboardButton

    from open_shrimp.handlers.state import (
        _approval_futures,
        _approval_metadata,
        _approval_tool_names,
    )
    from open_shrimp.handlers.utils import _escape_mdv2

    tool_use_id = f"askctx{os.urandom(6).hex()}"
    approve_data = f"approve:{tool_use_id}"
    deny_data = f"deny:{tool_use_id}"

    text = (
        f"🔎 *Ask {_escape_mdv2(target)}?*\n"
        f"> {_escape_mdv2(_summary_line(question, limit=300))}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=approve_data),
        InlineKeyboardButton("Deny", callback_data=deny_data),
    ]])

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
        **thread_kwargs,
    )

    _approval_tool_names[tool_use_id] = "ask_context"
    _approval_metadata[tool_use_id] = {
        "tool_name": "ask_context",
        "tool_input": {"context": target, "question": question},
        "chat_id": chat_id,
        "message_id": sent.message_id,
    }

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    try:
        return await future
    finally:
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _approval_tool_names.pop(tool_use_id, None)
        _approval_metadata.pop(tool_use_id, None)


def _make_parent_routed_approval(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    ctx: "ContextConfig",
    target: str,
    config: "Config",
    user_id: int,
    is_private_chat: bool,
    terminal_base_url: str | None,
    policy: Any,
) -> "Callable[..., Awaitable[bool]]":
    """Build the layer-3 approval callback.

    Anything the sub-query attempts that isn't pre-trusted routes a normal
    Approve/Deny prompt into the *parent* chat, prefixed to show it
    originates from the cross-context sub-query.
    """
    from open_shrimp.handlers.approval import _send_approval_keyboard
    from open_shrimp.handlers.utils import _escape_mdv2

    provenance = f"🔎 *{_escape_mdv2(target)}* sub\\-query wants to:"

    async def _request_approval(
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        suggested_session_dir: str | None = None,
    ) -> bool:
        return await _send_approval_keyboard(
            bot,
            chat_id,
            tool_name,
            tool_input,
            tool_use_id,
            cwd=ctx.directory,
            thread_id=thread_id,
            base_url=terminal_base_url,
            user_id=user_id,
            is_private_chat=is_private_chat,
            bot_token=config.telegram.token,
            suggested_session_dir=suggested_session_dir,
            policy=policy,
            provenance=provenance,
        )

    return _request_approval


def _build_sub_query_options(
    *,
    backend: "Backend",
    ctx: "ContextConfig",
    target: str,
    sandboxed: bool,
    chat_id: int,
    approval_cb: "CanUseTool",
    launch: _SandboxLaunch | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> "BackendOptions":
    """Assemble the fresh, read-only sub-query's runtime configuration.

    Capability layers 1 (base read) and 2 (inherit the target's trusted
    tools) plus the permission callback wiring live here; layer 3 is the
    supplied ``approval_cb``.
    """
    from open_shrimp.backend import BackendOptions

    allowed = list(_BASE_READ_TOOLS) + list(ctx.allowed_tools or [])
    if sandboxed:
        allowed.append("Bash")

    can_use_tool = backend.make_can_use_tool(
        request_approval=approval_cb,
        cwd=ctx.directory,
        additional_directories=ctx.additional_directories or None,
        chat_id=chat_id,
        is_containerized=sandboxed,
        policy=backend.policy,
    )

    extra: dict[str, Any] = {}
    if launch is not None and launch.endpoint is not None:
        extra["endpoint"] = launch.endpoint

    return BackendOptions(
        cwd=ctx.directory,
        model=ctx.model,
        effort=ctx.effort,
        allowed_tools=allowed,
        add_dirs=ctx.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        cli_path=launch.cli_path if launch is not None else None,
        max_buffer_size=10 * 1024 * 1024,
        can_use_tool=can_use_tool,
        mcp_servers=mcp_servers or None,
        extra=extra,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                f"Another agent is asking you a question about this project "
                f"({target}). You have no memory of their conversation. "
                f"Answer concisely and factually from this project's files."
            ),
        },
    )


async def _run_sub_query(
    *,
    client: "BackendClient",
    sink: _ProgressSink,
    question: str,
    cwd: str,
    policy: Any,
    timeout: float,
) -> _SubQueryResult:
    """Drive the sub-query client to completion, teeing the transcript.

    Owns only the execution lifecycle (connect → query → drain →
    disconnect) and the translation of streamed messages into transcript
    lines + collected answer text.  Never raises: every failure mode maps
    to a ``_SubQueryResult`` outcome.
    """
    from open_shrimp.backend.types import (
        AssistantMessage,
        TextBlock,
        ToolUseBlock,
    )
    from open_shrimp.stream import extract_tool_summary

    sink.write(f"> {question}\n\n")
    text_parts: list[str] = []
    tool_count = 0
    start = time.monotonic()

    async def _drain() -> None:
        nonlocal tool_count
        async for msg in client.receive_response():
            if not isinstance(msg, AssistantMessage):
                continue
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text.strip():
                        text_parts.append(block.text)
                        sink.write(block.text + "\n")
                elif isinstance(block, ToolUseBlock):
                    tool_count += 1
                    summary = extract_tool_summary(
                        block.name, block.input, cwd, policy=policy,
                    )
                    sink.write(
                        f"🔧 {block.name}: {summary}\n" if summary
                        else f"🔧 {block.name}\n"
                    )

    outcome = "ok"
    error_detail = ""
    try:
        await client.connect()
        await client.query(question)
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        outcome = "timeout"
        logger.warning("ask_context sub-query timed out")
        try:
            await client.interrupt()
        except Exception:
            logger.debug("interrupt after timeout failed", exc_info=True)
    except Exception as exc:
        outcome = "error"
        error_detail = str(exc)
        logger.exception("ask_context sub-query failed")
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("ask_context client disconnect failed", exc_info=True)
        sink.close()

    return _SubQueryResult(
        outcome=outcome,
        collected="\n\n".join(text_parts).strip(),
        error_detail=error_detail,
        elapsed=time.monotonic() - start,
        tool_count=tool_count,
    )


class _StatusMessage:
    """The Telegram status-message lifecycle for one ask_context call.

    Owns nothing but presentation: posts ``🔎 Asking …`` on start and edits
    it in place to a ``✅``/``⏱️``/``❌`` one-liner on finish.  Best-effort —
    a failed send/edit must not break the query.
    """

    def __init__(
        self, bot: Bot, chat_id: int, thread_id: int | None, target: str,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._target = target
        self._message_id: int | None = None

    async def start(
        self, keyboard: InlineKeyboardMarkup | None,
    ) -> None:
        from open_shrimp.handlers.utils import _escape_mdv2

        # The question is already shown on the approval card above this
        # message, so don't echo it again here.
        text = f"🔎 *Asking {_escape_mdv2(self._target)}…*"
        kwargs: dict[str, Any] = {}
        if self._thread_id is not None:
            kwargs["message_thread_id"] = self._thread_id
        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                disable_notification=True,
                **kwargs,
            )
            self._message_id = msg.message_id
        except Exception:
            logger.debug("Failed to send ask_context status message",
                         exc_info=True)

    async def finish(self, result: _SubQueryResult) -> None:
        if self._message_id is None:
            return
        from open_shrimp.handlers.utils import _escape_mdv2

        target = _escape_mdv2(self._target)
        if result.outcome == "ok":
            tools = (
                "1 tool" if result.tool_count == 1
                else f"{result.tool_count} tools"
            )
            text = (
                f"✅ *{target} answered* · {int(result.elapsed)}s · {tools}"
            )
        elif result.outcome == "timeout":
            text = (
                f"⏱️ *{target}* — query timed out after "
                f"{int(_DEFAULT_TIMEOUT_SECONDS)}s"
            )
        else:
            text = f"❌ *{target}* — query failed"
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception:
            logger.debug("Failed to edit ask_context status message",
                         exc_info=True)


def _view_output_keyboard(
    *,
    task_id: str,
    terminal_base_url: str | None,
    config: "Config",
    chat_id: int,
    user_id: int,
    is_private_chat: bool,
) -> InlineKeyboardMarkup | None:
    """Build the 📺 View output button for the Terminal Mini App, if enabled."""
    if not terminal_base_url:
        return None
    app_url = f"{terminal_base_url}/terminal/?type=task&id={task_id}"
    return InlineKeyboardMarkup([[
        make_web_app_button(
            "📺 View output",
            app_url,
            chat_id=chat_id,
            user_id=user_id,
            bot_token=config.telegram.token,
            is_private_chat=is_private_chat,
        ),
    ]])


def _format_tool_result(target: str, result: _SubQueryResult) -> dict[str, Any]:
    """Render the sub-query outcome as the MCP tool result for the agent."""
    if result.outcome == "timeout":
        return _text_result(
            f"Cross-context query to {target!r} timed out after "
            f"{int(_DEFAULT_TIMEOUT_SECONDS)}s. "
            f"Partial answer (if any):\n{result.collected}".rstrip(),
            is_error=True,
        )
    if result.outcome == "error":
        return _text_result(
            f"Cross-context query to {target!r} failed: {result.error_detail}",
            is_error=True,
        )
    collected = result.collected or "(the context produced no textual answer)"
    return _text_result(f"[{target} answered]\n{collected}")


async def _run_query(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    config: "Config",
    target: str,
    ctx: "ContextConfig",
    question: str,
    user_id: int,
    is_private_chat: bool,
    terminal_base_url: str | None,
    sandbox_managers: dict[str, Any] | None,
    mcp_proxy: Any | None,
) -> dict[str, Any]:
    """Orchestrate one cross-context query: approve, configure, run, report."""
    from open_shrimp.client_manager import resolve_backend
    from open_shrimp.config import is_sandboxed

    approved = await _request_outer_approval(
        bot=bot,
        chat_id=chat_id,
        thread_id=thread_id,
        target=target,
        question=question,
    )
    if not approved:
        return _text_result(
            f"Cross-context query to {target!r} was denied by the user.",
            is_error=True,
        )

    backend = resolve_backend(context=ctx)
    sandboxed = is_sandboxed(ctx)

    try:
        launch = await _launch_target_sandbox(
            backend=backend,
            ctx=ctx,
            target=target,
            sandbox_managers=sandbox_managers,
        ) if sandboxed else None
    except Exception as exc:
        logger.exception("ask_context sandbox launch failed")
        return _text_result(str(exc), is_error=True)

    # Sandboxed targets run under an isolated home, so their MCP servers must
    # be reached through the host proxy (same as the live-session path in
    # client_manager). Non-sandboxed targets load them from ~/.claude.json via
    # setting_sources, so no injection is needed.
    mcp_servers: dict[str, Any] | None = None
    if sandboxed and mcp_proxy is not None and launch is not None:
        try:
            mcp_servers = _proxied_mcp_servers(
                backend=backend,
                ctx=ctx,
                target=target,
                mcp_proxy=mcp_proxy,
                host_ip=launch.host_address or "127.0.0.1",
            )
        except Exception:
            logger.exception(
                "ask_context: failed to inject proxied MCP servers for %r",
                target,
            )

    approval_cb = _make_parent_routed_approval(
        bot=bot,
        chat_id=chat_id,
        thread_id=thread_id,
        ctx=ctx,
        target=target,
        config=config,
        user_id=user_id,
        is_private_chat=is_private_chat,
        terminal_base_url=terminal_base_url,
        policy=backend.policy,
    )
    options = _build_sub_query_options(
        backend=backend,
        ctx=ctx,
        target=target,
        sandboxed=sandboxed,
        chat_id=chat_id,
        approval_cb=approval_cb,
        launch=launch,
        mcp_servers=mcp_servers,
    )

    task_id = f"askctx{os.urandom(6).hex()}"
    sink = _ProgressSink(task_id, chat_id, thread_id, target)

    status = _StatusMessage(bot, chat_id, thread_id, target)
    await status.start(
        _view_output_keyboard(
            task_id=task_id,
            terminal_base_url=terminal_base_url,
            config=config,
            chat_id=chat_id,
            user_id=user_id,
            is_private_chat=is_private_chat,
        ),
    )

    try:
        result = await _run_sub_query(
            client=backend.make_client(options),
            sink=sink,
            question=question,
            cwd=ctx.directory,
            policy=backend.policy,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    finally:
        if launch is not None:
            for path in launch.cleanup_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    logger.debug(
                        "Failed to remove ask_context wrapper %s",
                        path,
                        exc_info=True,
                    )

    await status.finish(result)
    return _format_tool_result(target, result)
