"""``OpenCodeBackend`` — the OpenCode control plane as a ``Backend``.

* ``make_client`` constructs (does not connect) an ``OpenCodeClient`` from the
  backend-neutral ``BackendOptions`` (it translates internally via
  ``to_opencode``).
* ``make_tool_server`` returns the shared HTTP-bridge installer
  (``serve_tools_over_mcp_http``) — OpenCode speaks the same
  ``tools/list``/``tools/call`` MCP protocol, so no new tool code is added.
* ``make_can_use_tool`` delegates to ``hooks.make_can_use_tool``; the neutral
  permission results it returns are consumed directly by the OpenCode
  ``PermissionBridge``.
* ``list_sessions`` lists over HTTP via ``GET /session`` and returns
  ``backend.SessionInfo`` rows.  This is the non-sandboxed path (against the
  host-local ``opencode serve``); the sandboxed-resume listing — which must
  boot the sandbox and query its server — lives in
  ``handlers/commands.py:_list_sessions_for_context``.  Callers that already
  hold a sandbox-provided endpoint may pass ``base_url`` / ``auth_header``
  through ``**kwargs``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_shrimp.backend.opencode.client import OpenCodeClient
from open_shrimp.backend.protocol import (
    BackendOptions,
    CanUseTool,
    ToolFactory,
)
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.tools import serve_tools_over_mcp_http

if TYPE_CHECKING:
    from open_shrimp.sandbox.agent_runtime import AgentRuntime


class OpenCodeBackend:
    """The OpenCode backend.  ``name == "opencode"``."""

    name = "opencode"

    def make_client(self, options: BackendOptions) -> OpenCodeClient:
        return OpenCodeClient(options)

    def make_runtime(
        self,
        home_dir: Path,
        *,
        context_name: str,
        model: str | None = None,
    ) -> "AgentRuntime":
        """The OpenCode served-endpoint launch profile.

        Parses the provider id from ``model`` (``provider/model``) to filter the
        injected host ``auth.json``; ``context_name`` lets the runtime resolve
        the per-context host dirs the sandbox actually bind-mounts.
        """
        from open_shrimp.backend.opencode.options import split_provider_model
        from open_shrimp.sandbox.agent_runtime import opencode_runtime

        provider_id = split_provider_model(model)[0]
        return opencode_runtime(
            home_dir,
            context_name=context_name,
            provider_id=provider_id,
        )

    def make_tool_server(
        self, tools: ToolFactory
    ) -> Callable[..., dict[str, Any]]:
        """Select the installer for the OpenShrimp tool surface.

        OpenCode reaches the tool surface over the same MCP HTTP bridge every
        backend uses, so this returns ``serve_tools_over_mcp_http`` unchanged
        — the feasibility doc's central claim (one installer, all backends).
        The caller supplies the proxy handle, the tool factory, and the scope
        args at call time.
        """
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
        """List OpenCode sessions for ``directory`` as ``SessionInfo`` rows.

        Lists over HTTP (``GET /session``) against the running server.  With
        no ``base_url`` / ``auth_header`` in ``kwargs`` this targets the
        host-local ``opencode serve`` (the non-sandboxed default).  A caller
        that already holds a sandbox-provided endpoint passes both through
        ``kwargs`` to list against that server instead.
        """
        from open_shrimp.backend.opencode.sessions import (
            list_sessions as _list_sessions,
        )

        base_url = kwargs.get("base_url")
        auth_header = kwargs.get("auth_header")
        return await _list_sessions(
            directory,
            limit=limit,
            base_url=base_url,
            auth_header=auth_header,
        )


__all__ = ["OpenCodeBackend"]
