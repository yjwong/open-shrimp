"""Persistent Claude Agent SDK client manager for OpenShrimp.

Manages long-lived ClaudeSDKClient instances keyed by ChatScope, so the CLI
subprocess stays alive across multiple messages in the same conversation.
This avoids the "Continue from where you left off." injection that the CLI
performs when it detects an interrupted turn on session resume.

Only the first message in a session uses ``--resume`` to restore history;
subsequent messages simply call ``client.query()`` on the already-connected
client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ProcessError,
    ResultMessage,
    SystemMessage,
)

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.web_app_button import make_web_app_button

from open_shrimp.agent import AgentEvent
from open_shrimp.config import ContextConfig
from open_shrimp.db import ChatScope
from open_shrimp.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    QuestionCallback,
)
import sys

from open_shrimp.container import (
    COMPUTER_USE_IMAGE,
    CONTAINER_IMAGE,
    build_cli_wrapper as docker_build_cli_wrapper,
    cleanup_wrapper as docker_cleanup_wrapper,
    ensure_computer_use_image as docker_ensure_computer_use_image,
    ensure_container_running as docker_ensure_container,
    ensure_image as docker_ensure_image,
    get_screenshots_dir,
    register_build,
    unregister_build,
)
from open_shrimp.sandbox import (
    build_cli_wrapper as sandbox_build_cli_wrapper,
    cleanup_wrapper as sandbox_cleanup_wrapper,
)
from open_shrimp.tools import create_openshrimp_mcp_server

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


@dataclass
class AgentSession:
    """A long-lived SDK client associated with a chat scope."""

    client: ClaudeSDKClient
    session_id: str | None = None
    context_name: str = ""
    callback_context: CallbackContext = field(default_factory=CallbackContext)
    container_wrapper_path: str | None = None


_active_sessions: dict[ChatScope, AgentSession] = {}


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
) -> AgentSession:
    """Return an existing live session or create a new one.

    If a session already exists for *scope* with the same context,
    return it (after updating the callback context).  Otherwise create a
    fresh ``ClaudeSDKClient``, connect, and store it.

    Args:
        scope: ChatScope identifying the chat/thread.
        context_name: Name of the active context.
        context: Context configuration (directory, model, etc.).
        session_id: Session ID for ``--resume`` (only used when creating
            a new client).
        callback_context: Mutable callback holder to bind into hooks.

    Returns:
        An ``AgentSession`` with a connected client ready for ``query()``.
    """
    existing = _active_sessions.get(scope)
    if existing is not None:
        if existing.context_name == context_name:
            existing.callback_context.request_approval = callback_context.request_approval
            existing.callback_context.handle_user_questions = callback_context.handle_user_questions
            existing.callback_context.is_edit_auto_approved = callback_context.is_edit_auto_approved
            existing.callback_context.notify_auto_approved_edit = callback_context.notify_auto_approved_edit
            existing.callback_context.is_tool_auto_approved = callback_context.is_tool_auto_approved
            logger.info(
                "Reusing live client for scope %s context %s",
                scope,
                context_name,
            )
            return existing
        else:
            logger.info(
                "Context changed for scope %s (%s -> %s), closing old client",
                scope,
                existing.context_name,
                context_name,
            )
            await close_session(scope)

    from open_shrimp.hooks import make_can_use_tool

    can_use_tool = make_can_use_tool(
        request_approval=_make_approval_proxy(callback_context),
        cwd=context.directory,
        additional_directories=context.additional_directories or None,
        handle_user_questions=_make_questions_proxy(callback_context),
        is_edit_auto_approved=_make_edit_approved_proxy(callback_context),
        notify_auto_approved_edit=_make_edit_notify_proxy(callback_context),
        chat_id=scope.chat_id,
        is_tool_auto_approved=_make_tool_approved_proxy(callback_context),
        is_containerized=context.container is not None and context.container.enabled,
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
    # alongside whatever the user configured.
    allowed_tools = list(context.allowed_tools or [])
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
    # Auto-approve computer use tools when enabled.
    if (context.container is not None and context.container.computer_use):
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

    # When containerized, generate a wrapper script that runs the Claude
    # CLI in an isolated environment.  On macOS we use sandbox-exec (since
    # Docker runs a Linux VM and would break the native Claude CLI binary);
    # on Linux we use Docker.  The wrapper is pointed at via cli_path; all
    # other SDK machinery (stdin/stdout streaming, canUseTool, MCP) is
    # unchanged.
    wrapper_path: str | None = None
    cli_path: str | None = None
    is_containerized = context.container is not None and context.container.enabled
    if is_containerized:
        if sys.platform == "darwin":
            wrapper_path = sandbox_build_cli_wrapper(
                context_name=context_name,
                project_dir=context.directory,
                additional_directories=context.additional_directories or None,
            )
            logger.info(
                "Sandboxed context '%s': using sandbox-exec wrapper %s",
                context_name,
                wrapper_path,
            )
        else:
            # Check if the image needs building — send user feedback
            # before the potentially slow build.
            assert context.container is not None
            custom_dockerfile = context.container.dockerfile
            docker_in_docker = context.container.docker_in_docker
            computer_use = context.container.computer_use

            if computer_use and custom_dockerfile:
                image_name = f"openshrimp-claude:{context_name}"
            elif computer_use:
                image_name = COMPUTER_USE_IMAGE
            elif custom_dockerfile:
                image_name = f"openshrimp-claude:{context_name}"
            else:
                image_name = CONTAINER_IMAGE

            import subprocess as _subprocess
            inspect_result = _subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
            )
            needs_build = inspect_result.returncode != 0
            if needs_build and bot is not None:
                # Register the build so the terminal mini app can tail
                # the log file while the image is being built.
                log_file = register_build(context_name)

                build_text = (
                    "Building container image for the first time, "
                    "this may take a few minutes\\.\\.\\."
                )
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
                    text=build_text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            else:
                log_file = None

            def _ensure_and_build_wrapper() -> str:
                try:
                    if computer_use and custom_dockerfile:
                        # Build computer-use base first, then layer the
                        # custom Dockerfile on top.
                        docker_ensure_computer_use_image(
                            log_file=log_file,
                        )
                        docker_ensure_image(
                            image_name=image_name,
                            dockerfile=custom_dockerfile,
                            base_image=COMPUTER_USE_IMAGE,
                            log_file=log_file,
                        )
                    elif computer_use:
                        docker_ensure_computer_use_image(
                            image_name=image_name,
                            log_file=log_file,
                        )
                    else:
                        docker_ensure_image(
                            image_name=image_name,
                            dockerfile=custom_dockerfile,
                            log_file=log_file,
                        )
                finally:
                    if log_file is not None:
                        unregister_build(context_name)

                docker_ensure_container(
                    context_name=context_name,
                    project_dir=context.directory,
                    additional_directories=context.additional_directories or None,
                    docker_in_docker=docker_in_docker,
                    computer_use=computer_use,
                    image_name=image_name,
                )
                return docker_build_cli_wrapper(
                    context_name=context_name,
                    project_dir=context.directory,
                    additional_directories=context.additional_directories or None,
                    docker_in_docker=docker_in_docker,
                    computer_use=computer_use,
                    image_name=image_name,
                )

            wrapper_path = await asyncio.to_thread(_ensure_and_build_wrapper)
            logger.info(
                "Containerized context '%s': using Docker wrapper %s",
                context_name,
                wrapper_path,
            )
        cli_path = wrapper_path

    options = ClaudeAgentOptions(
        cwd=context.directory,
        model=context.model,
        allowed_tools=allowed_tools,
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
        cli_path=cli_path,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
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

    # Determine computer-use container name and screenshots dir for MCP.
    _cu_container: str | None = None
    _cu_screenshots_dir: str | None = None
    if (
        context.container is not None
        and context.container.computer_use
        and is_containerized
        and sys.platform != "darwin"
    ):
        from open_shrimp.container import _container_name
        _cu_container = _container_name(context_name)
        _cu_screenshots_dir = str(get_screenshots_dir(context_name))

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

    # Register in-process MCP tools (send_file, send_photo, etc.) so the
    # agent can send files directly to the Telegram chat.
    if bot is not None:
        openshrimp_server = create_openshrimp_mcp_server(
            bot=bot, chat_id=scope.chat_id, thread_id=scope.thread_id,
            db=db, config=config, job_queue=job_queue,
            computer_use_container=_cu_container,
            screenshots_dir=_cu_screenshots_dir,
            context_name=context_name,
            user_id=user_id,
            is_private_chat=is_private_chat,
        )
        mcp_servers: dict[str, Any] = {"openshrimp": openshrimp_server}

        # Add Playwright MCP for structured browser automation in
        # computer-use contexts.  The CLI runs inside the container,
        # so it spawns the Playwright MCP server as a child process
        # inside the container automatically.
        if _cu_container is not None:
            mcp_servers["playwright"] = {
                "command": "npx",
                "args": [
                    "@playwright/mcp",
                    "--cdp-endpoint", "http://localhost:9222",
                    "--caps", "pdf",
                ],
            }

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

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except ProcessError:
        if not session_id:
            raise
        # The session file may no longer exist (e.g. container state was
        # rebuilt, or the .jsonl was deleted).  Fall back to a fresh
        # session instead of surfacing a cryptic error.
        logger.warning(
            "Failed to resume session %s for scope %s – starting fresh",
            session_id,
            scope,
        )
        session_id = None
        options.resume = None
        client = ClaudeSDKClient(options=options)
        await client.connect()

    session = AgentSession(
        client=client,
        session_id=session_id,
        context_name=context_name,
        callback_context=callback_context,
        container_wrapper_path=wrapper_path,
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
    try:
        async with asyncio.timeout(5):
            await session.client.disconnect()
        logger.info("Closed client for scope %s", scope)
    except (Exception, TimeoutError):
        logger.debug("Error/timeout closing client for scope %s", scope, exc_info=True)
    if session.container_wrapper_path:
        if sys.platform == "darwin":
            sandbox_cleanup_wrapper(session.container_wrapper_path)
        else:
            docker_cleanup_wrapper(session.container_wrapper_path)


async def close_all_sessions() -> None:
    """Close all active sessions (for shutdown)."""
    scopes = list(_active_sessions.keys())
    for scope in scopes:
        await close_session(scope)


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


async def query_and_stream(
    session: AgentSession,
    prompt: str,
) -> AsyncIterator[AgentEvent]:
    """Send a query on an existing session and yield events."""
    logger.info("Sending query on live client: %s", prompt[:200])
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
        yield message


def _make_approval_proxy(
    ctx: CallbackContext,
) -> ApprovalCallback:
    async def _proxy(
        tool_name: str, tool_input: dict[str, Any], tool_use_id: str
    ) -> bool:
        if ctx.request_approval is None:
            logger.warning("No approval callback set, denying tool %s", tool_name)
            return False
        return await ctx.request_approval(tool_name, tool_input, tool_use_id)

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
