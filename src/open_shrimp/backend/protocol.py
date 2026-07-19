"""The runtime ``Backend`` / ``BackendClient`` protocols and ``BackendOptions``.

Three contracts:
``BackendOptions`` (the honoured configuration intersection), the
``BackendClient`` Protocol (the client lifecycle ``client_manager`` drives),
and the ``Backend`` Protocol (the factory surface selected once at startup).

No SDK imports — this is a pure structural contract.  The content/message
types, permission results, ``SessionInfo``, and the error aliases it references
are imported from their respective modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from open_shrimp.backend import types as bt
from open_shrimp.backend.sessions import SessionInfo
from open_shrimp.backend.usage import UsageReport
from open_shrimp.mcp_proxy.types import (
    HttpServerConfig,
    OAuthCredential,
    StdioServerConfig,
)

if TYPE_CHECKING:
    # Import-light: the launch profile type lives in the sandbox package and is
    # only referenced in an annotation, so it stays behind TYPE_CHECKING to keep
    # this protocol module free of sandbox imports (no import cycle at runtime).
    from telegram import Bot

    from open_shrimp.backend.policy import BackendPolicy
    from open_shrimp.config import Config, ContextConfig
    from open_shrimp.db import ChatScope
    from open_shrimp.sandbox.agent_runtime import AgentRuntime
    from open_shrimp.stream import StreamResult

# The callback the backend invokes before running a non-auto-approved tool.
# Signature is already uniform across master + opencode (hooks.py builds it).
CanUseTool = Callable[
    [str, dict[str, Any], bt.ToolPermissionContext],
    Awaitable[bt.PermissionResult],
]

# A factory that returns the live OpenShrimp tool list (bot/chat scope already
# bound).  ``tools.py:create_openshrimp_tools`` is invoked through one of these;
# ``Backend.make_tool_server`` selects the installer that consumes it.
ToolFactory = Callable[[], list[Any]]

# An async ``session_id -> checklist`` reader for backends whose checklist
# tools are incremental (no single tool input carries the full list).  Returns
# ``{"content", "status", "activeForm"}`` dicts — the shape the pinned-message
# renderer and agent-status helpers consume.
ChecklistReader = Callable[[str], Awaitable[list[dict[str, Any]]]]


# ---------------------------------------------------------------------------
# Options — the configuration contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCopy:
    """Backend-supplied strings for user-facing UI surfaces.

    A single declaration site for the bot's per-backend copy: the
    ``/login`` command description in the Telegram menu, the Mini-App
    body, the auth-error hint surfaced when an agent reports an auth
    failure mid-stream, and the assistant-error rendering table.

    A ``None`` field (or empty mapping for ``assistant_error_messages``)
    means "skip the corresponding site entirely" — distinct from an
    empty string, which would still be rendered.

    ``assistant_error_messages`` keys are the neutral error codes from
    ``AssistantMessage.error``; the value is the rendered message body
    (GFM, will be converted to MarkdownV2 by ``gfm_to_telegram``).
    Missing keys fall back to the shared neutral defaults in
    ``stream.py``, then to a generic ``⚠️ Error: <code>``.
    """

    login_command_description: str | None = None
    login_mini_app_body: str | None = None
    auth_error_hint: str | None = None
    assistant_error_messages: dict[str, str] = field(default_factory=dict)


@dataclass
class BackendOptions:
    """The honoured intersection of every backend's option set.

    Backend-specific knobs live in ``extra`` so call sites never branch.  On
    ``master`` the only backend (``claude_sdk``) honours *every* field, so the
    "accept but ignore" fields have no live effect yet — but they stay so the
    ``client_manager`` call site does not change when a second backend lands.

    ``system_prompt`` is typed ``Any`` (the reference draft types it
    ``str | None``): the live SDK path passes a *preset-dict*
    (``{"type": "preset", "preset": "claude_code", "append": ...}``,
    ``client_manager.py``), not a plain string.  The adapter accepts either
    shape and passes it through unchanged.
    """

    cwd: str
    model: str | None = None  # backends needing provider/model parse ``extra``
    resume: str | None = None

    # Honoured by all backends (semantics may map differently).
    effort: str | None = None
    allowed_tools: list[str] | None = None
    add_dirs: list[str] | None = None
    system_prompt: Any = None  # str | preset-dict | None (see docstring)

    # Callbacks.
    can_use_tool: CanUseTool | None = None
    stderr: Callable[[str], None] | None = None

    # In-process tool surface (OpenShrimp's MCP tools).  The installer
    # (``Backend.make_tool_server``) returns the backend-specific handle that
    # populates this; today it is the shared HTTP-bridge ``mcp_servers`` dict.
    mcp_servers: dict[str, Any] | None = None

    # Accepted-but-ignored by some backends (kept for call-site uniformity).
    # The SDK honours all of them; non-SDK backends will not.
    setting_sources: list[str] | None = None  # SDK only
    include_partial_messages: bool = True  # SDK only
    max_buffer_size: int | None = None  # SDK only
    cli_path: str | None = None  # SDK + JSONL (sandbox wrapper); not OpenCode

    # Backend-specific overflow (e.g. opencode endpoint, provider split).
    # Ignored by the ``claude_sdk`` backend.
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The client protocol — lifecycle
# ---------------------------------------------------------------------------


@runtime_checkable
class BackendClient(Protocol):
    """A persistent agent client, one per ChatScope.

    Lifecycle: ``connect()`` -> (``query()`` / ``receive_response()``)* ->
    ``disconnect()``.  The first message may resume (``options.resume``);
    subsequent messages on the same live client call ``query()`` again without
    re-resuming.  Exactly the method set ``client_manager.py`` calls.
    """

    @property
    def session_id(self) -> str | None:
        """The client's own view of the session id, available after the init
        ``SystemMessage`` — before the first ``ResultMessage``, so a cancel
        before the result still records the session.  (``AgentSession`` keeps
        its own ``session_id`` attribute updated in ``receive_events``; this
        property is complementary, not a replacement.)"""
        ...

    def is_alive(self) -> bool:
        """Non-blocking liveness probe.  SDK: subprocess returncode poke."""
        ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[bt.Message]:
        """Yield events until the turn ends (``ResultMessage``).  Yields
        ``backend.types`` messages, **not** SDK types — the SDK adapter
        translates inside this method so SDK types never escape it."""
        ...

    async def interrupt(self) -> None: ...

    async def stop_task(self, task_id: str) -> None: ...

    # MCP management (/mcp).
    async def get_mcp_status(self) -> dict[str, Any]: ...

    async def reconnect_mcp_server(self, name: str) -> None: ...

    async def toggle_mcp_server(self, name: str, *, enabled: bool) -> None: ...


# ---------------------------------------------------------------------------
# MCP provider seams — keep ``mcp_proxy/`` runtime-agnostic
# ---------------------------------------------------------------------------


@runtime_checkable
class MCPConfigProvider(Protocol):
    """Read MCP server declarations applicable to a context.

    The shape and storage of these declarations differs per backend
    (``~/.claude.json`` user+local merge vs. ``opencode.json`` plus
    a per-context overlay).  The proxy consumes the normalised result.
    """

    def stdio_servers(
        self, context: "ContextConfig"
    ) -> dict[str, StdioServerConfig]: ...

    def http_servers(
        self, context: "ContextConfig"
    ) -> dict[str, HttpServerConfig]: ...


@runtime_checkable
class MCPOAuthProvider(Protocol):
    """Resolve the OAuth credential for one HTTP MCP server.

    Returns ``None`` when no credential is on file; the proxy then
    surfaces a 401 with a backend-appropriate re-auth hint.
    """

    def get(
        self, server_name: str, server_url: str
    ) -> OAuthCredential | None: ...


# ---------------------------------------------------------------------------
# The backend protocol — the factory surface
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """A backend: a client factory, a tool-server selector, the permission
    entry point, and ``list_sessions``.  Selected once at startup from config
    (``backend:``), resolved like ``SandboxManager``.
    """

    name: str

    #: Per-backend tool taxonomy and rendering.  Answers tool-shape
    #: questions (path-scoped? mutating? how to summarise?) in the
    #: backend's native vocabulary so the orchestration code never
    #: branches on tool name.
    policy: "BackendPolicy"

    def make_client(self, options: BackendOptions) -> BackendClient:
        """Construct (do **not** connect) a client for one ChatScope."""
        ...

    def make_runtime(
        self,
        home_dir: Path,
        *,
        context_name: str,
        model: str | None,
    ) -> "AgentRuntime":
        """The sandbox launch profile for this backend.  Mirrors ``make_client``:
        the backend owns which runtime it wants and how to derive its inputs.

        ``claude_sdk`` → ``claude_runtime`` (WrappedCLI; ``context_name``/``model``
        unused); ``opencode`` → ``opencode_runtime`` (ServedEndpoint, parsing the
        provider id from ``model`` and resolving its per-context host dirs from
        ``context_name``).
        """
        ...

    def make_tool_server(self, tools: ToolFactory) -> Callable[..., dict[str, Any]]:
        """Select the installer for the OpenShrimp tool surface.

        Returns the installer *callable*; ``client_manager`` still supplies the
        proxy handle and scope identity (those are caller-owned sandbox/scope
        facts, not backend facts).  For ``claude_sdk`` this returns the shared
        HTTP-bridge installer ``serve_tools_over_mcp_http``.

        The ``tools`` factory is accepted so a backend that needs to install
        eagerly can, but the shared bridge installer takes the factory at call
        time, so this is the *selector* form."""
        ...

    def make_can_use_tool(
        self,
        request_approval: Any,
        cwd: str,
        **kwargs: Any,
    ) -> CanUseTool:
        """The permission callback.  Signature already uniform across backends;
        the body is ``hooks.make_can_use_tool`` (backend-agnostic except for
        how the decision is delivered, which is the SDK's own mechanism)."""
        ...

    async def list_sessions(
        self,
        directory: str | Path,
        *,
        limit: int = 500,
        **kwargs: Any,
    ) -> list[SessionInfo]:
        """For /resume.  SDK: ``claude_agent_sdk.list_sessions``."""
        ...

    def checklist_reader(
        self,
        *,
        ctx: "ContextConfig",
        ctx_name: str,
        **kwargs: Any,
    ) -> ChecklistReader | None:
        """An async ``session_id -> checklist`` reader for this context.

        ``None`` when the backend has no checklist store to pull from —
        i.e. when every checklist tool input carries a full snapshot
        (``policy.checklist_snapshot``), so the stream never needs to
        read the list from elsewhere.  How and where the checklist is
        persisted is entirely the backend's business; like
        ``list_sessions``, backend-specific context (e.g. sandbox state)
        travels in ``kwargs``.
        """
        ...

    def command_capabilities(self) -> set[str]:
        """The bot-command names this backend implements end-to-end.

        Commands every backend always supports (``/context``, ``/clear``,
        ``/status``, …) are registered unconditionally; this set only
        covers the surface a backend can genuinely opt out of.

        A flat ``set[str]`` rather than a typed flags struct so adding a
        new backend-specific command is a one-line declaration on one
        backend, not a protocol change.
        """
        ...

    def copy(self) -> "BackendCopy":
        """Backend-supplied strings for user-facing UI surfaces.

        See :class:`BackendCopy` for field semantics.  ``None`` on any
        field means "skip the corresponding site entirely" — distinct
        from an empty string.
        """
        ...

    def mcp_config_source(self) -> "MCPConfigProvider":
        """The MCP server-list reader for this backend.

        Returns the same instance every call so the underlying caches
        (file mtime, parsed config) survive between requests.
        """
        ...

    def mcp_oauth_source(self) -> "MCPOAuthProvider":
        """The MCP OAuth-credential reader for this backend.

        Returns the same instance every call so any TTL or mtime
        caches survive between requests.
        """
        ...

    async def usage(self) -> UsageReport | None:
        """Operator quota / spend snapshot, or ``None`` when unavailable.

        Backends that declare ``"usage"`` in :meth:`command_capabilities`
        must implement this. ``None`` is the runtime "couldn't fetch"
        path (creds missing, endpoint unreachable, token expired) —
        distinct from "doesn't support usage at all", which is the
        capability gate.

        Backends own their own caching; the handler calls this once per
        ``/usage`` invocation and renders whatever comes back.
        """
        ...

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
        """Post-turn extension point invoked after every Telegram turn.

        Called once after ``stream_response`` returns and the session-id
        persistence / pinned-status update has happened, so ``result``
        is fully populated.

        Backends use this for per-turn UI affordances that depend on
        the just-finished turn (e.g. attaching a prompt-suggestion
        button to the last finalized message). Implementations should
        schedule any long-running work as background tasks; the caller
        does not await indirectly via this hook. Backends without a
        per-turn affordance return without doing anything.
        """
        ...


__all__ = [
    "Backend",
    "BackendClient",
    "BackendCopy",
    "BackendOptions",
    "CanUseTool",
    "MCPConfigProvider",
    "MCPOAuthProvider",
    "ToolFactory",
    "UsageReport",
]
