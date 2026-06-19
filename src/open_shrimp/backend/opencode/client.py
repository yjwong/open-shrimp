"""OpenCodeClient: per-conversation handle bound to one OpenCode session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from open_shrimp.backend.errors import CLIConnectionError, ProcessError
from open_shrimp.backend.opencode.errors import OpenCodeAuthError
from open_shrimp.backend.opencode.options import OpenCodeOptions, to_opencode
from open_shrimp.backend.opencode.permission import PermissionBridge
from open_shrimp.backend.opencode.process import OpenCodeEndpoint, OpenCodeServer
from open_shrimp.backend.opencode.sse import EventBus, EventQueue
from open_shrimp.backend.opencode.tool_names import OPENCODE_PERMISSION_CATEGORIES
from open_shrimp.backend.opencode.translate import _iter_response
from open_shrimp.backend.protocol import BackendOptions
from open_shrimp.backend.types import (
    AssistantMessage,
    Message,
    ResultMessage,
    TextBlock,
)

logger = logging.getLogger(__name__)


_MUTATING_OPENCODE_PERMS = frozenset({
    "edit",
    "write",
    "apply_patch",
    "openshrimp_host_bash",
})
_ASK_BY_DEFAULT_MCP_PERMS = frozenset({
    "openshrimp_create_schedule",
    "openshrimp_delete_schedule",
})
_ALWAYS_ALLOWED_OPENCODE_PERMS = frozenset({"question", "todowrite"})


_BUS_REGISTRY: dict[tuple[str, str, str], EventBus] = {}
_BUS_LOCK: asyncio.Lock | None = None


async def _get_bus(
    server: OpenCodeServer | OpenCodeEndpoint,
    directory: str | None,
) -> EventBus:
    global _BUS_LOCK
    if _BUS_LOCK is None:
        _BUS_LOCK = asyncio.Lock()
    async with _BUS_LOCK:
        key = (server.base_url, server.auth_header, directory or "")
        bus = _BUS_REGISTRY.get(key)
        if bus is None:
            bus = EventBus(server, directory=directory)
            await bus.start()
            _BUS_REGISTRY[key] = bus
        return bus


async def _shutdown_buses() -> None:
    global _BUS_LOCK
    if _BUS_LOCK is None:
        _BUS_LOCK = asyncio.Lock()
    async with _BUS_LOCK:
        for bus in list(_BUS_REGISTRY.values()):
            await bus.stop()
        _BUS_REGISTRY.clear()


class OpenCodeClient:
    """A ``BackendClient`` backed by one ``opencode serve`` session.

    Constructed (not connected) by ``OpenCodeBackend.make_client`` with a
    backend-neutral ``BackendOptions``, translated internally to the native
    ``OpenCodeOptions`` via ``to_opencode``.  ``receive_response`` yields
    ``backend.types`` messages (translation lives in ``translate.py``).
    """

    def __init__(self, options: BackendOptions) -> None:
        self._options: OpenCodeOptions = to_opencode(options)
        self._server: OpenCodeServer | OpenCodeEndpoint | None = None
        self._bus: EventBus | None = None
        self._events: EventQueue | None = None
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._bridge: PermissionBridge | None = None
        self._permission_rules: list[dict[str, Any]] = []

    async def __aenter__(self) -> "OpenCodeClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def is_alive(self) -> bool:
        """True if the underlying ``opencode serve`` process is healthy."""
        server = self._server
        if server is None:
            return False
        if isinstance(server, OpenCodeEndpoint):
            owner = server.owner
            proc = getattr(owner, "_served_proc", None)
            if proc is not None:
                poll = getattr(proc, "poll", None)
                if callable(poll):
                    return poll() is None
            return True
        proc = getattr(server, "proc", None)
        if proc is None:
            return False
        return getattr(proc, "returncode", None) is None

    async def connect(self) -> None:
        if self._server is not None:
            return
        self._server = self._options.endpoint or await OpenCodeServer.get_or_start()
        self._bus = await _get_bus(self._server, self._options.cwd)
        self._http = httpx.AsyncClient(
            base_url=self._server.base_url,
            timeout=30.0,
            headers={"Authorization": self._server.auth_header},
        )
        try:
            await self._register_mcp_servers()
            if self._options.resume:
                self._session_id = self._options.resume
                try:
                    rules = self._build_initial_rules()
                    self._permission_rules = list(rules)
                    await self.get_session_info(self._session_id)
                except CLIConnectionError as exc:
                    if _is_not_found_error(exc):
                        logger.warning(
                            "Resume target %s missing; starting fresh session",
                            self._session_id,
                        )
                        self._session_id = await self._create_session()
                    else:
                        raise
            else:
                self._session_id = await self._create_session()
            assert self._session_id is not None
            self._events = self._bus.subscribe(self._session_id)
            if self._options.can_use_tool is not None:
                self._bridge = PermissionBridge(
                    http=self._http,
                    can_use_tool=self._options.can_use_tool,
                    session_id=self._session_id,
                    directory=self._options.cwd,
                )
        except BaseException:
            await self._http.aclose()
            self._http = None
            raise

    async def connect_control(self) -> None:
        """Connect HTTP control-plane APIs without creating a session."""
        if self._server is not None:
            return
        self._server = self._options.endpoint or await OpenCodeServer.get_or_start()
        self._http = httpx.AsyncClient(
            base_url=self._server.base_url,
            timeout=30.0,
            headers={"Authorization": self._server.auth_header},
        )

    async def _create_session(self) -> str:
        return await self.create_session()

    async def create_session(
        self,
        *,
        directory: str | None = None,
        permission_rules: list[dict[str, Any]] | None = None,
        parent_id: str | None = None,
        title: str | None = None,
        agent: str | None = None,
        model: dict[str, Any] | str | None = None,
    ) -> str:
        """Create an arbitrary OpenCode session on the connected server."""
        assert self._http is not None
        params: dict[str, str] = {}
        session_directory = directory if directory is not None else self._options.cwd
        if session_directory:
            params["directory"] = session_directory
        rules = permission_rules if permission_rules is not None else self._build_initial_rules()
        if permission_rules is None:
            self._permission_rules = list(rules)
        body: dict[str, Any] = {}
        if rules:
            body["permission"] = rules
        if parent_id:
            body["parentID"] = parent_id
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
        try:
            r = await self._http.post("/session", params=params, json=body)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"failed to create session: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code >= 400:
            raise ProcessError(
                f"POST /session returned {r.status_code}: {r.text[:300]}"
            )
        payload = r.json()
        sid = payload.get("id")
        if not sid:
            raise ProcessError(f"POST /session returned no id: {payload!r}")
        return sid

    async def fork_session(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
    ) -> str:
        """Fork an OpenCode session, cloning its conversation history."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.fork_session called before connect()")
        body: dict[str, Any] = {}
        if message_id:
            body["messageID"] = message_id
        try:
            r = await self._http.post(f"/session/{session_id}/fork", json=body)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"failed to fork session: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code == 404:
            raise CLIConnectionError(
                f"fork returned 404 for session {session_id}"
            )
        if r.status_code >= 400:
            raise ProcessError(
                f"POST /session/{session_id}/fork returned {r.status_code}: "
                f"{r.text[:300]}"
            )
        payload = r.json()
        sid = payload.get("id")
        if not sid:
            raise ProcessError(
                f"POST /session/{session_id}/fork returned no id: {payload!r}"
            )
        return sid

    async def delete_session(self, session_id: str) -> None:
        """Delete an arbitrary OpenCode session."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.delete_session called before connect()")
        try:
            r = await self._http.delete(f"/session/{session_id}")
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"DELETE /session/{session_id} failed: {exc}") from exc
        if r.status_code in (404, 410):
            return
        if r.status_code >= 400:
            raise ProcessError(
                f"DELETE /session/{session_id} returned {r.status_code}: {r.text[:300]}"
            )

    async def _register_mcp_servers(self) -> None:
        """Register dynamic MCP servers with OpenCode before session use."""
        if self._http is None or not self._options.mcp_servers:
            return
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        for name, raw_config in self._options.mcp_servers.items():
            config = _coerce_mcp_config(name, raw_config)
            try:
                r = await self._http.post(
                    "/mcp",
                    params=params,
                    json={"name": name, "config": config},
                )
            except httpx.HTTPError as exc:
                raise CLIConnectionError(
                    f"failed to register MCP server {name!r}: {exc}"
                ) from exc
            if r.status_code == 401:
                raise OpenCodeAuthError("opencode serve rejected our credentials")
            if r.status_code >= 400:
                raise ProcessError(
                    f"POST /mcp for {name!r} returned {r.status_code}: {r.text[:300]}"
                )

    async def get_mcp_status(self) -> dict[str, Any]:
        """Return MCP status in the handler shape used by command handlers."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.get_mcp_status called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        try:
            r = await self._http.get("/mcp", params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"GET /mcp failed: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code >= 400:
            raise ProcessError(f"GET /mcp returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        if not isinstance(payload, dict):
            raise ProcessError(f"GET /mcp returned unexpected payload: {payload!r}")
        servers: list[dict[str, Any]] = []
        for name, status in payload.items():
            if isinstance(status, dict):
                servers.append({"name": name, **status})
            else:
                servers.append({"name": name, "status": status})
        return {"mcpServers": servers}

    async def reconnect_mcp_server(self, name: str) -> None:
        """Request an OpenCode MCP server reconnect."""
        await self._post_mcp_connection(name, action="connect")

    async def toggle_mcp_server(self, name: str, *, enabled: bool) -> None:
        """Request a runtime connect or disconnect for an MCP server."""
        action = "connect" if enabled else "disconnect"
        await self._post_mcp_connection(name, action=action)

    async def _post_mcp_connection(self, name: str, *, action: str) -> None:
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient MCP management called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        quoted_name = quote(name, safe="")
        endpoint = f"/mcp/{quoted_name}/{action}"
        try:
            r = await self._http.post(endpoint, params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"POST {endpoint} failed: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code >= 400:
            raise ProcessError(f"POST {endpoint} returned {r.status_code}: {r.text[:300]}")

    def _build_initial_rules(self) -> list[dict[str, Any]]:
        """Construct the initial permission ruleset for this session.

        Order matters: OpenCode's evaluator picks the LAST matching
        rule, so the ask-baseline goes first and user allows go after.
        """
        rules: list[dict[str, Any]] = [
            {"permission": "*", "pattern": "*", "action": "ask"},
        ]
        for category in OPENCODE_PERMISSION_CATEGORIES:
            rules.append(
                {"permission": category, "pattern": "*", "action": "ask"}
            )
        for permission in sorted(_ALWAYS_ALLOWED_OPENCODE_PERMS):
            rules.append(
                {"permission": permission, "pattern": "*", "action": "allow"}
            )
        rules.extend(self._rules_from_allowed_tools(include=_ASK_BY_DEFAULT_MCP_PERMS, invert=True))
        for permission in sorted(_ASK_BY_DEFAULT_MCP_PERMS):
            rules.append({"permission": permission, "pattern": "*", "action": "ask"})
        rules.extend(self._rules_from_allowed_tools(include=_ASK_BY_DEFAULT_MCP_PERMS))
        rules.extend(self._rules_from_add_dirs())
        # OpenShrimp provides its own Agent-compatible MCP tool. Keep
        # OpenCode's built-in task tool out of the model-visible tool list so
        # the two delegation paths do not compete.
        rules.append({"permission": "task", "pattern": "*", "action": "deny"})
        return rules

    def _rules_from_allowed_tools(
        self,
        include: frozenset[str] | None = None,
        invert: bool = False,
    ) -> list[dict[str, Any]]:
        """Translate ``allowed_tools`` entries to OpenCode allow rules.

        Mutating tools (edit/write/apply_patch) are intentionally skipped —
        they always go through ``can_use_tool`` unless "accept all edits"
        is toggled on, which routes through ``update_permission_rules``.
        """
        out: list[dict[str, Any]] = []
        tools = self._options.allowed_tools or []
        for entry in tools:
            if not isinstance(entry, str):
                continue
            permission, pattern = _parse_allowed_tool(entry)
            if permission is None:
                continue
            in_include = include is None or permission in include
            if invert:
                in_include = include is not None and permission not in include
            if not in_include:
                continue
            if permission in _MUTATING_OPENCODE_PERMS:
                continue
            out.append(
                {
                    "permission": permission,
                    "pattern": pattern or "*",
                    "action": "allow",
                }
            )
        return out

    def _rules_from_add_dirs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        dirs: list[str] = []
        if self._options.cwd:
            dirs.append(self._options.cwd)
        if self._options.add_dirs:
            dirs.extend(self._options.add_dirs)
        for d in dirs:
            if not d:
                continue
            pattern = d.rstrip("/") + "/*"
            out.append(
                {
                    "permission": "external_directory",
                    "pattern": pattern,
                    "action": "allow",
                }
            )
        return out

    async def disconnect(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None
        if self._bus is not None and self._session_id is not None:
            self._bus.unsubscribe(self._session_id)
        self._events = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def query(self, prompt: str) -> None:
        if self._http is None or self._session_id is None:
            raise CLIConnectionError("OpenCodeClient.query called before connect()")
        await self.prompt_session(
            self._session_id,
            parts=[{"type": "text", "text": prompt}],
            provider=self._options.provider,
            model=self._options.model,
            variant=self._options.effort,
            system=self._options.system_prompt,
        )

    async def prompt_session(
        self,
        session_id: str,
        *,
        parts: list[dict[str, Any]],
        provider: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        variant: str | None = None,
        system: str | dict[str, Any] | None = None,
    ) -> None:
        """Prompt an arbitrary OpenCode session."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.prompt_session called before connect()")
        body: dict[str, Any] = {
            "parts": parts,
        }
        provider_id = provider if provider is not None else self._options.provider
        model_id = model if model is not None else self._options.model
        if provider_id and model_id:
            body["model"] = {
                "providerID": provider_id,
                "modelID": model_id,
            }
        if agent:
            body["agent"] = agent
        if system is not None:
            body["system"] = _coerce_system_prompt(system)
        if variant is not None:
            body["variant"] = variant
        try:
            r = await self._http.post(
                f"/session/{session_id}/prompt_async", json=body
            )
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"prompt_async failed: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code == 404:
            raise CLIConnectionError(
                f"prompt_async returned 404 for session {session_id}"
            )
        if r.status_code != 204:
            raise ProcessError(
                f"prompt_async returned {r.status_code}: {r.text[:300]}"
            )

    async def patch_session_permissions(
        self,
        session_id: str,
        rules: list[dict[str, Any]],
    ) -> None:
        """Patch permission rules for an arbitrary session.

        OpenCode appends incoming rules to the session ruleset; its evaluator
        uses the last matching rule.
        """
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.patch_session_permissions called before connect()")
        try:
            r = await self._http.patch(
                f"/session/{session_id}",
                json={"permission": rules},
            )
        except httpx.HTTPError as exc:
            raise CLIConnectionError(
                f"PATCH /session/{session_id} failed: {exc}"
            ) from exc
        if r.status_code == 404:
            raise CLIConnectionError(f"PATCH /session/{session_id} returned 404")
        if r.status_code >= 400:
            raise ProcessError(
                f"PATCH /session/{session_id} returned {r.status_code}: {r.text[:300]}"
            )

    async def get_session_info(self, session_id: str) -> dict[str, Any]:
        """Fetch an arbitrary OpenCode session."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.get_session_info called before connect()")
        try:
            r = await self._http.get(f"/session/{session_id}")
        except httpx.HTTPError as exc:
            raise CLIConnectionError(
                f"GET /session/{session_id} failed: {exc}"
            ) from exc
        if r.status_code == 404:
            raise CLIConnectionError(f"GET /session/{session_id} returned 404")
        if r.status_code >= 400:
            raise ProcessError(
                f"GET /session/{session_id} returned {r.status_code}: {r.text[:300]}"
            )
        payload = r.json()
        if not isinstance(payload, dict):
            raise ProcessError(f"GET /session/{session_id} returned non-object: {payload!r}")
        return payload

    async def get_config(self) -> dict[str, Any]:
        """Fetch OpenCode config for this client's project directory."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.get_config called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        try:
            r = await self._http.get("/config", params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"GET /config failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"GET /config returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        if not isinstance(payload, dict):
            raise ProcessError(f"GET /config returned non-object: {payload!r}")
        return payload

    async def get_models(self) -> list[dict[str, Any]]:
        """Fetch available OpenCode models for this client's project directory."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.get_models called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["location[directory]"] = self._options.cwd
        try:
            r = await self._http.get("/api/model", params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"GET /api/model failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"GET /api/model returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        if not isinstance(payload, list):
            raise ProcessError(f"GET /api/model returned non-list: {payload!r}")
        return [item for item in payload if isinstance(item, dict)]

    async def list_providers(self) -> dict[str, Any]:
        """Fetch provider list and connected state from OpenCode."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.list_providers called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        try:
            r = await self._http.get("/provider", params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"GET /provider failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"GET /provider returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        if not isinstance(payload, dict):
            raise ProcessError(f"GET /provider returned non-object: {payload!r}")
        return payload

    async def list_provider_auth_methods(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch provider auth methods from OpenCode."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.list_provider_auth_methods called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        try:
            r = await self._http.get("/provider/auth", params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"GET /provider/auth failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"GET /provider/auth returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        if not isinstance(payload, dict):
            raise ProcessError(f"GET /provider/auth returned non-object: {payload!r}")
        return {
            str(key): [item for item in value if isinstance(item, dict)]
            for key, value in payload.items()
            if isinstance(value, list)
        }

    async def authorize_provider(
        self,
        provider_id: str,
        method_index: int,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Start an OAuth provider auth flow."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.authorize_provider called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        body: dict[str, Any] = {"method": method_index}
        if inputs:
            body["inputs"] = inputs
        path = f"/provider/{quote(provider_id, safe='')}/oauth/authorize"
        try:
            r = await self._http.post(path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"POST {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"POST {path} returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ProcessError(f"POST {path} returned non-object: {payload!r}")
        return payload

    async def complete_provider_oauth(
        self,
        provider_id: str,
        method_index: int,
        code: str | None = None,
    ) -> bool:
        """Complete an OAuth provider auth flow."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.complete_provider_oauth called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        body: dict[str, Any] = {"method": method_index}
        if code:
            body["code"] = code
        path = f"/provider/{quote(provider_id, safe='')}/oauth/callback"
        try:
            r = await self._http.post(path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"POST {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"POST {path} returned {r.status_code}: {r.text[:300]}")
        return bool(r.json())

    async def set_provider_api_key(
        self,
        provider_id: str,
        key: str,
        metadata: dict[str, str] | None = None,
    ) -> bool:
        """Write API-key provider auth through OpenCode."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.set_provider_api_key called before connect()")
        body: dict[str, Any] = {"type": "api", "key": key}
        if metadata:
            body["metadata"] = metadata
        path = f"/auth/{quote(provider_id, safe='')}"
        try:
            r = await self._http.put(path, json=body)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"PUT {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"PUT {path} returned {r.status_code}: {r.text[:300]}")
        return bool(r.json())

    async def remove_provider_auth(self, provider_id: str) -> bool:
        """Remove provider auth through OpenCode."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.remove_provider_auth called before connect()")
        path = f"/auth/{quote(provider_id, safe='')}"
        try:
            r = await self._http.delete(path)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"DELETE {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"DELETE {path} returned {r.status_code}: {r.text[:300]}")
        return bool(r.json())

    async def patch_config_permission(
        self,
        permission_config: dict[str, Any],
    ) -> None:
        """Patch durable OpenCode config permission rules."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.patch_config_permission called before connect()")
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        try:
            r = await self._http.patch(
                "/config",
                params=params,
                json={"permission": permission_config},
            )
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"PATCH /config failed: {exc}") from exc
        if r.status_code >= 400:
            raise ProcessError(f"PATCH /config returned {r.status_code}: {r.text[:300]}")

    async def count_assistant_turns(self, session_id: str) -> int | None:
        """Return assistant message count for a session, if OpenCode exposes it."""
        if self._http is None:
            raise CLIConnectionError("OpenCodeClient.count_assistant_turns called before connect()")
        try:
            r = await self._http.get(f"/session/{session_id}/message")
        except httpx.HTTPError as exc:
            raise CLIConnectionError(
                f"GET /session/{session_id}/message failed: {exc}"
            ) from exc
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ProcessError(
                f"GET /session/{session_id}/message returned {r.status_code}: {r.text[:300]}"
            )
        payload = r.json()
        rows = payload.get("messages") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return None
        return _count_assistant_message_rows(rows)

    async def collect_next_assistant_text(
        self,
        session_id: str,
        queue: EventQueue,
        *,
        timeout: float,
    ) -> str | None:
        """Collect assistant text from an arbitrary session response."""
        async def _collect() -> str | None:
            chunks: list[str] = []
            async for msg in self.iter_session_response(session_id, queue, bridge=None):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            chunks.append(block.text)
                elif isinstance(msg, ResultMessage):
                    break
            text = "".join(chunks).strip()
            return text or None

        try:
            return await asyncio.wait_for(_collect(), timeout=timeout)
        except TimeoutError:
            logger.debug("Timed out collecting prompt suggestion for %s", session_id)
            return None

    async def generate_prompt_suggestion(
        self,
        *,
        prompt: str,
        timeout: float = 30.0,
    ) -> str | None:
        """Generate a next-prompt suggestion in a deny-all fork."""
        if self._session_id is None:
            return None
        fork_id = await self.fork_session(self._session_id)
        queue = self.subscribe_session(fork_id)
        rules = _deny_all_permission_rules()
        try:
            await self.patch_session_permissions(fork_id, rules)
            await self.prompt_session(
                fork_id,
                parts=[{"type": "text", "text": prompt}],
                provider=self._options.provider,
                model=self._options.model,
                variant=self._options.effort,
                system=self._options.system_prompt,
            )
            return await self.collect_next_assistant_text(
                fork_id,
                queue,
                timeout=timeout,
            )
        finally:
            self.unsubscribe_session(fork_id)
            try:
                await self.abort_session(fork_id)
            except Exception:
                logger.debug("Failed to abort prompt suggestion fork %s", fork_id, exc_info=True)
            try:
                await self.delete_session(fork_id)
            except Exception:
                logger.debug("Failed to delete prompt suggestion fork %s", fork_id, exc_info=True)

    async def stop_task(self, task_id: str) -> None:
        """Best-effort background-task stop.

        OpenCode does not expose a per-task abort endpoint, so this
        currently falls back to a full-session interrupt.
        """
        logger.warning(
            "stop_task is not implemented for OpenCode (task_id=%s); "
            "falling back to interrupt()", task_id,
        )
        await self.interrupt()

    async def interrupt(self) -> None:
        """Abort the in-flight turn for this session.

        Maps to OpenCode's ``POST /session/{id}/abort`` endpoint.
        """
        if self._http is None or self._session_id is None:
            return
        await self.abort_session(self._session_id)

    async def abort_session(self, session_id: str) -> None:
        """Abort the in-flight turn for an arbitrary session."""
        if self._http is None:
            return
        try:
            await self._http.post(f"/session/{session_id}/abort")
        except httpx.HTTPError as exc:
            logger.warning("interrupt: POST /abort failed: %s", exc)

    def subscribe_session(self, session_id: str) -> EventQueue:
        """Subscribe to events for an arbitrary session."""
        if self._bus is None:
            raise CLIConnectionError("OpenCodeClient.subscribe_session called before connect()")
        return self._bus.subscribe(session_id)

    def unsubscribe_session(self, session_id: str) -> None:
        """Unsubscribe from events for an arbitrary session."""
        if self._bus is not None:
            self._bus.unsubscribe(session_id)

    def create_permission_bridge(
        self,
        session_id: str,
    ) -> PermissionBridge | None:
        """Create a permission bridge for an arbitrary session."""
        if self._http is None or self._options.can_use_tool is None:
            return None
        return PermissionBridge(
            http=self._http,
            can_use_tool=self._options.can_use_tool,
            session_id=session_id,
            directory=self._options.cwd,
        )

    async def iter_session_response(
        self,
        session_id: str,
        queue: EventQueue,
        *,
        bridge: PermissionBridge | None = None,
    ) -> AsyncIterator[Message]:
        """Translate events for an arbitrary session until ``session.idle``."""
        async for msg in _iter_response(
            queue,
            session_id,
            self._http,
            bridge,
            self._options.handle_questions,
        ):
            yield msg

    async def update_permission_rules(
        self, rules: list[dict[str, Any]],
    ) -> None:
        """Patch the session's permission ruleset.

        Used when the user toggles "accept all edits" — passes the
        new rules to ``PATCH /session/{id}``.
        """
        self._permission_rules.extend(rules)
        if self._session_id is not None:
            await self.patch_session_permissions(self._session_id, rules)

    @property
    def permission_rules(self) -> list[dict[str, Any]]:
        """Current session permission ruleset (most recent build)."""
        return list(self._permission_rules)

    async def receive_response(self) -> AsyncIterator[Message]:
        if self._events is None or self._session_id is None:
            raise CLIConnectionError(
                "OpenCodeClient.receive_response called before connect()"
            )
        async for msg in _iter_response(
            self._events,
            self._session_id,
            self._http,
            self._bridge,
            self._options.handle_questions,
        ):
            if isinstance(msg, ResultMessage) and msg.session_id:
                self._session_id = msg.session_id
            yield msg


def _parse_allowed_tool(entry: str) -> tuple[str | None, str | None]:
    """Parse an ``allowed_tools`` entry into (permission, pattern).

    Accepts both the OpenCode wire form (``bash``, ``bash(git *)``) and
    the capitalised hooks form (``Bash``, ``Bash(git *)``). Lowercases
    everything so the result is an OpenCode permission name.
    """
    text = entry.strip()
    if not text:
        return None, None
    pattern: str | None = None
    if "(" in text and text.endswith(")"):
        head, _, tail = text.partition("(")
        text = head.strip()
        pattern = tail[:-1].strip() or None
    # Treat MCP qualified names (mcp__server__tool, server_tool) as
    # opaque permission names. OpenCode exposes MCP tools as server_tool.
    if text.startswith("mcp__"):
        parts = text.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}_{parts[2]}", pattern
        return text, pattern
    if text.startswith("_"):
        return text, pattern
    lowered = text.lower()
    return lowered, pattern


def _deny_all_permission_rules() -> list[dict[str, Any]]:
    rules = [{"permission": "*", "pattern": "*", "action": "deny"}]
    for category in OPENCODE_PERMISSION_CATEGORIES:
        rules.append({"permission": category, "pattern": "*", "action": "deny"})
    for permission in sorted(
        _MUTATING_OPENCODE_PERMS
        | _ASK_BY_DEFAULT_MCP_PERMS
        | _ALWAYS_ALLOWED_OPENCODE_PERMS
        | {"task"}
    ):
        rules.append({"permission": permission, "pattern": "*", "action": "deny"})
    return rules


def _count_assistant_message_rows(rows: list[Any]) -> int:
    """Count assistant turns in OpenCode's session-message response."""
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = row.get("info")
        if isinstance(info, dict) and info.get("role") == "assistant":
            count += 1
    return count


def _coerce_mcp_config(name: str, raw_config: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not name:
        raise ValueError("MCP server name must be a non-empty string")
    if not isinstance(raw_config, dict):
        raise ValueError(f"MCP server {name!r} config must be an object")
    config = dict(raw_config)
    if "command" in config and "type" not in config:
        command = config.pop("command")
        if not isinstance(command, str) or not command:
            raise ValueError(f"MCP server {name!r} command must be a string")
        args = config.pop("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"MCP server {name!r} args must be a string list")
        env = config.pop("env", config.pop("environment", None))
        out: dict[str, Any] = {"type": "local", "command": [command, *args]}
        if env is not None:
            if not isinstance(env, dict):
                raise ValueError(f"MCP server {name!r} environment must be an object")
            out["environment"] = {str(k): str(v) for k, v in env.items()}
        return out
    cfg_type = config.get("type")
    if cfg_type in {"remote", "http", "sse"}:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"MCP server {name!r} remote config requires url")
        out = {"type": "remote", "url": url}
        if "headers" in config:
            headers = config["headers"]
            if not isinstance(headers, dict):
                raise ValueError(f"MCP server {name!r} headers must be an object")
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
        if "oauth" in config:
            out["oauth"] = config["oauth"]
        if "enabled" in config:
            out["enabled"] = bool(config["enabled"])
        if "timeout" in config:
            out["timeout"] = config["timeout"]
        return out
    if cfg_type == "local":
        command = config.get("command")
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            raise ValueError(f"MCP server {name!r} local command must be a string list")
        out = {"type": "local", "command": command}
        if "environment" in config:
            env = config["environment"]
            if not isinstance(env, dict):
                raise ValueError(f"MCP server {name!r} environment must be an object")
            out["environment"] = {str(k): str(v) for k, v in env.items()}
        return out
    raise ValueError(f"Unsupported MCP config for {name!r}: {raw_config!r}")


def _coerce_system_prompt(value: Any) -> str:
    """Normalise ``options.system_prompt`` into a string for OpenCode."""
    if isinstance(value, str):
        return value
    return ""


def _is_not_found_error(exc: BaseException) -> bool:
    """Heuristic: does an exception suggest the OpenCode session is gone?"""
    msg = str(exc)
    return "404" in msg


__all__ = ["OpenCodeClient"]
