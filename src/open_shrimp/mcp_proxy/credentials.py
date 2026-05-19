"""Read OAuth tokens for HTTP MCP servers.

The Claude CLI stores per-server OAuth tokens under the top-level
``mcpOAuth`` key of the same credentials blob that holds the Claude
account credentials.  Each entry is keyed by ``<serverName>|<hash>``
and carries the access token, refresh token, client id/secret, and
expiry.

Storage backend differs by platform (matches Claude Code's own
``getSecureStorage``):

* **macOS** — the macOS login Keychain, service ``Claude Code-credentials``,
  account ``$USER``.  Falls back to the plaintext file if Keychain
  returns nothing.  Same pattern as
  :func:`open_shrimp.sandbox.lima_helpers._read_credentials_json`.
* **Linux / Windows** — ``~/.claude/.credentials.json`` (mode 0600).

The proxy reads these on the host and injects them as upstream
``Authorization`` headers — sandboxes never see the tokens directly.

Token refresh is out of scope for now: if the access token has expired,
the user should re-authenticate via ``/mcp`` on the host so the CLI
refreshes the credentials store.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# macOS Keychain entry written by the Claude Code app.
_MACOS_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Keychain reads cost ~500ms per spawn (per Claude Code's own
# keychainPrefetch.ts).  An MCP SSE handshake can issue several
# requests in quick succession, so we cache for a short window.
_MACOS_KEYCHAIN_TTL_SECONDS = 30.0


@dataclass
class OAuthCredential:
    """Resolved OAuth credential for a single HTTP MCP server."""

    server_name: str
    server_url: str
    access_token: str
    expires_at_ms: int | None  # epoch milliseconds, or None if unknown


def _credentials_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


# File-based cache keyed on (path, mtime_ns).  Invalidates automatically
# when the user re-authenticates and the CLI rewrites the file.
_file_cache: tuple[tuple[str, int], dict[str, Any]] | None = None


def _load_credentials_file() -> dict[str, Any]:
    global _file_cache
    path = _credentials_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        _file_cache = None
        return {}
    except OSError as exc:
        logger.warning("Failed to stat %s: %s", path, exc)
        return {}

    cache_key = (str(path), mtime_ns)
    if _file_cache is not None and _file_cache[0] == cache_key:
        return _file_cache[1]

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}

    _file_cache = (cache_key, data)
    return data


# TTL cache for the macOS Keychain spawn.  There's no cheap mtime
# equivalent to invalidate on.
_keychain_cache: tuple[float, dict[str, Any]] | None = None


def _load_credentials_macos() -> dict[str, Any]:
    """Read credentials from macOS Keychain, falling back to the file."""
    global _keychain_cache
    now = time.monotonic()
    if (
        _keychain_cache is not None
        and now - _keychain_cache[0] < _MACOS_KEYCHAIN_TTL_SECONDS
    ):
        return _keychain_cache[1]

    data: dict[str, Any] = {}
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                _MACOS_KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            payload = result.stdout.strip()
            if payload:
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Failed to parse Keychain credentials JSON: %s", exc
                    )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug(
            "Failed to read credentials from macOS Keychain", exc_info=True
        )

    if not data:
        # Claude Code uses fallback plaintext storage when Keychain
        # writes fail, so try the file as a secondary source.
        data = _load_credentials_file()

    _keychain_cache = (now, data)
    return data


def _load_credentials() -> dict[str, Any]:
    if sys.platform == "darwin":
        return _load_credentials_macos()
    return _load_credentials_file()


def get_oauth_credential(
    server_name: str, server_url: str
) -> OAuthCredential | None:
    """Return the stored OAuth credential for *(server_name, server_url)*.

    Matches on both fields because the same logical name (e.g. "figma")
    could in principle point at different URLs across config scopes.
    Returns ``None`` if no credential is found.
    """
    creds = _load_credentials()
    mcp_oauth = creds.get("mcpOAuth")
    if not isinstance(mcp_oauth, dict):
        return None

    for _key, entry in mcp_oauth.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("serverName") != server_name:
            continue
        if entry.get("serverUrl") != server_url:
            continue
        access_token = entry.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            continue
        expires_at = entry.get("expiresAt")
        return OAuthCredential(
            server_name=server_name,
            server_url=server_url,
            access_token=access_token,
            expires_at_ms=(
                int(expires_at) if isinstance(expires_at, (int, float)) else None
            ),
        )
    return None


def is_expired(cred: OAuthCredential, skew_seconds: int = 60) -> bool:
    """Return True if the credential is expired (with skew tolerance)."""
    if cred.expires_at_ms is None:
        return False
    now_ms = int(time.time() * 1000)
    return now_ms >= cred.expires_at_ms - skew_seconds * 1000
