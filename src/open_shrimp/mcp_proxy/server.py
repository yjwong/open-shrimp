"""MCP Streamable HTTP proxy server.

A lightweight Starlette app that accepts JSON-RPC over HTTP POST and
forwards messages to stdio MCP server processes on the host.  Runs on
a **separate** listener from the main review/config Starlette app to
minimise the attack surface exposed to sandboxes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from open_shrimp.mcp_proxy.config_reader import StdioServerConfig
from open_shrimp.mcp_proxy.registry import ProxyRegistry
from open_shrimp.mcp_proxy.stdio_manager import StdioManager

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# HTTP handler
# -----------------------------------------------------------------------

def _create_proxy_app(
    registry: ProxyRegistry,
    stdio_manager: StdioManager,
) -> Starlette:
    """Build the Starlette ASGI app with the proxy route."""

    async def mcp_endpoint(request: Request) -> Response:
        context_name: str = request.path_params["context_name"]
        server_name: str = request.path_params["server_name"]

        # --- authenticate ---
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
            )
        token = auth_header[7:]
        reg = registry.authenticate(token)
        if reg is None:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        if reg.context_name != context_name:
            return JSONResponse(
                {"error": "token/context mismatch"}, status_code=403
            )

        # --- resolve server ---
        config = reg.servers.get(server_name)
        if config is None:
            return JSONResponse(
                {"error": f"unknown server: {server_name}"}, status_code=404
            )

        # --- parse body ---
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        # --- forward to stdio ---
        try:
            proc = await stdio_manager.get_or_spawn(
                context_name, server_name, config
            )
            response = await stdio_manager.send_message(proc, body)
        except Exception:
            logger.exception(
                "Error forwarding to MCP server '%s/%s'",
                context_name,
                server_name,
            )
            # Return a JSON-RPC internal error so the client can retry.
            error_response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": "Internal error: MCP server unavailable",
                },
            }
            if "id" in body:
                error_response["id"] = body["id"]
            return JSONResponse(error_response, status_code=502)

        if response is None:
            # Notification — no response expected.
            return Response(status_code=202)

        return JSONResponse(response)

    routes = [
        Route(
            "/mcp/{context_name}/{server_name}",
            mcp_endpoint,
            methods=["POST"],
        ),
    ]

    return Starlette(routes=routes)


# -----------------------------------------------------------------------
# McpProxy — lifecycle wrapper
# -----------------------------------------------------------------------

class McpProxy:
    """Manages the MCP proxy HTTP server and backing stdio processes."""

    def __init__(self) -> None:
        self._registry = ProxyRegistry()
        self._stdio_manager = StdioManager()
        self._port: int | None = None
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._listen_socket: socket.socket | None = None

    @property
    def port(self) -> int:
        """The TCP port the proxy is listening on (set after ``start``)."""
        assert self._port is not None, "proxy not started"
        return self._port

    def register_context(
        self,
        context_name: str,
        servers: "dict[str, StdioServerConfig]",
    ) -> str:
        """Register MCP servers for a context, return the auth token."""
        return self._registry.register_context(context_name, servers)

    async def unregister_context(self, context_name: str) -> None:
        """Unregister a context and stop its stdio processes."""
        self._registry.unregister_context(context_name)
        await self._stdio_manager.stop_context(context_name)

    def get_proxy_url(
        self,
        context_name: str,
        server_name: str,
        host_ip: str,
    ) -> str:
        """Build the URL a sandbox should use to reach a proxied server."""
        return f"http://{host_ip}:{self.port}/mcp/{context_name}/{server_name}"

    async def start(self) -> None:
        """Start the HTTP server on an OS-assigned port."""
        # Pre-bind a socket so we know the port before uvicorn starts,
        # and keep it open to avoid a TOCTOU race on the port number.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self._port = sock.getsockname()[1]
        self._listen_socket = sock

        app = _create_proxy_app(self._registry, self._stdio_manager)
        config = uvicorn.Config(
            app,
            # host/port are ignored — we pass our own socket via fd.
            log_level="warning",
            access_log=False,
            fd=sock.fileno(),
        )
        self._server = uvicorn.Server(config)

        self._serve_task = asyncio.create_task(
            self._server.serve(),
            name="mcp-proxy-server",
        )
        for _ in range(50):
            if self._server.started:
                break
            await asyncio.sleep(0.05)

        logger.info("MCP proxy listening on 127.0.0.1:%d", self._port)

    async def shutdown(self) -> None:
        """Stop the HTTP server and all stdio processes."""
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
        if self._listen_socket is not None:
            self._listen_socket.close()
            self._listen_socket = None
        await self._stdio_manager.stop_all()
        logger.info("MCP proxy shut down")
