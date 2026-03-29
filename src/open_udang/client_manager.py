"""Persistent Claude Agent SDK client manager for OpenUdang.

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

from telegram import Bot

from open_udang.agent import AgentEvent
from open_udang.config import ContextConfig
from open_udang.db import ChatScope
from open_udang.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    QuestionCallback,
)
import sys

from open_udang.container import (
    CONTAINER_IMAGE,
    build_cli_wrapper as docker_build_cli_wrapper,
    cleanup_wrapper as docker_cleanup_wrapper,
    ensure_container_running as docker_ensure_container,
    ensure_image as docker_ensure_image,
)
from open_udang.sandbox import (
    build_cli_wrapper as sandbox_build_cli_wrapper,
    cleanup_wrapper as sandbox_cleanup_wrapper,
)
from open_udang.tools import create_openudang_mcp_server

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

    from open_udang.hooks import make_can_use_tool

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

    def _log_stderr(line: str) -> None:
        logger.info("CLI stderr: %s", line.rstrip())

    # Auto-approve the built-in OpenUdang MCP tools (send_file, send_photo)
    # alongside whatever the user configured.
    allowed_tools = list(context.allowed_tools or [])
    allowed_tools.append("mcp__openudang__send_file")
    if scope.thread_id is not None:
        allowed_tools.append("mcp__openudang__edit_topic")
    # Auto-approve scheduling tools when available.
    if db is not None and config is not None and job_queue is not None:
        allowed_tools.extend([
            "mcp__openudang__create_schedule",
            "mcp__openudang__list_schedules",
            "mcp__openudang__delete_schedule",
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
            image_name = (
                f"openudang-claude:{context_name}"
                if custom_dockerfile
                else CONTAINER_IMAGE
            )

            import subprocess as _subprocess
            inspect_result = _subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
            )
            if inspect_result.returncode != 0 and bot is not None:
                await bot.send_message(
                    chat_id=scope.chat_id,
                    message_thread_id=scope.thread_id,
                    text=(
                        "Building container image for the first time, "
                        "this may take a few minutes\\.\\.\\."
                    ),
                    parse_mode="MarkdownV2",
                )

            def _ensure_and_build_wrapper() -> str:
                docker_ensure_image(
                    image_name=image_name,
                    dockerfile=custom_dockerfile,
                )
                docker_ensure_container(
                    context_name=context_name,
                    project_dir=context.directory,
                    additional_directories=context.additional_directories or None,
                    docker_in_docker=docker_in_docker,
                    image_name=image_name,
                )
                return docker_build_cli_wrapper(
                    context_name=context_name,
                    project_dir=context.directory,
                    additional_directories=context.additional_directories or None,
                    docker_in_docker=docker_in_docker,
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

    if system_prompt_parts:
        options.system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(system_prompt_parts),
        }

    # Register in-process MCP tools (send_file, send_photo, etc.) so the
    # agent can send files directly to the Telegram chat.
    if bot is not None:
        openudang_server = create_openudang_mcp_server(
            bot=bot, chat_id=scope.chat_id, thread_id=scope.thread_id,
            db=db, config=config, job_queue=job_queue,
        )
        options.mcp_servers = {"openudang": openudang_server}

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
