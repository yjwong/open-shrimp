"""Backend-neutral tool installer.

Serves the OpenShrimp tool surface (~22 tools) over the MCP proxy's
host-loopback HTTP bridge (``/tools/{scope_token}``) and returns the
``mcp_servers`` config handle the agent backend consumes.  Every backend
that speaks the MCP ``tools/list`` / ``tools/call`` protocol reaches tools
over this same bridge; ``Backend.make_tool_server`` delegates here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from open_shrimp.tools import OpenShrimpTool


def serve_tools_over_mcp_http(
    mcp_proxy: Any,  # McpProxy
    tool_factory: Callable[[], list[OpenShrimpTool]],
    *,
    context_name: str,
    chat_id: int,
    thread_id: int | None,
    user_id: int,
    host_ip: str,
) -> dict[str, Any]:
    """Register the scope and return ``{"type": "http", "url": ...}``.

    The ``tool_factory`` is stored by the bridge and re-invoked per request,
    so the tool list can depend on live state (e.g. forum-thread membership).
    ``host_ip`` is the caller's sandbox/loopback decision; the installer stays
    ignorant of it.
    """
    scope_token = mcp_proxy.register_tool_scope(
        context_name=context_name,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        tool_factory=tool_factory,
    )
    return {
        "type": "http",
        "url": mcp_proxy.get_tools_url(scope_token, host_ip),
    }
