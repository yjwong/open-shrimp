"""Backend-neutral dataclasses for the MCP proxy.

The proxy itself stays runtime-agnostic: it spawns stdio MCP servers
and reverse-proxies HTTP/SSE MCP servers, but it does not know where
the server lists or OAuth credentials come from.  The dataclasses
here are the shared shape every backend provider produces.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class StdioServerConfig:
    """Parsed stdio MCP server entry, normalised across backends."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class HttpServerConfig:
    """Parsed HTTP/SSE MCP server entry, normalised across backends.

    ``headers`` carries static headers from the config file.  OAuth
    credentials are resolved separately at proxy-forwarding time so
    tokens never enter the sandbox.
    """

    url: str
    transport: Literal["http", "sse"]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class OAuthCredential:
    """Resolved OAuth credential for a single HTTP MCP server."""

    server_name: str
    server_url: str
    access_token: str
    expires_at_ms: int | None  # epoch milliseconds, or None if unknown


def is_expired(cred: OAuthCredential, skew_seconds: int = 60) -> bool:
    """Return True if the credential is expired (with skew tolerance)."""
    if cred.expires_at_ms is None:
        return False
    now_ms = int(time.time() * 1000)
    return now_ms >= cred.expires_at_ms - skew_seconds * 1000


__all__ = [
    "HttpServerConfig",
    "OAuthCredential",
    "StdioServerConfig",
    "is_expired",
]
