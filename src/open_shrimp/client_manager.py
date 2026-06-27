"""Persistent backend client manager for OpenShrimp.

Manages long-lived backend clients keyed by ChatScope, so the agent CLI
subprocess stays alive across multiple messages in the same conversation.
This avoids the "Continue from where you left off." injection that the CLI
performs when it detects an interrupted turn on session resume.

The concrete client is produced by the configured ``Backend``: the manager
constructs ``BackendOptions``, calls ``backend.make_client(opts)``, and drives
the resulting ``BackendClient`` through the protocol method set.  The
SDK-specific details (options translation, the resume-fallback retry on
connect, the subprocess liveness poke, SDK-message translation) live inside the
``claude_sdk`` adapter, not here.

Only the first message in a session uses ``--resume`` to restore history;
subsequent messages simply call ``client.query()`` on the already-connected
client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_shrimp.backend import get_backend, get_backend_by_name
from open_shrimp.backend.protocol import Backend, BackendClient, BackendOptions
from open_shrimp.backend.types import ResultMessage, SystemMessage

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.web_app_button import make_web_app_button

from open_shrimp.agent import AgentEvent
from open_shrimp.config import ContextConfig, is_sandboxed
from open_shrimp.db import ChatScope, delete_session
from open_shrimp.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    HostBashApprovalCallback,
    QuestionCallback,
)
from open_shrimp.sandbox import Sandbox, SandboxManager
from open_shrimp.sandbox.agent_runtime import (
    AgentHandle,
    AgentRuntime,
)
from open_shrimp.sandbox.agent_runtime_watcher import (
    register_sandbox as register_cred_sandbox,
    unregister_sandbox as unregister_cred_sandbox,
)
from open_shrimp.tools import OpenShrimpTool, create_openshrimp_tools

logger = logging.getLogger(__name__)

@dataclass
class CallbackContext:
    """Mutable holder for per-message callback state.

    The ``canUseTool`` closure is bound at client creation time and cannot
    be changed.  This indirection lets per-message state (like
    ``draft_state``) be swapped in before each ``query()`` call while the
    same closure keeps referencing *this* object.
    """

    request_approval: ApprovalCallback | None = None
    handle_user_questions: QuestionCallback | None = None
    is_edit_auto_approved: Callable[[], bool] | None = None
    notify_auto_approved_edit: EditNotifyCallback | None = None
    is_tool_auto_approved: Callable[[str, dict[str, Any]], bool] | None = None
    get_session_approved_dirs: Callable[[], list[str]] | None = None
    request_host_bash_approval: HostBashApprovalCallback | None = None


@dataclass
class AgentSession:
    """A long-lived backend client associated with a chat scope."""

    client: BackendClient
    session_id: str | None = None
    context_name: str = ""
    callback_context: CallbackContext = field(default_factory=CallbackContext)
    sandbox: Sandbox | None = None
    runtime: AgentRuntime | None = None
    mcp_proxy: Any | None = None
    wrapper_cleanup_paths: list[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)
    # Pinned at creation so a config edit mid-turn doesn't shift the policy.
    backend: Backend | None = None


_active_sessions: dict[ChatScope, AgentSession] = {}

# The configured top-level backend; ``None`` until ``run_bot`` calls
# ``set_default_backend``.  Pre-startup callers (tests, scope-less paths)
# fall back to ``get_backend({})`` which honours the registry default.
_default_backend: Backend | None = None


def set_default_backend(backend: Backend) -> None:
    """Install the process-wide default backend (called once at startup)."""
    global _default_backend
    _default_backend = backend


def resolve_backend(
    backend: "Backend | None" = None,
    *,
    scope: "ChatScope | None" = None,
    context: "ContextConfig | None" = None,
) -> Backend:
    """Resolve which backend should serve a given call site.

    Resolution order:
    * ``context``: the backend named by ``context.backend`` — declarative
      per-context override wins over any caller-supplied default. A session
      pinned to a different backend will be rebuilt downstream.
    * ``backend``: explicit caller-supplied backend (e.g., a session-pinned
      one threaded through a reconnect path).
    * ``scope``: the live session's pinned backend.
    * Otherwise: the top-level default.
    """
    if context is not None and context.backend is not None:
        return get_backend_by_name(context.backend)
    if backend is not None:
        return backend
    if scope is not None:
        existing = _active_sessions.get(scope)
        if existing is not None and existing.backend is not None:
            return existing.backend
    return _default_backend or get_backend({})

# Idle session timeout: sessions with no activity for this long are closed.
_IDLE_TIMEOUT: float = 30 * 60  # 30 minutes
_idle_sweep_task: asyncio.Task[None] | None = None

# Per-context lock: serialises sandbox creation so two scopes sharing
# the same libvirt context don't race on VM boot / virtiofsd / ports.
_context_locks: dict[str, asyncio.Lock] = {}

# Scopes that have already been warned about degraded (proxy-less) tool
# availability, so the warning fires at most once per scope per process.
_tools_degraded_warned: set[ChatScope] = set()


async def _warn_tools_degraded_once(bot: Bot, scope: ChatScope) -> None:
    """Tell the user once that OpenShrimp tools are unavailable this run.

    Best-effort: never let a failed Telegram send break the turn.
    """
    if scope in _tools_degraded_warned:
        return
    _tools_degraded_warned.add(scope)
    try:
        kwargs: dict[str, Any] = {}
        if scope.thread_id is not None:
            kwargs["message_thread_id"] = scope.thread_id
        await bot.send_message(
            chat_id=scope.chat_id,
            text=(
                "⚠️ File, topic, schedule, and host tools are unavailable "
                "this session — the local tool server didn't start. "
                "Restart OpenShrimp to restore them."
            ),
            **kwargs,
        )
    except Exception:
        logger.debug(
            "Failed to send degraded-tools warning to %s", scope,
            exc_info=True,
        )


async def _notify_backend_swapped(
    bot: Bot, scope: ChatScope, old_backend: str, new_backend: str,
) -> None:
    """Tell the user a backend change reset the conversation.

    Sessions are backend-scoped, so switching a context's backend can't carry
    history across — without this notice the conversation would silently reset
    and the user wouldn't know why.  Best-effort: never break the turn.
    """
    try:
        kwargs: dict[str, Any] = {}
        if scope.thread_id is not None:
            kwargs["message_thread_id"] = scope.thread_id
        await bot.send_message(
            chat_id=scope.chat_id,
            text=(
                f"🔄 Backend changed from {old_backend} to {new_backend} for "
                "this context — starting a fresh conversation. History from "
                "the previous backend can't be carried over."
            ),
            **kwargs,
        )
    except Exception:
        logger.debug(
            "Failed to send backend-swap notice to %s", scope,
            exc_info=True,
        )


async def get_or_create_session(
    scope: ChatScope,
    context_name: str,
    context: ContextConfig,
    session_id: str | None,
    callback_context: CallbackContext,
    bot: Bot | None = None,
    db: Any | None = None,
    config: Any | None = None,
    job_queue: Any | None = None,
    terminal_base_url: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    sandbox_manager: SandboxManager | None = None,
    mcp_proxy: Any | None = None,
    backend: Backend | None = None,
) -> AgentSession:
    """Return an existing live session or create a new one.

    If a session already exists for *scope* with the same context,
    return it (after updating the callback context).  Otherwise build
    ``BackendOptions``, call ``backend.make_client(opts)``, connect, and
    store the resulting ``BackendClient``.

    Args:
        scope: ChatScope identifying the chat/thread.
        context_name: Name of the active context.
        context: Context configuration (directory, model, etc.).
        session_id: Session ID for ``--resume`` (only used when creating
            a new client).
        callback_context: Mutable callback holder to bind into hooks.
        backend: The agent backend to build the client from.  Optional;
            falls back to the process-wide default (``set_default_backend``)
            so existing call sites need not thread it through immediately.

    Returns:
        An ``AgentSession`` with a connected client ready for ``query()``.
    """
    backend = resolve_backend(backend, context=context)
    existing = _active_sessions.get(scope)
    if existing is not None:
        same_context = existing.context_name == context_name
        same_backend = existing.backend is backend
        if same_context and same_backend:
            if existing.client.is_alive():
                existing.callback_context.request_approval = callback_context.request_approval
                existing.callback_context.handle_user_questions = callback_context.handle_user_questions
                existing.callback_context.is_edit_auto_approved = callback_context.is_edit_auto_approved
                existing.callback_context.notify_auto_approved_edit = callback_context.notify_auto_approved_edit
                existing.callback_context.is_tool_auto_approved = callback_context.is_tool_auto_approved
                existing.callback_context.get_session_approved_dirs = callback_context.get_session_approved_dirs
                existing.callback_context.request_host_bash_approval = callback_context.request_host_bash_approval
                existing.last_activity = time.monotonic()
                logger.info(
                    "Reusing live client for scope %s context %s",
                    scope,
                    context_name,
                )
                return existing
            else:
                logger.warning(
                    "CLI process dead for scope %s context %s, closing stale session",
                    scope,
                    context_name,
                )
                await close_session(scope)
        elif not same_context:
            logger.info(
                "Context changed for scope %s (%s -> %s), closing old client",
                scope,
                existing.context_name,
                context_name,
            )
            await close_session(scope)
        else:
            # Session IDs are backend-scoped; drop the resume id on rebuild
            # and clear the persisted mapping so a later turn (or a cold
            # start after restart) doesn't try to resume the previous
            # backend's session — that would fail over to a fresh session
            # with a spurious "failed to resume" warning.
            old_backend_name = existing.backend.name if existing.backend else "?"
            logger.info(
                "Backend changed for scope %s context %s (%s -> %s), "
                "closing old client",
                scope,
                context_name,
                old_backend_name,
                backend.name,
            )
            await close_session(scope)
            session_id = None
            if db is not None:
                try:
                    await delete_session(db, scope, context_name)
                except Exception:
                    logger.warning(
                        "Failed to clear persisted session for scope %s "
                        "context %s after backend swap",
                        scope,
                        context_name,
                        exc_info=True,
                    )
            if bot is not None:
                await _notify_backend_swapped(
                    bot, scope, old_backend_name, backend.name,
                )

    can_use_tool = backend.make_can_use_tool(
        request_approval=_make_approval_proxy(callback_context),
        cwd=context.directory,
        additional_directories=context.additional_directories or None,
        handle_user_questions=_make_questions_proxy(callback_context),
        is_edit_auto_approved=_make_edit_approved_proxy(callback_context),
        notify_auto_approved_edit=_make_edit_notify_proxy(callback_context),
        chat_id=scope.chat_id,
        is_tool_auto_approved=_make_tool_approved_proxy(callback_context),
        is_containerized=is_sandboxed(context),
        get_session_approved_dirs=_make_session_dirs_proxy(callback_context),
        request_host_bash_approval=_make_host_bash_approval_proxy(callback_context),
        policy=backend.policy,
    )

    _last_stderr: list[str] = [""]
    _stderr_repeat_count: list[int] = [0]

    def _log_stderr(line: str) -> None:
        stripped = line.rstrip()
        if stripped == _last_stderr[0]:
            _stderr_repeat_count[0] += 1
            if _stderr_repeat_count[0] in (10, 50, 100):
                logger.info(
                    "CLI stderr (repeated %d times): %s",
                    _stderr_repeat_count[0], stripped,
                )
            return
        if _stderr_repeat_count[0] > 1:
            logger.info(
                "CLI stderr (repeated %d times total): %s",
                _stderr_repeat_count[0], _last_stderr[0],
            )
        _last_stderr[0] = stripped
        _stderr_repeat_count[0] = 1
        logger.info("CLI stderr: %s", stripped)

    # Auto-approve the built-in OpenShrimp MCP tools (send_file, send_photo)
    # alongside whatever the user configured.  The ``mcp__openshrimp__*``
    # tools are served by the MCP proxy; when it failed to start
    # (``mcp_proxy is None``, degraded mode) the ``openshrimp`` server is
    # not registered, so we must NOT advertise its tools — otherwise the
    # agent would attempt calls that surface as "unknown tool" errors
    # mid-conversation.
    allowed_tools = list(context.allowed_tools or [])
    # Seed the backend's session-start auto-approve list (the backend's
    # native vocabulary for tools whose interactive default is auto-allow
    # and which have no user-facing approval value in OpenShrimp's flow:
    # async task management, mode transitions, MCP discovery).  These
    # flow through the backend's interactive default and never reach
    # ``can_use_tool``.
    allowed_tools.extend(backend.policy.auto_approved_at_session_start())
    if mcp_proxy is not None:
        allowed_tools.append("mcp__openshrimp__send_file")
        if scope.thread_id is not None:
            allowed_tools.append("mcp__openshrimp__edit_topic")
        # Auto-approve scheduling tools when available.
        if db is not None and config is not None and job_queue is not None:
            allowed_tools.extend([
                "mcp__openshrimp__create_schedule",
                "mcp__openshrimp__list_schedules",
                "mcp__openshrimp__delete_schedule",
            ])
        # Auto-approve ask_context at the parent session: it renders its
        # own tailored approval card per call, so the generic can_use_tool
        # card must not also fire (which would show the raw wire name).
        if config is not None:
            allowed_tools.append("mcp__openshrimp__ask_context")
    # Auto-approve computer use tools when enabled.
    _computer_use_enabled = (
        (context.container is not None and context.container.computer_use)
        or (context.sandbox is not None and context.sandbox.computer_use)
    )
    if _computer_use_enabled:
        if mcp_proxy is not None:
            allowed_tools.extend([
                "mcp__openshrimp__computer_screenshot",
                "mcp__openshrimp__computer_click",
                "mcp__openshrimp__computer_type",
                "mcp__openshrimp__computer_key",
                "mcp__openshrimp__computer_scroll",
                "mcp__openshrimp__computer_toplevel",
            ])
        # Auto-approve Playwright MCP browser tools (core + tabs,
        # always enabled).  Tool names from microsoft/playwright-mcp.
        allowed_tools.extend([
            # Core automation
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_close",
            "mcp__playwright__browser_console_messages",
            "mcp__playwright__browser_drag",
            "mcp__playwright__browser_evaluate",
            "mcp__playwright__browser_file_upload",
            "mcp__playwright__browser_fill_form",
            "mcp__playwright__browser_handle_dialog",
            "mcp__playwright__browser_hover",
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_navigate_back",
            "mcp__playwright__browser_network_requests",
            "mcp__playwright__browser_press_key",
            "mcp__playwright__browser_resize",
            "mcp__playwright__browser_run_code",
            "mcp__playwright__browser_select_option",
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_take_screenshot",
            "mcp__playwright__browser_type",
            "mcp__playwright__browser_wait_for",
            # Tab management
            "mcp__playwright__browser_tabs",
            # PDF (opt-in via --caps=pdf)
            "mcp__playwright__browser_pdf_save",
            # Testing assertions (opt-in via --caps=testing)
            "mcp__playwright__browser_generate_locator",
            "mcp__playwright__browser_verify_element_visible",
            "mcp__playwright__browser_verify_list_visible",
            "mcp__playwright__browser_verify_text_visible",
            "mcp__playwright__browser_verify_value",
        ])

    # When sandboxed, launch the agent inside an isolated environment.  The
    # launch fork is the *one* legitimate per-backend branch here: the
    # wrapped-CLI flavour points ``cli_path`` at a wrapper script; the
    # served-endpoint flavour returns a host-reachable endpoint that rides in
    # ``options.extra["endpoint"]``.  Everything downstream (make_client,
    # make_tool_server, options) stays backend-uniform.
    sandbox: Sandbox | None = None
    runtime: AgentRuntime | None = None
    cli_path: str | None = None
    wrapper_cleanup_paths: list[str] = []
    served_endpoint: Any = None  # the served endpoint handle, or None
    is_containerized = is_sandboxed(context)
    if is_containerized:
        assert sandbox_manager is not None, (
            "sandbox_manager is required for containerized contexts"
        )

        # Serialise sandbox boot per context_name so two scopes sharing
        # the same libvirt domain (or Docker container) don't race on VM
        # boot, virtiofsd startup, port allocation, etc.
        ctx_lock = _context_locks.setdefault(context_name, asyncio.Lock())
        async with ctx_lock:
            # Resolved before create_sandbox so the runtime's image bundle +
            # served-launch home mounts feed the Docker image/run-argv
            # selection.  ``make_runtime`` is pure/cheap, so computing it
            # first is safe.
            _runtime = backend.make_runtime(
                sandbox_manager.agent_home_dir(context_name),
                context_name=context_name,
                model=context.model,
            )
            runtime = _runtime

            # The runtime selects the Docker image/run-argv bundle and (for
            # served launches) the extra host-synced home mounts; VM backends
            # consume only the served-launch's mounts.
            sandbox = sandbox_manager.create_sandbox(
                context_name,
                context,
                runtime=_runtime,
            )

            # Check if the environment needs building or the sandbox
            # needs starting — send user feedback before potentially
            # slow operations.
            needs_build = not sandbox.environment_ready()
            needs_start = not needs_build and not sandbox.running()
            if (needs_build or needs_start) and bot is not None:
                log_file = sandbox_manager.register_build(context_name)

                if needs_build:
                    progress_text = (
                        "Building container image for the first time, "
                        "this may take a few minutes\\.\\.\\."
                    )
                else:
                    progress_text = "Starting sandbox\\.\\.\\."

                keyboard = None
                if terminal_base_url and config is not None:
                    app_url = (
                        f"{terminal_base_url}/terminal/"
                        f"?type=container_build&id={context_name}"
                    )
                    keyboard = InlineKeyboardMarkup([[
                        make_web_app_button(
                            "📺 View build log",
                            app_url,
                            chat_id=scope.chat_id,
                            user_id=user_id,
                            bot_token=config.telegram.token,
                            is_private_chat=is_private_chat,
                        )
                    ]])
                await bot.send_message(
                    chat_id=scope.chat_id,
                    message_thread_id=scope.thread_id,
                    text=progress_text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            else:
                log_file = None

            _sandbox = sandbox  # capture for closure
            _mgr = sandbox_manager  # capture for closure

            def _ensure_and_start_agent() -> "AgentHandle":
                try:
                    _sandbox.ensure_environment(log_file=log_file)
                    _sandbox.ensure_running(log_file=log_file)
                finally:
                    if log_file is not None:
                        assert _mgr is not None
                        _mgr.unregister_build(context_name)
                _sandbox.provision_workspace()
                return _sandbox.start_agent(_runtime)

            handle = await asyncio.to_thread(_ensure_and_start_agent)
            # WrappedCLI → cli_path; ServedEndpoint → endpoint.
            cli_path = handle.cli_path
            wrapper_cleanup_paths = handle.cleanup_paths
            served_endpoint = handle.endpoint
            if served_endpoint is not None:
                logger.info(
                    "Sandbox context '%s': using served endpoint %s",
                    context_name,
                    served_endpoint.base_url,
                )
            else:
                logger.info(
                    "Sandbox context '%s': using wrapper %s",
                    context_name,
                    cli_path,
                )

    # Backend-specific overflow: the sandbox-provided served endpoint rides in
    # ``extra`` (it is not an honoured-intersection field).  When no sandbox
    # supplies one, it stays unset → the client spawns its own host-local
    # server.  Backends that don't use a served endpoint ignore ``extra``.
    extra: dict[str, Any] = {}
    if served_endpoint is not None:
        extra["endpoint"] = served_endpoint
    extra["handle_questions"] = _make_opencode_questions_proxy(callback_context)

    options = BackendOptions(
        cwd=context.directory,
        model=context.model,
        effort=context.effort,
        allowed_tools=allowed_tools,
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
        cli_path=cli_path,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
        extra=extra,
    )

    system_prompt_parts: list[str] = []

    if scope.thread_id is not None:
        system_prompt_parts.append(
            "This conversation is in a Telegram forum topic. "
            "After your first response, use the edit_topic tool to set "
            "a concise title (max 128 chars) summarizing the conversation, "
            "and optionally an icon using a standard emoji (e.g. 📝, 🔥, "
            "🤖, 💬). If the topic changes significantly later, update "
            "the title again."
        )

    # Check if this sandbox supports computer-use (has a screenshots dir).
    _computer_use_sandbox = sandbox if (
        sandbox is not None and sandbox.get_screenshots_dir() is not None
    ) else None
    if _computer_use_sandbox is not None:
        system_prompt_parts.append(
            "This context has computer use (GUI interaction) enabled. "
            "You have access to a headless 1280x720 Linux desktop with "
            "a Wayland compositor (labwc), a web browser (Chromium), "
            "and a terminal (foot).\n\n"
            "For browser/web testing, prefer the Playwright MCP tools "
            "(browser_navigate, browser_click, browser_type, browser_snapshot, "
            "browser_screenshot, etc.) — they provide structured DOM access "
            "via accessibility snapshots which is far more reliable than "
            "pixel-based interaction. Use browser_snapshot to read the page "
            "structure before interacting.\n\n"
            "For non-browser GUI interaction (terminal, native apps, or when "
            "Playwright tools are insufficient), use the pixel-based tools: "
            "computer_screenshot to see the screen, computer_click to click "
            "at coordinates, computer_type to type text, computer_key for "
            "special keys and combos, computer_scroll to scroll, and "
            "computer_toplevel to switch between windows. Always take a "
            "screenshot first to understand the current state."
        )

    if system_prompt_parts:
        options.system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(system_prompt_parts),
        }

    # Register OpenShrimp's own MCP tools (send_file, edit_topic, schedules,
    # host_bash, computer use) over the MCP proxy's host-loopback HTTP
    # endpoint so the agent can reach them.  The handlers run in *this*
    # process; the HTTP hop is transport only and adds no sandbox boundary.
    if bot is not None:
        mcp_servers: dict[str, Any] = {}

        # Sudo mode (host_bash) is registered only when the context's
        # sandbox config explicitly opts in. Commands run with cwd set to
        # the context's source directory so they operate on the same tree
        # the agent sees inside the sandbox, just unsandboxed.
        _host_bash_workdir: str | None = None
        if (
            context.sandbox is not None
            and context.sandbox.allow_host_escape
        ):
            _host_bash_workdir = context.directory

        if mcp_proxy is not None:
            def _tool_factory() -> list[OpenShrimpTool]:
                return create_openshrimp_tools(
                    bot=bot, chat_id=scope.chat_id, thread_id=scope.thread_id,
                    db=db, config=config, job_queue=job_queue,
                    sandbox=sandbox,
                    context_name=context_name,
                    user_id=user_id,
                    is_private_chat=is_private_chat,
                    host_bash_workdir=_host_bash_workdir,
                    terminal_base_url=terminal_base_url,
                )

            # Sandboxed CLIs must reach the host proxy via the sandbox's
            # host address; non-sandboxed CLIs use loopback.
            host_ip = (
                sandbox.host_address
                if is_containerized and sandbox is not None
                else "127.0.0.1"
            )
            # The backend selects the installer; the manager calls it with
            # the proxy handle and scope identity.
            install_tools = backend.make_tool_server(_tool_factory)
            mcp_servers["openshrimp"] = install_tools(
                mcp_proxy,
                _tool_factory,
                context_name=context_name,
                chat_id=scope.chat_id,
                thread_id=scope.thread_id,
                user_id=user_id,
                host_ip=host_ip,
            )
        else:
            # Degraded mode: the proxy failed to start, so OpenShrimp tools
            # cannot be served.  Omit the server entirely (the matching
            # ``allowed_tools`` appends were already skipped above) and warn
            # the user once.  host_bash hook wiring is also moot here since
            # the tool is absent.
            logger.warning("OpenShrimp tools omitted: MCP proxy unavailable.")
            await _warn_tools_degraded_once(bot, scope)

        # Add Playwright MCP for structured browser automation in
        # computer-use contexts.  The CLI runs inside the sandbox,
        # so it spawns the Playwright MCP server as a child process
        # inside the sandbox automatically.
        if _computer_use_sandbox is not None:
            mcp_servers["playwright"] = {
                "command": "npx",
                "args": [
                    "@playwright/mcp",
                    "--cdp-endpoint", "http://localhost:9222",
                    "--caps", "pdf",
                ],
            }

        # Keep MCP server credentials on the host; sandbox sees only
        # HTTP endpoints via the proxy.  Covers stdio MCP servers
        # (spawned on the host) and HTTP/SSE MCP servers (reverse-
        # proxied so OAuth tokens stay on the host).
        if is_containerized and mcp_proxy is not None and sandbox is not None:
            mcp_source = backend.mcp_config_source()
            stdio_servers = mcp_source.stdio_servers(context)
            http_servers = mcp_source.http_servers(context)
            if stdio_servers or http_servers:
                token = mcp_proxy.register_context(
                    context_name,
                    servers=stdio_servers or None,
                    http_servers=http_servers or None,
                )
                host_ip = sandbox.host_address
                for name in stdio_servers:
                    mcp_servers[name] = {
                        "type": "http",
                        "url": mcp_proxy.get_proxy_url(
                            context_name, name, host_ip
                        ),
                        "headers": {
                            "Authorization": f"Bearer {token}",
                        },
                    }
                for name, http_cfg in http_servers.items():
                    mcp_servers[name] = {
                        "type": http_cfg.transport,
                        "url": mcp_proxy.get_http_proxy_url(
                            context_name, name, host_ip
                        ),
                        "headers": {
                            "Authorization": f"Bearer {token}",
                        },
                    }
                logger.info(
                    "Injected %d stdio + %d HTTP proxied MCP server(s) "
                    "for sandboxed context '%s': stdio=[%s] http=[%s]",
                    len(stdio_servers),
                    len(http_servers),
                    context_name,
                    ", ".join(stdio_servers),
                    ", ".join(http_servers),
                )

        options.mcp_servers = mcp_servers

    if session_id:
        options.resume = session_id
        logger.info(
            "Creating new client for scope %s: resuming session %s in %s",
            scope,
            session_id,
            context.directory,
        )
    else:
        logger.info(
            "Creating new client for scope %s: new session in %s",
            scope,
            context.directory,
        )

    # The backend builds (does not connect) the client; ``connect()`` owns the
    # resume-fallback retry.  Backends surface the fallback differently:
    #
    # * One rebuilds its inner client with ``resume`` cleared and mutates *this*
    #   ``options.resume`` to None — so a stale resume reads back as
    #   ``options.resume is None``.  Its ``client.session_id`` is not populated
    #   until the first ``receive_response`` (the init message), so we cannot
    #   rely on it here.
    # * Another validates the resume target in ``connect()`` and, on a stale
    #   resume, creates a fresh session — leaving ``client.session_id`` already
    #   set to the *new* id at connect time (it does not touch ``options.resume``).
    #
    # Reconcile both: prefer the client's own post-connect view when it has
    # one, else fall back to the ``options.resume`` signal.
    client = backend.make_client(options)
    await client.connect()
    if session_id:
        client_sid = client.session_id
        if client_sid is not None:
            session_id = client_sid
        elif options.resume is None:
            session_id = None

    session = AgentSession(
        client=client,
        session_id=session_id,
        context_name=context_name,
        callback_context=callback_context,
        sandbox=sandbox,
        runtime=runtime,
        # Always thread the proxy through: it now serves OpenShrimp's own
        # tools for every context (not just sandboxed ones), so its tool
        # scope must be unregistered on close regardless of containerisation.
        mcp_proxy=mcp_proxy,
        wrapper_cleanup_paths=wrapper_cleanup_paths,
        backend=backend,
    )

    # Register this sandbox for host-side credential syncing.  The watcher
    # only starts when the runtime declares a non-default
    # ``watch_host_credentials`` body *and* host credentials are present —
    # a runtime that re-injects per dispatch (or has no host store at all)
    # is a no-op here.
    if sandbox is not None and runtime is not None:
        home_dir = runtime.home_mount.host_dir
        if home_dir.exists():
            register_cred_sandbox(
                runtime.name,
                context_name,
                home_dir,
                write=runtime.write_cred_target,
                watch=runtime.watch_host_credentials,
                host_credentials_available=runtime.host_credentials_available,
            )

    _active_sessions[scope] = session
    return session


async def reconnect_session(
    scope: ChatScope,
    context_name: str,
    context: ContextConfig,
    bot: Bot | None = None,
    db: Any | None = None,
    config: Any | None = None,
    job_queue: Any | None = None,
    terminal_base_url: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    sandbox_manager: SandboxManager | None = None,
    mcp_proxy: Any | None = None,
    backend: Backend | None = None,
) -> AgentSession | None:
    """Reconnect after a mid-session container crash.

    Closes the dead session, ensures the container is running again,
    and creates a new client that resumes the existing session.

    Returns the new ``AgentSession``, or ``None`` if reconnection fails.
    """
    old_session = _active_sessions.get(scope)
    if old_session is None:
        return None

    session_id = old_session.session_id
    callback_context = old_session.callback_context
    # Never cross backends on reconnect; explicit ``backend`` still wins.
    if backend is None:
        backend = old_session.backend

    # Tear down the dead client (ignore errors — it's already dead).
    await close_session(scope)

    if not session_id:
        logger.warning(
            "Cannot reconnect scope %s: no session_id to resume", scope
        )
        return None

    logger.info(
        "Reconnecting scope %s: resuming session %s after container crash",
        scope, session_id,
    )

    try:
        return await get_or_create_session(
            scope=scope,
            context_name=context_name,
            context=context,
            session_id=session_id,
            callback_context=callback_context,
            bot=bot,
            db=db,
            config=config,
            job_queue=job_queue,
            terminal_base_url=terminal_base_url,
            user_id=user_id,
            is_private_chat=is_private_chat,
            sandbox_manager=sandbox_manager,
            mcp_proxy=mcp_proxy,
            backend=backend,
        )
    except Exception:
        logger.exception(
            "Failed to reconnect session for scope %s", scope
        )
        return None


async def close_session(scope: ChatScope) -> None:
    """Close and remove the session for *scope*, if any."""
    session = _active_sessions.pop(scope, None)
    if session is None:
        return
    # Unregister from credential syncing if this was a sandboxed session.
    # Check if any other active session still uses the same context before
    # removing the sync target.
    if session.sandbox is not None and session.runtime is not None:
        ctx = session.context_name
        still_used = any(
            s.context_name == ctx and s.sandbox is not None
            for s in _active_sessions.values()
        )
        if not still_used:
            unregister_cred_sandbox(session.runtime.name, ctx)
    # Unregister proxied MCP servers when no other session needs them.
    if session.mcp_proxy is not None:
        ctx = session.context_name
        still_used = any(
            s.context_name == ctx and s.mcp_proxy is not None
            for s in _active_sessions.values()
        )
        if not still_used:
            await session.mcp_proxy.unregister_context(ctx)
    try:
        async with asyncio.timeout(5):
            await session.client.disconnect()
        logger.info("Closed client for scope %s", scope)
    except (Exception, TimeoutError):
        logger.debug("Error/timeout closing client for scope %s", scope, exc_info=True)
    # Clean up per-session temp files (wrapper script, sandbox profile, etc.).
    # The sandbox itself is shared across sessions and managed by the
    # SandboxManager.
    for path in session.wrapper_cleanup_paths:
        Path(path).unlink(missing_ok=True)
        logger.debug("Removed temp file %s", path)


async def close_all_sessions() -> None:
    """Close all active sessions (for shutdown).

    Runs all disconnects in parallel so shutdown latency is dominated by
    the slowest single client (up to the 5s per-client timeout in
    ``close_session``), not the sum across every active scope.
    """
    scopes = list(_active_sessions.keys())
    if not scopes:
        return
    await asyncio.gather(
        *(close_session(scope) for scope in scopes),
        return_exceptions=True,
    )


async def close_sessions_for_context(context_name: str) -> int:
    """Close all active sessions bound to *context_name*.

    Returns the number of sessions closed.  Used before sandbox
    reboot/reset so SDK subprocesses don't orphan onto a dead runtime.
    """
    scopes = [
        scope for scope, session in _active_sessions.items()
        if session.context_name == context_name
    ]
    if not scopes:
        return 0
    await asyncio.gather(
        *(close_session(scope) for scope in scopes),
        return_exceptions=True,
    )
    return len(scopes)


async def _sweep_idle_sessions() -> None:
    """Periodically close sessions that have been idle too long."""
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        stale = [
            scope for scope, session in _active_sessions.items()
            if now - session.last_activity > _IDLE_TIMEOUT
        ]
        for scope in stale:
            logger.info(
                "Closing idle session for scope %s (idle %.0fs)",
                scope,
                now - _active_sessions[scope].last_activity,
            )
            await close_session(scope)


def start_idle_sweep() -> None:
    """Start the background idle-session sweep task."""
    global _idle_sweep_task
    if _idle_sweep_task is None or _idle_sweep_task.done():
        _idle_sweep_task = asyncio.create_task(_sweep_idle_sessions())
        logger.info("Started idle session sweep (timeout=%ds)", _IDLE_TIMEOUT)


def stop_idle_sweep() -> None:
    """Cancel the idle-session sweep task."""
    global _idle_sweep_task
    if _idle_sweep_task is not None:
        _idle_sweep_task.cancel()
        _idle_sweep_task = None


def get_session(scope: ChatScope) -> AgentSession | None:
    """Return the active session for *scope*, or None."""
    return _active_sessions.get(scope)


async def stop_background_task(scope: ChatScope, task_id: str) -> bool:
    """Send a stop signal for a background task.  Returns True on success."""
    session = _active_sessions.get(scope)
    if session is None:
        logger.warning("No active session for scope %s to stop task %s", scope, task_id)
        return False
    try:
        logger.info("Sending stop signal for task %s in scope %s", task_id, scope)
        await session.client.stop_task(task_id)
        logger.info("Stop signal sent successfully for task %s", task_id)
        return True
    except Exception:
        logger.exception("Failed to stop task %s for scope %s", task_id, scope)
        return False


def has_session(scope: ChatScope) -> bool:
    """Return True if *scope* has a live session."""
    return scope in _active_sessions


def _reinject_runtime_credentials(session: AgentSession) -> None:
    """Re-run the runtime's ``inject`` hook against the sandbox home.

    No-op when the runtime declares ``re_inject_on_dispatch=False`` (the
    default).  See :class:`AgentRuntime` for the refresh model.  Best-effort:
    a failing inject must not block the query.
    """
    runtime = session.runtime
    if runtime is None or not runtime.re_inject_on_dispatch:
        return
    try:
        runtime.inject(runtime.home_mount.host_dir)
    except Exception:
        logger.debug(
            "Per-dispatch credential re-inject failed for runtime %s",
            runtime.name, exc_info=True,
        )


async def query_and_stream(
    session: AgentSession,
    prompt: str,
) -> AsyncIterator[AgentEvent]:
    """Send a query on an existing session and yield events."""
    session.last_activity = time.monotonic()
    logger.info("Sending query on live client: %s", prompt[:200])
    _reinject_runtime_credentials(session)
    await session.client.query(prompt)
    async for message in receive_events(session):
        yield message


async def receive_events(
    session: AgentSession,
) -> AsyncIterator[AgentEvent]:
    """Yield events from an existing session without sending a new query.

    Use this when ``session.client.query()`` has already been called
    separately (e.g. for message-injection support).
    """
    async for message in session.client.receive_response():
        if isinstance(message, SystemMessage):
            sid = getattr(message, "session_id", None)
            if sid:
                session.session_id = sid
        elif isinstance(message, ResultMessage):
            if message.session_id:
                session.session_id = message.session_id
        session.last_activity = time.monotonic()
        yield message


def _make_approval_proxy(
    ctx: CallbackContext,
) -> ApprovalCallback:
    async def _proxy(
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        suggested_session_dir: str | None = None,
    ) -> bool:
        if ctx.request_approval is None:
            logger.warning("No approval callback set, denying tool %s", tool_name)
            return False
        return await ctx.request_approval(
            tool_name, tool_input, tool_use_id, suggested_session_dir,
        )

    return _proxy


def _make_questions_proxy(
    ctx: CallbackContext,
) -> QuestionCallback:
    async def _proxy(
        questions: list[dict[str, Any]],
    ) -> dict[str, str]:
        if ctx.handle_user_questions is None:
            logger.warning("No question callback set, returning empty answers")
            return {}
        return await ctx.handle_user_questions(questions)

    return _proxy


def _make_opencode_questions_proxy(
    ctx: CallbackContext,
) -> Callable[[list[dict[str, Any]]], Any]:
    async def _proxy(
        questions: list[dict[str, Any]],
    ) -> list[list[str]]:
        if ctx.handle_user_questions is None:
            logger.warning("No question callback set, returning empty answers")
            return []

        answers_by_question = await ctx.handle_user_questions(questions)
        answers: list[list[str]] = []
        for question in questions:
            question_text = str(question.get("question", ""))
            answer = answers_by_question.get(question_text, "")
            if question.get("multiSelect") and answer != "None selected":
                answers.append([
                    part.strip() for part in answer.split(", ") if part.strip()
                ])
            else:
                answers.append([answer] if answer else [])
        return answers

    return _proxy


def _make_edit_approved_proxy(
    ctx: CallbackContext,
) -> Callable[[], bool]:
    def _proxy() -> bool:
        if ctx.is_edit_auto_approved is None:
            return False
        return ctx.is_edit_auto_approved()

    return _proxy


def _make_edit_notify_proxy(
    ctx: CallbackContext,
) -> EditNotifyCallback:
    async def _proxy(
        tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        if ctx.notify_auto_approved_edit is None:
            return
        await ctx.notify_auto_approved_edit(tool_name, tool_input)

    return _proxy


def _make_tool_approved_proxy(
    ctx: CallbackContext,
) -> Callable[[str, dict[str, Any]], bool]:
    def _proxy(tool_name: str, tool_input: dict[str, Any]) -> bool:
        if ctx.is_tool_auto_approved is None:
            return False
        return ctx.is_tool_auto_approved(tool_name, tool_input)

    return _proxy


def _make_session_dirs_proxy(
    ctx: CallbackContext,
) -> Callable[[], list[str]]:
    def _proxy() -> list[str]:
        if ctx.get_session_approved_dirs is None:
            return []
        return ctx.get_session_approved_dirs()

    return _proxy


def _make_host_bash_approval_proxy(
    ctx: CallbackContext,
) -> HostBashApprovalCallback:
    async def _proxy(
        tool_input: dict[str, Any], tool_use_id: str,
    ) -> Any:
        if ctx.request_host_bash_approval is None:
            logger.warning(
                "host_bash invoked but no approval callback set; denying"
            )
            return "denied"
        return await ctx.request_host_bash_approval(tool_input, tool_use_id)

    return _proxy
