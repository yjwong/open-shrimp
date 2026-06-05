"""Long-lived per-scope OpenCode session runners."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from telegram import Bot
from telegram.ext import ContextTypes

from open_shrimp import agent_tasks
from open_shrimp.agent import (
    FileAttachment,
    build_prompt_with_attachments,
    cleanup_attachments,
    save_attachments,
)
from open_shrimp.client_manager import (
    AgentSession,
    CallbackContext,
    close_session,
    get_or_create_session,
    get_session,
    receive_events,
    reconnect_session,
)
from open_shrimp.config import Config, ContextConfig, is_sandboxed
from open_shrimp.db import ChatScope, get_pinned_message_id, get_session_id, set_session_id
from open_shrimp.handlers.approval import (
    _send_approval_keyboard,
    _send_auto_approved_diff,
    _send_host_bash_approval,
    flush_deferred_project_tool_permission_patches,
)
from open_shrimp.handlers.questions import _handle_questions
from open_shrimp.handlers.state import (
    _edit_approved_sessions,
    _session_approved_dirs,
    _tool_approved_sessions,
)
from open_shrimp.handlers.utils import _escape_mdv2, _get_context, _thread_kwargs, _update_pinned_status
from open_shrimp.hooks import matches_approval_rule as _matches_rule
from open_shrimp.opencode_client import CLIConnectionError, ProcessError
from open_shrimp.prompt_suggestion import schedule_prompt_suggestion, supersede_prompt_suggestion
from open_shrimp.stream import _DraftState, finalize_and_reset, TelegramTurnRenderer

logger = logging.getLogger(__name__)

RunnerStatus = Literal["starting", "idle", "submitting", "running", "stopping", "failed"]
RunnerInputSource = Literal["telegram", "setup", "agent_notification", "system"]


@dataclass
class RunnerInput:
    prompt: str
    attachments: list[FileAttachment] = field(default_factory=list)
    source: RunnerInputSource = "telegram"
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class RunnerState:
    scope: ChatScope
    context_name: str
    status: RunnerStatus = "starting"
    session: AgentSession | None = None
    current_turn_id: str | None = None
    last_activity: float = field(default_factory=time.monotonic)
    draft_state: _DraftState | None = None
    startup_buffer_depth: int = 0
    steering_submissions: int = 0
    pending_responses: int = 0


class SessionRunner:
    def __init__(
        self,
        *,
        scope: ChatScope,
        config: Config,
        db: aiosqlite.Connection,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int = 0,
        is_private_chat: bool = True,
    ) -> None:
        self.scope = scope
        self.config = config
        self.db = db
        self.context = context
        self.user_id = user_id
        self.is_private_chat = is_private_chat
        self.state = RunnerState(scope=scope, context_name="")
        self._work_available = asyncio.Event()
        self._startup_buffer: list[RunnerInput] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._submit_lock = asyncio.Lock()
        self._attachment_paths: list[Path] = []
        self._latest_todos: list[dict[str, Any]] = []
        self._suppress_prompt_suggestion = False

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def submit(self, item: RunnerInput) -> None:
        supersede_prompt_suggestion(self.scope)
        self.state.last_activity = time.monotonic()
        session = self.state.session
        if session is None or not self._ready.is_set():
            self._startup_buffer.append(item)
            self.state.startup_buffer_depth = len(self._startup_buffer)
            await self.start()
            return
        try:
            await self._submit_to_session(item)
        except (CLIConnectionError, BrokenPipeError):
            if item.source == "agent_notification":
                raise
            logger.warning(
                "Dead transport while submitting prompt for %s; restarting runner",
                self.scope,
                exc_info=True,
            )
            await self._restart_after_dead_transport(item)

    async def wake_for_notifications(self) -> None:
        """Wake the runner so queued Agent notifications are submitted."""
        self.state.last_activity = time.monotonic()
        await self.start()
        if self.state.session is not None and self._ready.is_set():
            await self._submit_parent_notifications(self.state.session)

    async def cancel_current(self) -> None:
        self._startup_buffer.clear()
        self.state.startup_buffer_depth = 0
        self.state.pending_responses = 0
        self.state.steering_submissions = 0
        self._work_available.clear()
        self._suppress_prompt_suggestion = False
        session = self.state.session or get_session(self.scope)
        if session is not None:
            try:
                await session.client.interrupt()
            except Exception:
                logger.debug("Failed to interrupt runner for %s", self.scope, exc_info=True)

    async def _restart_after_dead_transport(self, item: RunnerInput) -> None:
        """Tear down a dead client and redeliver a Telegram prompt."""
        await close_session(self.scope)
        self.state.session = None
        self.state.status = "starting"
        self.state.pending_responses = 0
        self.state.steering_submissions = 0
        self._ready.clear()
        self._work_available.clear()
        self._suppress_prompt_suggestion = False
        self._startup_buffer.insert(0, item)
        self.state.startup_buffer_depth = len(self._startup_buffer)

        current = asyncio.current_task()
        if self._task is not None and self._task is not current and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.start()

    async def stop(self) -> None:
        self.state.status = "stopping"
        self._stop.set()
        await self.cancel_current()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        cleanup_attachments(self._attachment_paths)
        self._attachment_paths.clear()
        await close_session(self.scope)
        await flush_deferred_project_tool_permission_patches(self.scope)

    def is_running_turn(self) -> bool:
        return self.state.status in {"submitting", "running"}

    async def _run(self) -> None:
        ctx_name = ""
        ctx_config: ContextConfig | None = None
        renderer: TelegramTurnRenderer | None = None
        try:
            ctx_name, ctx_config = await _get_context(self.scope, self.config, self.db)
            self.state.context_name = ctx_name
            self.state.draft_state = _DraftState(
                chat_id=self.scope.chat_id,
                thread_id=self.scope.thread_id,
                user_id=self.user_id,
                is_private_chat=self.is_private_chat,
                bot_token=self.config.telegram.token,
            )
            if not await get_pinned_message_id(self.db, self.scope):
                await _update_pinned_status(
                    self.context.bot, self.scope, ctx_name, ctx_config, self.db,
                )
            session_id = await get_session_id(self.db, self.scope, ctx_name)
            session = await self._ensure_session(ctx_name, ctx_config, session_id)
            self.state.session = session
            self._ready.set()
            renderer = self._make_renderer(ctx_config)
            await renderer.start()
            await self._flush_startup_buffer()
            await self._submit_parent_notifications(session)

            container_retries = 0
            max_container_retries = 2
            while not self._stop.is_set():
                if not self._work_available.is_set() and not await self._has_pending_notifications():
                    self.state.status = "idle"
                    try:
                        await asyncio.wait_for(self._work_available.wait(), timeout=60)
                    except TimeoutError:
                        continue

                await self._submit_parent_notifications(session)
                if not self._work_available.is_set():
                    continue

                self.state.status = "running"
                try:
                    async for event in receive_events(session):
                        result = await renderer.process_event(event)
                        if result is not None:
                            if result.session_id:
                                await set_session_id(self.db, self.scope, ctx_name, result.session_id)
                            if result.model_usage or result.turn_usage:
                                await _update_pinned_status(
                                    self.context.bot,
                                    self.scope,
                                    ctx_name,
                                    ctx_config,
                                    self.db,
                                    model_usage=result.model_usage,
                                    turn_usage=result.turn_usage,
                                    todos=self._latest_todos if self._latest_todos else None,
                                    opencode_client=session.client,
                                )
                            suppress_suggestion = self._suppress_prompt_suggestion
                            if not suppress_suggestion:
                                schedule_prompt_suggestion(
                                    bot=self.context.bot,
                                    scope=self.scope,
                                    config=self.config,
                                    client=session.client,
                                    result=result,
                                    context_name=ctx_name,
                                    context_config=ctx_config,
                                )
                            self.state.steering_submissions = 0
                            self._consume_pending_response()
                            await self._submit_parent_notifications(session)
                            break
                    container_retries = 0
                except CLIConnectionError:
                    if not is_sandboxed(ctx_config) or container_retries >= max_container_retries:
                        raise
                    container_retries += 1
                    await finalize_and_reset(self.context.bot, self.state.draft_state)
                    new_session = await reconnect_session(
                        scope=self.scope,
                        context_name=ctx_name,
                        context=ctx_config,
                        bot=self.context.bot,
                        db=self.db,
                        config=self.config,
                        job_queue=getattr(self.context, "job_queue", None),
                        terminal_base_url=self._terminal_base_url(),
                        user_id=self.user_id,
                        is_private_chat=self.is_private_chat,
                        sandbox_manager=_select_sandbox_manager(self.context.bot_data, ctx_config),
                        mcp_proxy=self.context.bot_data.get("mcp_proxy"),
                    )
                    if new_session is None:
                        raise
                    session = new_session
                    self.state.session = session
                    await self.context.bot.send_message(
                        chat_id=self.scope.chat_id,
                        text="Container restarted, resuming session\\.\\.\\.",
                        parse_mode="MarkdownV2",
                        **_thread_kwargs(self.scope),
                    )
        except asyncio.CancelledError:
            raise
        except CLIConnectionError:
            logger.exception("Session runner transport failed for scope %s", self.scope)
            self.state.status = "failed"
            await close_session(self.scope)
            self.state.session = None
            self._ready.clear()
            await self._send_failure(ctx_config)
        except ProcessError as exc:
            logger.exception("Session runner OpenCode error for scope %s", self.scope)
            self.state.status = "failed"
            await close_session(self.scope)
            self.state.session = None
            self._ready.clear()
            await self._send_opencode_error(exc)
        except Exception:
            logger.exception("Session runner failed for scope %s", self.scope)
            self.state.status = "failed"
            self.state.session = None
            self._ready.clear()
            try:
                await self.context.bot.send_message(
                    chat_id=self.scope.chat_id,
                    text="An error occurred while processing your request\\.",
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(self.scope),
                )
            except Exception:
                logger.exception("Failed to send runner error message")
        finally:
            if renderer is not None:
                await renderer.close()
            if self.state.draft_state and self.state.draft_state.session_id and ctx_name:
                try:
                    await set_session_id(
                        self.db, self.scope, ctx_name, self.state.draft_state.session_id,
                    )
                except Exception:
                    logger.debug("Failed to save session during runner cleanup", exc_info=True)
            cleanup_attachments(self._attachment_paths)
            self._attachment_paths.clear()
            await flush_deferred_project_tool_permission_patches(self.scope)
            if self._stop.is_set():
                self.state.status = "stopping"
            elif self.state.status != "failed":
                self.state.status = "idle"

    async def _ensure_session(
        self, ctx_name: str, ctx_config: ContextConfig, session_id: str | None,
    ) -> AgentSession:
        draft_state = self.state.draft_state
        assert draft_state is not None
        cb_ctx = CallbackContext(
            request_approval=self._make_request_approval(ctx_name, ctx_config, draft_state),
            handle_questions=lambda questions: _handle_questions(
                self.context.bot, self.scope, questions, draft_state,
            ),
            is_edit_auto_approved=lambda: (self.scope, ctx_name) in _edit_approved_sessions,
            notify_auto_approved_edit=self._make_notify_edit(ctx_config, draft_state),
            is_tool_auto_approved=lambda tn, ti: any(
                _matches_rule(rule, tn, ti)
                for rule in _tool_approved_sessions.get((self.scope, ctx_name), [])
            ),
            get_session_approved_dirs=lambda: list(
                _session_approved_dirs.get((self.scope, ctx_name), set())
            ),
            request_host_bash_approval=self._make_host_bash_approval(ctx_name, draft_state),
        )
        return await get_or_create_session(
            scope=self.scope,
            context_name=ctx_name,
            context=ctx_config,
            session_id=session_id,
            callback_context=cb_ctx,
            bot=self.context.bot,
            db=self.db,
            config=self.config,
            job_queue=getattr(self.context, "job_queue", None),
            terminal_base_url=self._terminal_base_url(),
            user_id=self.user_id,
            is_private_chat=self.is_private_chat,
            sandbox_manager=_select_sandbox_manager(self.context.bot_data, ctx_config),
            mcp_proxy=self.context.bot_data.get("mcp_proxy"),
        )

    def _make_request_approval(
        self, ctx_name: str, ctx_config: ContextConfig, draft_state: _DraftState,
    ):
        async def request_approval(
            tool_name: str,
            tool_input: dict[str, Any],
            tool_use_id: str,
            suggested_session_dir: str | None = None,
            always_patterns: list[str] | None = None,
        ):
            await finalize_and_reset(self.context.bot, draft_state)
            return await _send_approval_keyboard(
                self.context.bot,
                self.scope.chat_id,
                tool_name,
                tool_input,
                tool_use_id,
                cwd=ctx_config.directory,
                thread_id=self.scope.thread_id,
                base_url=self._terminal_base_url(),
                user_id=self.user_id,
                is_private_chat=self.is_private_chat,
                bot_token=self.config.telegram.token,
                suggested_session_dir=suggested_session_dir,
                scope=self.scope,
                context_name=ctx_name,
                always_patterns=list(always_patterns or []),
            )
        return request_approval

    def _make_notify_edit(self, ctx_config: ContextConfig, draft_state: _DraftState):
        async def notify_edit(tool_name: str, tool_input: dict[str, Any]) -> None:
            await finalize_and_reset(self.context.bot, draft_state)
            await _send_auto_approved_diff(
                self.context.bot,
                self.scope.chat_id,
                tool_name,
                tool_input,
                cwd=ctx_config.directory,
                thread_id=self.scope.thread_id,
            )
        return notify_edit

    def _make_host_bash_approval(self, ctx_name: str, draft_state: _DraftState):
        async def request_host_bash(tool_input: dict[str, Any], tool_use_id: str) -> Any:
            await finalize_and_reset(self.context.bot, draft_state)
            return await _send_host_bash_approval(
                bot=self.context.bot,
                chat_id=self.scope.chat_id,
                context_name=ctx_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                thread_id=self.scope.thread_id,
            )
        return request_host_bash

    def _make_renderer(self, ctx_config: ContextConfig) -> TelegramTurnRenderer:
        async def on_todo_update(todos: list[dict[str, Any]]) -> None:
            self._latest_todos.clear()
            self._latest_todos.extend(todos)
            await _update_pinned_status(
                self.context.bot,
                self.scope,
                self.state.context_name,
                ctx_config,
                self.db,
                todos=todos if todos else None,
            )

        return TelegramTurnRenderer(
            bot=self.context.bot,
            chat_id=self.scope.chat_id,
            draft_state=self.state.draft_state,
            allowed_tools=ctx_config.allowed_tools,
            cwd=ctx_config.directory,
            on_todo_update=on_todo_update,
            terminal_base_url=self._terminal_base_url(),
            scope=self.scope,
        )

    async def _flush_startup_buffer(self) -> None:
        submitted = 0
        while self._startup_buffer:
            item = self._startup_buffer.pop(0)
            self.state.startup_buffer_depth = len(self._startup_buffer)
            submitted += 1
            await self._submit_to_session(
                item,
                expect_followup=item.source == "telegram" and submitted > 1,
            )

    async def _submit_to_session(
        self,
        item: RunnerInput,
        *,
        expect_followup: bool = False,
    ) -> None:
        session = self.state.session
        if session is None:
            self._startup_buffer.append(item)
            self.state.startup_buffer_depth = len(self._startup_buffer)
            return
        async with self._submit_lock:
            was_running = self.state.status == "running"
            self.state.status = "submitting"
            actual_prompt = await self._prepare_prompt(session, item)
            await session.client.query(actual_prompt)
            session.last_activity = time.monotonic()
            if was_running or expect_followup:
                self.state.steering_submissions += 1
                self.state.pending_responses += 1
                self._suppress_prompt_suggestion = True
                if was_running:
                    self.state.status = "running"
            elif item.source != "telegram":
                self.state.steering_submissions += 1
                self._suppress_prompt_suggestion = True
            self._work_available.set()
            self.state.last_activity = time.monotonic()

    def _consume_pending_response(self) -> bool:
        """Consume one expected follow-up response after a ResultMessage."""
        if self.state.pending_responses <= 0:
            self._work_available.clear()
            self._suppress_prompt_suggestion = False
            return False
        self.state.pending_responses -= 1
        if self.state.pending_responses == 0:
            self._suppress_prompt_suggestion = False
        return True

    async def _prepare_prompt(self, session: AgentSession, item: RunnerInput) -> str:
        if not item.attachments:
            return item.prompt
        attachment_paths = save_attachments(item.attachments, self.scope.chat_id)
        self._attachment_paths.extend(attachment_paths)
        prompt_paths = attachment_paths
        if session.sandbox is not None:
            prompt_paths = await session.sandbox.copy_files_in(attachment_paths)
        return build_prompt_with_attachments(item.prompt, prompt_paths)

    async def _has_pending_notifications(self) -> bool:
        session = self.state.session
        sid = session.session_id if session else None
        return bool(sid and agent_tasks.has_parent_notifications(sid))

    async def _submit_parent_notifications(self, session: AgentSession) -> None:
        if not session.session_id:
            return
        async def submit(payload: str) -> None:
            await self.submit(RunnerInput(payload, source="agent_notification"))

        await agent_tasks.submit_parent_notifications(session.session_id, submit)

    def _terminal_base_url(self) -> str | None:
        if self.config.review.public_url:
            return self.config.review.public_url.rstrip("/")
        return f"https://{self.config.review.host}:{self.config.review.port}"

    async def _send_failure(self, ctx_config: ContextConfig | None) -> None:
        try:
            if ctx_config is not None and is_sandboxed(ctx_config):
                text = (
                    "The sandbox process terminated unexpectedly "
                    "\\(possibly due to a VM shutdown\\)\\. "
                    "Send a new message to restart the session\\."
                )
            else:
                text = (
                    "The OpenCode session terminated unexpectedly\\. "
                    "Send a new message to restart the session\\."
                )
            await self.context.bot.send_message(
                chat_id=self.scope.chat_id,
                text=text,
                parse_mode="MarkdownV2",
                **_thread_kwargs(self.scope),
            )
        except Exception:
            logger.exception("Failed to send runner failure message")

    async def _send_opencode_error(self, exc: ProcessError) -> None:
        try:
            message = str(exc) or "Unknown OpenCode error"
            if len(message) > 1200:
                message = message[:1197] + "..."
            await self.context.bot.send_message(
                chat_id=self.scope.chat_id,
                text="OpenCode returned an error:\n" + _escape_mdv2(message),
                parse_mode="MarkdownV2",
                **_thread_kwargs(self.scope),
            )
        except Exception:
            logger.exception("Failed to send OpenCode error")


_session_runners: dict[ChatScope, SessionRunner] = {}
_runner_locks: dict[ChatScope, asyncio.Lock] = {}


async def get_or_start_runner(
    *,
    scope: ChatScope,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int = 0,
    is_private_chat: bool = True,
) -> SessionRunner:
    lock = _runner_locks.setdefault(scope, asyncio.Lock())
    async with lock:
        runner = _session_runners.get(scope)
        if runner is None or runner.state.status in {"failed", "stopping"}:
            runner = SessionRunner(
                scope=scope,
                config=config,
                db=db,
                context=context,
                user_id=user_id,
                is_private_chat=is_private_chat,
            )
            _session_runners[scope] = runner
            await runner.start()
        return runner


def get_runner(scope: ChatScope) -> SessionRunner | None:
    return _session_runners.get(scope)


async def stop_runner(scope: ChatScope) -> None:
    runner = _session_runners.pop(scope, None)
    if runner is not None:
        await runner.stop()


async def stop_all_runners() -> None:
    scopes = list(_session_runners)
    await asyncio.gather(*(stop_runner(scope) for scope in scopes), return_exceptions=True)


def runner_status(scope: ChatScope) -> RunnerState | None:
    runner = get_runner(scope)
    return runner.state if runner is not None else None


def _select_sandbox_manager(
    bot_data: dict[str, Any],
    ctx_config: ContextConfig,
) -> "Any | None":
    managers = bot_data.get("sandbox_managers")
    if managers and ctx_config.sandbox:
        return managers.get(ctx_config.sandbox.backend)
    return None
