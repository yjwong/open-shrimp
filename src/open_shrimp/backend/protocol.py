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

if TYPE_CHECKING:
    # Import-light: the launch profile type lives in the sandbox package and is
    # only referenced in an annotation, so it stays behind TYPE_CHECKING to keep
    # this protocol module free of sandbox imports (no import cycle at runtime).
    from open_shrimp.backend.policy import BackendPolicy
    from open_shrimp.sandbox.agent_runtime import AgentRuntime

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


# ---------------------------------------------------------------------------
# Options — the configuration contract
# ---------------------------------------------------------------------------


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


__all__ = [
    "Backend",
    "BackendClient",
    "BackendOptions",
    "CanUseTool",
    "ToolFactory",
]
