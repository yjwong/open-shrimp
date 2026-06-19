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
from open_shrimp.backend.protocol import (
    BackendOptions,
    CanUseTool,
    ToolFactory,
)
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.tools import serve_tools_over_mcp_http

if TYPE_CHECKING:
    from open_shrimp.sandbox.agent_runtime import AgentRuntime


class ClaudeSdkBackend:
    """The Claude Agent SDK backend.  ``name == "claude_sdk"``."""

    name = "claude_sdk"

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
        this is a shallow re-pack.  The sandboxed-directory scan and the SDK
        ``_internal.sessions`` helpers stay in ``handlers/commands.py``'s
        ``_list_sessions_for_context``; this method is the non-sandboxed path.
        """
        from claude_agent_sdk import list_sessions

        rows = list_sessions(directory=directory, limit=limit, **kwargs)
        return [_to_session_info(r) for r in rows]


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
