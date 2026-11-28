"""``ClaudeSdkBackend`` — the Claude Agent SDK as a ``Backend``.

Each method is thin:

* ``make_client`` constructs (does not connect) a ``ClaudeSdkClient``.
* ``make_tool_server`` returns the shared HTTP-bridge installer (the selector
  form — ``client_manager`` supplies the proxy + scope args).
* ``make_can_use_tool`` delegates to ``hooks.make_can_use_tool``.
* ``list_sessions`` wraps ``claude_agent_sdk.list_sessions`` and re-packs its
  rows into ``backend.SessionInfo`` (field-for-field).
* ``fork_session`` runs the SDK's fork transform over a transcript it locates
  itself, so a sandboxed context's claude-home works without touching
  ``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_shrimp.backend.claude_sdk.client import ClaudeSdkClient
from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.claude_sdk.task_checklist import read_checklist
from open_shrimp.backend.claude_sdk import models
from open_shrimp.backend.protocol import (
    BackendClient,
    BackendCopy,
    BackendOptions,
    CanUseTool,
    ChecklistReader,
    MCPConfigProvider,
    MCPOAuthProvider,
    ModelChoice,
    ToolFactory,
)
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.tools import serve_tools_over_mcp_http
from open_shrimp.backend.types import ForkResult, SessionTranscript
from open_shrimp.backend.usage import UsageReport

if TYPE_CHECKING:
    from telegram import Bot

    from open_shrimp.config import Config, ContextConfig
    from open_shrimp.db import ChatScope
    from open_shrimp.sandbox.agent_runtime import AgentRuntime, HomeMount
    from open_shrimp.stream import StreamResult

logger = logging.getLogger(__name__)


def _sandbox_home_mount(
    ctx: "ContextConfig | None",
    ctx_name: str | None,
    sandbox_managers: Any | None,
) -> "HomeMount | None":
    """The per-context claude-home mount for a sandboxed context, else None.

    Sandboxed contexts keep their session state (and the CLI's task store)
    under the host-side directory the sandbox mounts as the guest's
    ``~/.claude`` — not the host's own ``~/.claude``.
    """
    if (
        ctx is None
        or ctx_name is None
        or not sandbox_managers
        or ctx.sandbox is None
        or not ctx.sandbox.enabled
    ):
        return None
    from open_shrimp.backend.claude_sdk.runtime import (
        claude_home_dir,
        claude_runtime,
    )

    mgr = sandbox_managers.get(ctx.sandbox.backend)
    if mgr is None:
        return None
    home_dir = claude_home_dir(ctx_name)
    return claude_runtime(home_dir).home_mount


def _sandbox_project_dir(
    home_mount: "HomeMount", directory: str | Path,
) -> Path | None:
    """The claude-home ``projects/<sanitized cwd>`` dir for *directory*.

    The SDK resolves this under ``CLAUDE_CONFIG_DIR``, which is process-global,
    so a sandboxed context's per-context home has to be resolved by hand.
    Paths whose sanitized form exceeds the SDK's limit carry a hash suffix
    that may not match ours; those fall back to a prefix scan.
    """
    from claude_agent_sdk._internal.sessions import (
        MAX_SANITIZED_LENGTH,
        _canonicalize_path,
        _sanitize_path,
    )

    projects_dir = home_mount.host_dir / "projects"
    sanitized = _sanitize_path(_canonicalize_path(str(directory)))
    candidate = projects_dir / sanitized
    if candidate.is_dir():
        return candidate
    if len(sanitized) > MAX_SANITIZED_LENGTH:
        prefix = sanitized[:MAX_SANITIZED_LENGTH]
        try:
            for entry in projects_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(prefix + "-"):
                    return entry
        except OSError:
            pass
    return None


def _session_file(
    session_id: str,
    directory: str | Path,
    home_mount: "HomeMount | None",
) -> Path:
    """Locate *session_id*'s transcript, host or sandboxed.

    Raises ``FileNotFoundError`` when there is none.
    """
    if home_mount is not None and home_mount.holds_session_state:
        project_dir = _sandbox_project_dir(home_mount, directory)
        path = (
            project_dir / f"{session_id}.jsonl"
            if project_dir is not None
            else None
        )
        if path is None or not path.is_file():
            raise FileNotFoundError(f"Session {session_id} not found")
        return path

    from claude_agent_sdk._internal.session_mutations import (
        _find_session_file_with_dir,
    )

    found = _find_session_file_with_dir(session_id, str(directory))
    if found is None:
        raise FileNotFoundError(f"Session {session_id} not found")
    return found[0]


def _text_of(content: Any) -> str | None:
    """The text of a transcript ``message.content``, or None.

    ``None`` too for content holding a ``tool_result`` block: that entry is
    the CLI reporting a tool's output, not something the user typed.
    """
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n\n".join(texts).strip() or None


def _last_exchange(
    transcript: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """The last typed user prompt and the last assistant text after it.

    A turn that answered only with tool calls has no reply; text from an
    earlier turn is not borrowed for it.
    """
    reply: str | None = None
    for entry in reversed(transcript):
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        kind = entry.get("type")
        if kind == "assistant" and reply is None:
            reply = _text_of(message.get("content"))
        elif kind == "user":
            prompt = _text_of(message.get("content"))
            if prompt is not None:
                return prompt, reply
    return None, reply


def _fork_transcript(
    path: Path, session_id: str, at_message_id: str | None,
) -> ForkResult:
    """Write a fork of the transcript at *path* beside it.

    The CLI appends to *path* while the source turn runs, so the file is
    read once and the fork, the summary and the echo all come from those
    bytes.  Uses the SDK's own transform, so the copy is what
    ``claude_agent_sdk.fork_session`` would have written, minus the
    ``CLAUDE_CONFIG_DIR`` lookup a sandboxed context cannot use.
    """
    import os

    from claude_agent_sdk._internal.session_mutations import (
        _build_fork_lines,
        _parse_fork_transcript,
    )
    from claude_agent_sdk._internal.sessions import (
        _jsonl_to_lite,
        _parse_session_info_from_lite,
    )

    content = path.read_bytes()
    if not content:
        raise ValueError(f"Session {session_id} has no messages to fork")

    summary: str | None = None
    try:
        info = _parse_session_info_from_lite(
            session_id,
            _jsonl_to_lite(content.decode("utf-8", errors="replace"), 0),
        )
        summary = info.summary if info is not None else None
    except Exception:
        logger.debug("Could not read a summary for %s", session_id, exc_info=True)

    transcript, replacements = _parse_fork_transcript(content, session_id)
    forked_id, lines = _build_fork_lines(
        transcript,
        replacements,
        session_id,
        at_message_id,
        f"{summary} (fork)" if summary else None,
        lambda: None,
    )

    kept = transcript
    if at_message_id is not None:
        for i, entry in enumerate(transcript):
            if entry.get("uuid") == at_message_id:
                kept = transcript[: i + 1]
                break
    try:
        last_prompt, last_reply = _last_exchange(kept)
    except Exception:
        logger.debug("Could not read the last exchange of %s", session_id, exc_info=True)
        last_prompt = last_reply = None

    fork_path = path.parent / f"{forked_id}.jsonl"
    fd = os.open(fork_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)

    return ForkResult(
        session_id=forked_id,
        summary=summary,
        last_prompt=last_prompt,
        last_reply=last_reply,
    )


class ClaudeSdkBackend:
    """The Claude Agent SDK backend.  ``name == "claude_sdk"``."""

    name = "claude_sdk"
    display_name = "Claude Code"

    policy: ClaudeSdkPolicy = ClaudeSdkPolicy()

    def __init__(self) -> None:
        # SDK monkey-patches are applied here so they only run when this
        # backend is actually constructed — multi-backend installs that
        # never instantiate ``ClaudeSdkBackend`` never import the SDK.
        # Idempotent.
        import warnings

        from claude_agent_sdk import CanUseToolShadowedWarning

        from open_shrimp.backend.claude_sdk.prompt_suggestion import (
            install_patches as install_suggestion_patches,
        )

        # OpenShrimp deliberately whole-tool-allows its read-only and MCP
        # tools (Read/Glob/Grep, Task*, computer_*, etc.) so they bypass the
        # can_use_tool approval callback; that same callback still gates the
        # mutating tools. The SDK's advisory shadowing warning is expected
        # here and would only be log noise, so silence it.
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

        install_suggestion_patches()

        self._mcp_config_provider: MCPConfigProvider | None = None
        self._mcp_oauth_provider: MCPOAuthProvider | None = None

    def make_client(self, options: BackendOptions) -> ClaudeSdkClient:
        return ClaudeSdkClient(options)

    def make_runtime(
        self,
        *,
        context_name: str,
        model: str | None = None,
    ) -> "AgentRuntime":
        """The Claude wrapped-CLI launch profile.

        ``model`` is unused — the wrapped-CLI runtime needs only the host-side
        home dir, which ``context_name`` locates.
        """
        from open_shrimp.backend.claude_sdk.runtime import (
            claude_home_dir,
            claude_runtime,
        )

        return claude_runtime(claude_home_dir(context_name))

    def make_tool_server(
        self, tools: ToolFactory
    ) -> Callable[..., dict[str, Any]]:
        """Select the installer for the OpenShrimp tool surface."""
        return serve_tools_over_mcp_http

    def make_can_use_tool(
        self,
        request_approval: Any,
        cwd: str,
        **kwargs: Any,
    ) -> CanUseTool:
        from open_shrimp.hooks import make_can_use_tool

        return make_can_use_tool(
            request_approval=request_approval,
            cwd=cwd,
            **kwargs,
        )

    async def list_sessions(
        self,
        directory: str | Path,
        *,
        limit: int = 500,
        **kwargs: Any,
    ) -> list[SessionInfo]:
        """Return the SDK's sessions for ``directory`` as ``SessionInfo`` rows.

        The SDK's ``SDKSessionInfo`` is field-for-field ``SessionInfo``, so
        this is a shallow re-pack.

        Two paths:

        * **Non-sandboxed (default):** ``claude_agent_sdk.list_sessions`` scans
          the host ``~/.claude/projects``.
        * **Sandboxed:** the session corpus lives under the per-context
          claude-home directory (mapped as ``~/.claude`` inside the sandbox),
          not the host's ``~/.claude``.  We scan that directory directly using
          the SDK's ``_internal.sessions`` helpers to avoid mutating global
          process state (``CLAUDE_CONFIG_DIR``).  The caller routes us into
          this branch by passing ``ctx_name`` + ``sandbox_managers`` (plus the
          ``ContextConfig`` ``ctx`` so we can detect sandboxing without a
          second import in the call site).
        """
        ctx = kwargs.pop("ctx", None)
        ctx_name = kwargs.pop("ctx_name", None)
        sandbox_managers = kwargs.pop("sandbox_managers", None)
        offset = kwargs.pop("offset", 0)

        home_mount = _sandbox_home_mount(ctx, ctx_name, sandbox_managers)

        if home_mount is not None and home_mount.holds_session_state:
            from claude_agent_sdk._internal.sessions import (
                _apply_sort_limit_offset,
                _canonicalize_path,
                _read_sessions_from_dir,
            )

            project_dir = _sandbox_project_dir(home_mount, directory)
            if project_dir is None:
                return []
            sessions = _read_sessions_from_dir(
                project_dir, _canonicalize_path(str(directory)),
            )
            rows = _apply_sort_limit_offset(sessions, limit, offset)
            return [_to_session_info(r) for r in rows]

        from claude_agent_sdk import list_sessions

        rows = list_sessions(directory=directory, limit=limit, **kwargs)
        return [_to_session_info(r) for r in rows]

    async def fork_session(
        self,
        session_id: str,
        directory: str | Path,
        *,
        at_message_id: str | None,
        **kwargs: Any,
    ) -> ForkResult:
        """Fork offline: transcripts are host files in both modes.

        A sandboxed context's live in the per-context claude-home, which is
        the guest's ``~/.claude``, so no guest round-trip is needed.
        """
        home_mount = _sandbox_home_mount(
            kwargs.get("ctx"), kwargs.get("ctx_name"),
            kwargs.get("sandbox_managers"),
        )

        def run() -> ForkResult:
            path = _session_file(session_id, directory, home_mount)
            return _fork_transcript(path, session_id, at_message_id)

        return await asyncio.to_thread(run)

    async def delete_session(
        self,
        session_id: str,
        directory: str | Path,
        **kwargs: Any,
    ) -> None:
        home_mount = _sandbox_home_mount(
            kwargs.get("ctx"), kwargs.get("ctx_name"),
            kwargs.get("sandbox_managers"),
        )

        def run() -> None:
            _session_file(session_id, directory, home_mount).unlink()

        await asyncio.to_thread(run)

    async def export_session(
        self,
        session_id: str,
        directory: str | Path,
        **kwargs: Any,
    ) -> SessionTranscript:
        """Read offline, as :meth:`fork_session` does (see ``export.py``)."""
        from open_shrimp.backend.claude_sdk.export import export_transcript

        home_mount = _sandbox_home_mount(
            kwargs.get("ctx"), kwargs.get("ctx_name"),
            kwargs.get("sandbox_managers"),
        )

        def run() -> SessionTranscript:
            path = _session_file(session_id, directory, home_mount)
            return export_transcript(path, session_id)

        return await asyncio.to_thread(run)

    def host_prefetch(self) -> None:
        """Nothing: the CLI arrives as a native ``_bundled/claude`` inside the
        pinned ``claude-agent-sdk`` wheel, so ``uv sync`` has already paid for
        it."""
        return None

    def checklist_reader(
        self,
        *,
        ctx: "ContextConfig",
        ctx_name: str,
        **kwargs: Any,
    ) -> ChecklistReader | None:
        """An async ``session_id -> checklist`` reader over the CLI's
        on-disk task store (``<claude-home>/tasks/<session-id>/``).

        Resolves the claude-home the same way :meth:`list_sessions` does:
        sandboxed contexts (``sandbox_managers`` in ``kwargs``) read the
        per-context home the sandbox mounts as the guest's ``~/.claude``;
        host contexts use the SDK's own resolution (``CLAUDE_CONFIG_DIR``,
        falling back to ``~/.claude``).
        """
        home_mount = _sandbox_home_mount(
            ctx, ctx_name, kwargs.get("sandbox_managers"),
        )
        if home_mount is not None and home_mount.holds_session_state:
            claude_home = home_mount.host_dir
        else:
            from claude_agent_sdk._internal.sessions import (
                _get_claude_config_home_dir,
            )

            claude_home = _get_claude_config_home_dir()

        async def read(session_id: str) -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                read_checklist, claude_home, session_id,
            )

        return read

    def command_capabilities(self) -> set[str]:
        return {"login", "usage", "mcp", "fork", "share"}

    def normalize_model(self, model: str | None) -> str | None:
        return models.normalize_model(model)

    def model_catalog(self) -> list[ModelChoice]:
        return list(models.MODEL_CHOICES)

    def is_known_model(self, model: str) -> bool:
        return models.is_known_model(model)

    def copy(self) -> BackendCopy:
        return BackendCopy(
            login_command_description="Re-authenticate Claude Code OAuth",
            login_mini_app_body="Re-authenticate Claude Code OAuth",
            auth_error_hint="Run /login to re-authenticate Claude Code.",
            assistant_error_messages={
                "authentication_failed": (
                    "⚠️ **Authentication failed.** Claude was unable to "
                    "authenticate. Check that your API key or OAuth session "
                    "is valid. Run /login to re-authenticate Claude Code."
                ),
                "billing_error": (
                    "⚠️ **Billing error.** There is a problem with your "
                    "Anthropic account billing. Please check your account "
                    "at console.anthropic.com."
                ),
                "invalid_request": (
                    "⚠️ **Invalid request.** The request to Claude was "
                    "rejected. This may indicate a configuration issue."
                ),
                "server_error": (
                    "⚠️ **Server error.** Anthropic's servers returned an "
                    "error. Please try again shortly."
                ),
            },
        )

    def mcp_config_source(self) -> MCPConfigProvider:
        if self._mcp_config_provider is None:
            from open_shrimp.backend.claude_sdk.mcp_config import (
                ClaudeMcpConfigProvider,
            )

            self._mcp_config_provider = ClaudeMcpConfigProvider()
        return self._mcp_config_provider

    def mcp_oauth_source(self) -> MCPOAuthProvider:
        if self._mcp_oauth_provider is None:
            from open_shrimp.backend.claude_sdk.mcp_config import (
                ClaudeMcpOAuthProvider,
            )

            self._mcp_oauth_provider = ClaudeMcpOAuthProvider()
        return self._mcp_oauth_provider

    async def usage(self) -> UsageReport | None:
        from open_shrimp.backend.claude_sdk.usage import fetch

        return await fetch()

    async def on_turn_end(
        self,
        *,
        bot: "Bot",
        scope: "ChatScope",
        client: BackendClient,
        result: "StreamResult",
        config: "Config",
        context_name: str,
        context_config: "ContextConfig",
    ) -> None:
        """Register a per-turn ``prompt_suggestion`` handler.

        The CLI emits its suggestion frame asynchronously after the
        result; the registered closure is fired by the SDK transport
        patch in ``prompt_suggestion.install_patches`` and edits the
        last finalized message to attach a "💡 <suggestion>" inline
        button.
        """
        if not (result.session_id and result.sent_message_ids):
            return
        from open_shrimp.backend.claude_sdk.prompt_suggestion import (
            register_handler_for_turn,
        )

        register_handler_for_turn(
            bot=bot,
            chat_id=scope.chat_id,
            message_id=result.sent_message_ids[-1],
            session_id=result.session_id,
        )


def _to_session_info(row: Any) -> SessionInfo:
    """Re-pack an SDK ``SDKSessionInfo`` (or an already-``SessionInfo``) row."""
    if isinstance(row, SessionInfo):
        return row
    return SessionInfo(
        session_id=row.session_id,
        summary=row.summary,
        last_modified=row.last_modified,
        created_at=getattr(row, "created_at", None),
        custom_title=getattr(row, "custom_title", None),
        first_prompt=getattr(row, "first_prompt", None),
        git_branch=getattr(row, "git_branch", None),
        file_size=getattr(row, "file_size", None),
        cwd=getattr(row, "cwd", None),
        tag=getattr(row, "tag", None),
    )


__all__ = ["ClaudeSdkBackend", "_to_session_info"]
