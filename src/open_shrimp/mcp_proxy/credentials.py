"""Read OpenCode OAuth tokens for HTTP MCP servers.

OpenCode stores MCP OAuth tokens in
``~/.local/share/opencode/mcp-auth.json``.  The file is a JSON object keyed
by MCP server name; each entry may contain ``serverUrl`` and ``tokens``.
The proxy reads this file on the host and injects upstream
``Authorization`` headers, so sandboxes never see the tokens directly.

Token refresh is out of scope for now: if the access token has expired,
the user should re-authenticate via ``/mcp`` on the host so the CLI
refreshes the credentials store.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OAuthCredential:
    """Resolved OAuth credential for a single HTTP MCP server."""

    server_name: str
    server_url: str
    access_token: str
    expires_at_ms: int | None  # epoch milliseconds, or None if unknown


def _opencode_mcp_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "opencode" / "mcp-auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "mcp-auth.json"


# File-based cache keyed on (path, mtime_ns). Invalidates automatically
# when OpenCode re-authenticates and rewrites the file.
_opencode_file_cache: tuple[tuple[str, int], dict[str, Any]] | None = None


def _load_opencode_mcp_auth_file() -> dict[str, Any]:
    global _opencode_file_cache
    path = _opencode_mcp_auth_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        _opencode_file_cache = None
        return {}
    except OSError as exc:
        logger.warning("Failed to stat %s: %s", path, exc)
        return {}

    cache_key = (str(path), mtime_ns)
    if _opencode_file_cache is not None and _opencode_file_cache[0] == cache_key:
        return _opencode_file_cache[1]

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}

    _opencode_file_cache = (cache_key, data)
    return data


def _normalise_expires_at_ms(expires_at: Any) -> int | None:
    if not isinstance(expires_at, (int, float)):
        return None
    return int(expires_at * 1000)


def get_oauth_credential(
    server_name: str, server_url: str
) -> OAuthCredential | None:
    """Return the stored OAuth credential for *(server_name, server_url)*.

    Matches on both fields because the same logical name (e.g. "figma")
    could in principle point at different URLs across config scopes.
    Returns ``None`` if no credential is found.
    """
    entry = _load_opencode_mcp_auth_file().get(server_name)
    if not isinstance(entry, dict):
        return None
    stored_url = entry.get("serverUrl")
    if stored_url is not None and stored_url != server_url:
        return None
    tokens = entry.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        return None
    return OAuthCredential(
        server_name=server_name,
        server_url=server_url,
        access_token=access_token,
        expires_at_ms=_normalise_expires_at_ms(tokens.get("expiresAt")),
    )


def is_expired(cred: OAuthCredential, skew_seconds: int = 60) -> bool:
    """Return True if the credential is expired (with skew tolerance)."""
    if cred.expires_at_ms is None:
        return False
    now_ms = int(time.time() * 1000)
    return now_ms >= cred.expires_at_ms - skew_seconds * 1000
