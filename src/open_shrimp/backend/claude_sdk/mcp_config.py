"""MCP server-list and OAuth providers for the Claude Agent SDK backend.

The Claude CLI reads MCP server declarations from ``~/.claude.json``:

* **User scope** — top-level ``mcpServers`` map.
* **Local scope** — ``projects[normalised_dir].mcpServers`` map.  Local
  entries win on name conflicts.

OAuth tokens for HTTP MCP servers live under the top-level
``mcpOAuth`` key of the same credentials blob that holds the Claude
account credentials.  Storage backend differs by platform:

* **macOS** — login Keychain, service ``Claude Code-credentials``,
  account ``$USER``.  Falls back to the plaintext file if Keychain
  returns nothing.
* **Linux / Windows** — ``~/.claude/.credentials.json`` (mode 0600).

Token refresh is out of scope: an expired token surfaces as a 401
with a re-auth hint pointing at ``/login`` / ``/mcp``.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from open_shrimp.mcp_proxy.types import (
    HttpServerConfig,
    OAuthCredential,
    StdioServerConfig,
)

if TYPE_CHECKING:
    from open_shrimp.config import ContextConfig

logger = logging.getLogger(__name__)


_MACOS_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Keychain reads cost ~500ms per spawn.  An MCP SSE handshake can issue
# several requests in quick succession, so a short TTL amortises the cost.
_MACOS_KEYCHAIN_TTL_SECONDS = 30.0


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

_T = TypeVar("_T")


def _expand_env_vars(value: str) -> str:
    r"""Expand ``${VAR}`` and ``${VAR:-default}`` in *value*.

    Mirrors the Claude CLI's ``expandEnvVarsInString``.  Missing variables
    with no default are left as-is (``${VAR}``).
    """

    def _replace(match: re.Match[str]) -> str:
        content = match.group(1)
        parts = content.split(":-", 1)
        var_name = parts[0]
        default = parts[1] if len(parts) > 1 else None
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        logger.warning("MCP config references undefined env var: ${%s}", var_name)
        return match.group(0)

    return _ENV_VAR_RE.sub(_replace, value)


def _expand_server_env(env: dict[str, str]) -> dict[str, str]:
    return {k: _expand_env_vars(v) for k, v in env.items()}


def _expand_server_args(args: list[str]) -> list[str]:
    return [_expand_env_vars(a) for a in args]


def _get_claude_config_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def _normalise_path_for_config_key(path: str) -> str:
    """Match the key format the Claude CLI writes for project entries."""
    normalised = os.path.normpath(path)
    return normalised.replace("\\", "/")


def _parse_stdio_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, StdioServerConfig]:
    if not raw_servers:
        return {}

    result: dict[str, StdioServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        server_type = entry.get("type")
        # stdio is the default when type is omitted.
        if server_type is not None and server_type != "stdio":
            continue
        command = entry.get("command")
        if not command or not isinstance(command, str):
            logger.warning(
                "MCP server '%s' in ~/.claude.json has no command, skipping",
                name,
            )
            continue
        raw_args = entry.get("args", [])
        raw_env = entry.get("env", {})
        result[name] = StdioServerConfig(
            command=_expand_env_vars(command),
            args=_expand_server_args(
                raw_args if isinstance(raw_args, list) else []
            ),
            env=_expand_server_env(
                raw_env if isinstance(raw_env, dict) else {}
            ),
        )
    return result


def _parse_http_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, HttpServerConfig]:
    if not raw_servers:
        return {}

    result: dict[str, HttpServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        server_type = entry.get("type")
        if server_type not in ("http", "sse"):
            continue
        url = entry.get("url")
        if not url or not isinstance(url, str):
            logger.warning(
                "MCP server '%s' in ~/.claude.json has no url, skipping",
                name,
            )
            continue
        raw_headers = entry.get("headers", {})
        headers = {
            k: _expand_env_vars(v)
            for k, v in (
                raw_headers if isinstance(raw_headers, dict) else {}
            ).items()
            if isinstance(v, str)
        }
        result[name] = HttpServerConfig(
            url=_expand_env_vars(url),
            transport=server_type,
            headers=headers,
        )
    return result


class ClaudeMcpConfigProvider:
    """Read MCP server declarations from ``~/.claude.json``.

    Holds a single ``(path, mtime_ns) → parsed config`` cache, so the
    file is re-read whenever the user edits it (or runs ``/mcp`` to
    add a server) but reused otherwise.
    """

    def __init__(self) -> None:
        # (path, mtime_ns) → parsed JSON dict.
        self._file_cache: tuple[tuple[str, int], dict[str, Any]] | None = None

    def _load_claude_config(self) -> dict[str, Any]:
        config_path = _get_claude_config_path()
        try:
            mtime_ns = config_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._file_cache = None
            return {}
        except OSError as exc:
            logger.warning("Failed to stat %s: %s", config_path, exc)
            return {}

        cache_key = (str(config_path), mtime_ns)
        if self._file_cache is not None and self._file_cache[0] == cache_key:
            return self._file_cache[1]

        try:
            data: dict[str, Any] = json.loads(
                config_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", config_path, exc)
            return {}

        self._file_cache = (cache_key, data)
        return data

    def _merge_user_and_local(
        self,
        project_dir: str,
        parser: Callable[[dict[str, Any] | None], dict[str, _T]],
        label: str,
    ) -> dict[str, _T]:
        config = self._load_claude_config()
        if not config:
            return {}

        user_servers = parser(config.get("mcpServers"))

        local_servers: dict[str, _T] = {}
        projects = config.get("projects")
        if isinstance(projects, dict):
            resolved_dir = str(Path(project_dir).resolve())
            key = _normalise_path_for_config_key(resolved_dir)
            project_config = projects.get(key, {})
            if isinstance(project_config, dict):
                local_servers = parser(project_config.get("mcpServers"))

        merged = {**user_servers, **local_servers}
        if merged:
            logger.info(
                "Found %d %s MCP server(s) for %s: %s",
                len(merged),
                label,
                project_dir,
                ", ".join(merged),
            )
        return merged

    def stdio_servers(
        self, context: "ContextConfig"
    ) -> dict[str, StdioServerConfig]:
        return self._merge_user_and_local(
            context.directory, _parse_stdio_servers, "stdio"
        )

    def http_servers(
        self, context: "ContextConfig"
    ) -> dict[str, HttpServerConfig]:
        return self._merge_user_and_local(
            context.directory, _parse_http_servers, "HTTP"
        )


def _credentials_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


class ClaudeMcpOAuthProvider:
    """Read per-server OAuth credentials from the Claude credentials blob.

    Linux/Windows: ``~/.claude/.credentials.json`` keyed on file mtime.
    macOS: login Keychain (``Claude Code-credentials``) with a TTL cache
    (no cheap mtime equivalent on the Keychain spawn), falling back to
    the plaintext file when Keychain returns nothing.
    """

    def __init__(self) -> None:
        # File-based cache keyed on (path, mtime_ns).
        self._file_cache: tuple[tuple[str, int], dict[str, Any]] | None = None
        # TTL cache for the macOS Keychain spawn.
        self._keychain_cache: tuple[float, dict[str, Any]] | None = None

    def _load_credentials_file(self) -> dict[str, Any]:
        path = _credentials_path()
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._file_cache = None
            return {}
        except OSError as exc:
            logger.warning("Failed to stat %s: %s", path, exc)
            return {}

        cache_key = (str(path), mtime_ns)
        if self._file_cache is not None and self._file_cache[0] == cache_key:
            return self._file_cache[1]

        try:
            data: dict[str, Any] = json.loads(
                path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return {}

        self._file_cache = (cache_key, data)
        return data

    def _load_credentials_macos(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self._keychain_cache is not None
            and now - self._keychain_cache[0] < _MACOS_KEYCHAIN_TTL_SECONDS
        ):
            return self._keychain_cache[1]

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
                            "Failed to parse Keychain credentials JSON: %s",
                            exc,
                        )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug(
                "Failed to read credentials from macOS Keychain",
                exc_info=True,
            )

        if not data:
            # Mirrors the host CLI's fallback when Keychain writes fail.
            data = self._load_credentials_file()

        self._keychain_cache = (now, data)
        return data

    def _load_credentials(self) -> dict[str, Any]:
        if sys.platform == "darwin":
            return self._load_credentials_macos()
        return self._load_credentials_file()

    def get(
        self, server_name: str, server_url: str
    ) -> OAuthCredential | None:
        """Return the stored OAuth credential for ``(server_name, server_url)``.

        Matches on both fields because the same logical name (e.g. "figma")
        could in principle point at different URLs across config scopes.
        Returns ``None`` if no credential is found.
        """
        creds = self._load_credentials()
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
                    int(expires_at)
                    if isinstance(expires_at, (int, float))
                    else None
                ),
            )
        return None


__all__ = [
    "ClaudeMcpConfigProvider",
    "ClaudeMcpOAuthProvider",
]
