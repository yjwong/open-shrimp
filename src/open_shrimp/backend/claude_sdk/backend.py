"""``ClaudeSdkBackend`` — the Claude Agent SDK as a ``Backend``.

Each method is thin:

* ``make_client`` constructs (does not connect) a ``ClaudeSdkClient``.
* ``make_tool_server`` returns the shared HTTP-bridge installer (the selector
  form — ``client_manager`` supplies the proxy + scope args).
* ``make_can_use_tool`` delegates to ``hooks.make_can_use_tool``.
* ``list_sessions`` wraps ``claude_agent_sdk.list_sessions`` and re-packs its
  rows into ``backend.SessionInfo`` (field-for-field).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_shrimp.backend.claude_sdk.client import ClaudeSdkClient
from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.protocol import (
    AuthCopy,
    BackendOptions,
    CanUseTool,
    MCPConfigProvider,
    MCPOAuthProvider,
    ToolFactory,
)
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.tools import serve_tools_over_mcp_http

if TYPE_CHECKING:
    from open_shrimp.sandbox.agent_runtime import AgentRuntime


class ClaudeSdkBackend:
    """The Claude Agent SDK backend.  ``name == "claude_sdk"``."""

    name = "claude_sdk"

    policy: ClaudeSdkPolicy = ClaudeSdkPolicy()

    def __init__(self) -> None:
        # SDK monkey-patches are applied here so they only run when this
        # backend is actually constructed — multi-backend installs that
        # never instantiate ``ClaudeSdkBackend`` never import the SDK.
        # Both calls are idempotent; order matters because
        # ``prompt_suggestion`` overrides ``read_messages`` on the class
        # ``sdk_patches`` subclasses.
        from open_shrimp.prompt_suggestion import (
            install_patches as install_suggestion_patches,
        )
        from open_shrimp.sdk_patches import apply as apply_sdk_patches

        apply_sdk_patches()
        install_suggestion_patches()

        self._mcp_config_provider: MCPConfigProvider | None = None
        self._mcp_oauth_provider: MCPOAuthProvider | None = None

    def make_client(self, options: BackendOptions) -> ClaudeSdkClient:
        return ClaudeSdkClient(options)

    def make_runtime(
        self,
        home_dir: Path,
        *,
        context_name: str,
        model: str | None = None,
    ) -> "AgentRuntime":
        """The Claude wrapped-CLI launch profile.

        ``context_name`` and ``model`` are unused — the wrapped-CLI runtime
        needs only the host-side home dir.
        """
        from open_shrimp.sandbox.agent_runtime import claude_runtime

        return claude_runtime(home_dir)

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

        home_mount = None
        if (
            ctx is not None
            and ctx_name is not None
            and sandbox_managers
            and ctx.sandbox is not None
            and ctx.sandbox.enabled
        ):
            from open_shrimp.sandbox.agent_runtime import claude_runtime

            mgr = sandbox_managers.get(ctx.sandbox.backend)
            if mgr is not None:
                home_mount = claude_runtime(mgr.agent_home_dir(ctx_name)).home_mount

        if home_mount is not None and home_mount.holds_session_state:
            from claude_agent_sdk._internal.sessions import (
                MAX_SANITIZED_LENGTH,
                _apply_sort_limit_offset,
                _canonicalize_path,
                _read_sessions_from_dir,
                _sanitize_path,
            )

            projects_dir = home_mount.host_dir / "projects"
            canonical = _canonicalize_path(str(directory))
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
            rows = _apply_sort_limit_offset(sessions, limit, offset)
            return [_to_session_info(r) for r in rows]

        from claude_agent_sdk import list_sessions

        rows = list_sessions(directory=directory, limit=limit, **kwargs)
        return [_to_session_info(r) for r in rows]

    def command_capabilities(self) -> set[str]:
        return {"login", "usage", "mcp"}

    def auth_copy(self) -> AuthCopy:
        return AuthCopy(
            login_command_description="Re-authenticate Claude Code OAuth",
            login_mini_app_body="Re-authenticate Claude Code OAuth",
            auth_error_hint="Run /login to re-authenticate Claude Code.",
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
