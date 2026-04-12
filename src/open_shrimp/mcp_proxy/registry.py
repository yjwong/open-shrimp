"""Per-context authentication registry for the MCP proxy.

Each sandboxed context gets a cryptographically random token.
The proxy validates tokens on every request to prevent cross-context
access.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from open_shrimp.mcp_proxy.config_reader import StdioServerConfig


@dataclass
class ContextRegistration:
    """A registered context with its auth token and server configs."""

    context_name: str
    token: str
    servers: dict[str, StdioServerConfig]


class ProxyRegistry:
    """Maps auth tokens to context registrations."""

    def __init__(self) -> None:
        self._by_token: dict[str, ContextRegistration] = {}
        self._by_context: dict[str, ContextRegistration] = {}

    def register_context(
        self,
        context_name: str,
        servers: dict[str, StdioServerConfig],
    ) -> str:
        """Register (or re-register) servers for *context_name*.

        Returns the auth token.  If the context is already registered,
        the existing token is returned and the server list is updated.
        """
        existing = self._by_context.get(context_name)
        if existing is not None:
            existing.servers = servers
            return existing.token

        token = secrets.token_hex(32)
        reg = ContextRegistration(
            context_name=context_name,
            token=token,
            servers=servers,
        )
        self._by_token[token] = reg
        self._by_context[context_name] = reg
        return token

    def unregister_context(self, context_name: str) -> None:
        """Remove all registrations for *context_name*."""
        reg = self._by_context.pop(context_name, None)
        if reg is not None:
            self._by_token.pop(reg.token, None)

    def authenticate(self, token: str) -> ContextRegistration | None:
        """Look up a registration by token.  O(1)."""
        return self._by_token.get(token)
