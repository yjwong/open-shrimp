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
* ``list_sessions`` returns ``backend.SessionInfo`` rows via one of three
  paths: HTTP against a caller-supplied endpoint (``base_url`` +
  ``auth_header``), a direct read of OpenCode's on-disk SQLite database
  for sandboxed contexts (avoiding the cost of booting the sandbox just
  to enumerate sessions), or HTTP against the host-local ``opencode
  serve`` as the default.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_shrimp.backend.opencode.client import OpenCodeClient
from open_shrimp.backend.opencode.policy import OpenCodePolicy
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
from open_shrimp.backend.usage import UsageReport

if TYPE_CHECKING:
    from open_shrimp.sandbox.agent_runtime import AgentRuntime


class OpenCodeBackend:
    """The OpenCode backend.  ``name == "opencode"``."""

    name = "opencode"

    policy: OpenCodePolicy = OpenCodePolicy()

    def __init__(self) -> None:
        self._mcp_config_provider: MCPConfigProvider | None = None
        self._mcp_oauth_provider: MCPOAuthProvider | None = None

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
        from open_shrimp.backend.opencode.runtime import opencode_runtime

        provider_id = split_provider_model(model)[0]
        return opencode_runtime(
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

        Three paths, in priority order:

        1. **Caller-supplied endpoint** (``base_url`` + ``auth_header`` in
           ``kwargs``) — list over HTTP against that server.
        2. **Sandboxed context** (``ctx`` + ``ctx_name`` + ``sandbox_managers``
           in ``kwargs``, with ``ctx.sandbox.enabled``) — read OpenCode's
           SQLite database directly from the sandbox-mapped host home dir.
           Avoids the multi-second VM-boot cost of standing the sandbox up
           just to query the running server.
        3. **Non-sandboxed default** — HTTP to host-local ``opencode serve``.

        ``offset`` is accepted in ``kwargs`` for caller uniformity but
        ignored: OpenCode's HTTP list silently drops it server-side, and
        the SQLite path uses ``LIMIT`` without it.
        """
        from open_shrimp.backend.opencode.sessions import (
            list_sessions as _list_sessions,
            list_sessions_from_sqlite,
        )

        base_url = kwargs.get("base_url")
        auth_header = kwargs.get("auth_header")
        if base_url is not None or auth_header is not None:
            return await _list_sessions(
                directory,
                limit=limit,
                base_url=base_url,
                auth_header=auth_header,
            )

        ctx = kwargs.get("ctx")
        ctx_name = kwargs.get("ctx_name")
        sandbox_managers = kwargs.get("sandbox_managers")
        if (
            ctx is not None
            and ctx_name is not None
            and sandbox_managers
            and ctx.sandbox is not None
            and ctx.sandbox.enabled
            and ctx.sandbox.backend in sandbox_managers
        ):
            # The OpenCode runtime maps the host opencode-home (the same dir
            # returned here) to the guest's ``$XDG_DATA_HOME/opencode`` — the
            # directory the in-guest ``opencode serve`` writes its SQLite DB
            # to.  See ``backend/opencode/runtime.py:opencode_runtime``.
            from open_shrimp.sandbox.opencode_runtime import (
                get_opencode_home_dir,
            )

            return await list_sessions_from_sqlite(
                get_opencode_home_dir(ctx_name),
                directory,
                limit=limit,
            )

        return await _list_sessions(
            directory,
            limit=limit,
            base_url=base_url,
            auth_header=auth_header,
        )

    def command_capabilities(self) -> set[str]:
        """OpenCode implements none of the opt-in commands.

        The OpenCode-side equivalents (auth Mini-App, MCP management)
        ship separately and flip their capabilities on then.
        """
        return set()

    def auth_copy(self) -> AuthCopy:
        """Skip every auth-copy site that doesn't apply to OpenCode.

        The Mini-App and command-description strings stay non-empty so
        a future OpenCode-side login flow can flip the capability on
        without re-touching this file.  ``auth_error_hint`` is ``None``
        because the Claude-shaped ``/login`` hint would mislead.
        """
        return AuthCopy(
            login_command_description="Re-authenticate provider",
            login_mini_app_body="Re-authenticate provider",
            auth_error_hint=None,
        )

    def mcp_config_source(self) -> MCPConfigProvider:
        if self._mcp_config_provider is None:
            from open_shrimp.backend.opencode.mcp_config import (
                OpenCodeMcpConfigProvider,
            )

            self._mcp_config_provider = OpenCodeMcpConfigProvider()
        return self._mcp_config_provider

    def mcp_oauth_source(self) -> MCPOAuthProvider:
        if self._mcp_oauth_provider is None:
            from open_shrimp.backend.opencode.mcp_config import (
                OpenCodeMcpOAuthProvider,
            )

            self._mcp_oauth_provider = OpenCodeMcpOAuthProvider()
        return self._mcp_oauth_provider

    async def usage(self) -> UsageReport | None:
        """No usage notion yet; flips on with the auth Mini-App."""
        return None


__all__ = ["OpenCodeBackend"]
