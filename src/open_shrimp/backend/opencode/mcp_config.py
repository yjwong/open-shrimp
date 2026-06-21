"""MCP server-list and OAuth providers for the OpenCode backend.

OpenCode reads MCP server declarations from
``$XDG_CONFIG_HOME/opencode/opencode.json`` (``.jsonc`` fallback via
``json5``).  Its ``mcp`` schema differs from Claude:

* ``type: local`` → stdio.  ``command`` is a *list* (executable + args).
  Honours ``enabled: false``.
* ``type: remote`` → http.

OpenShrimp layers a private overlay on top: the ``mcp:`` block in
``ContextConfig``, parsed with the OpenShrimp shape (``type: stdio | http
| sse``, ``command`` string).  The overlay wins on name conflicts.

OAuth tokens live in ``$XDG_DATA_HOME/opencode/mcp-auth.json``, keyed
by server name.  Each entry has ``tokens.accessToken`` and
``tokens.expiresAt`` (seconds, converted to ms here).
"""

from __future__ import annotations

import json
import logging
import os
import re
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


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

_T = TypeVar("_T")


def _expand_env_vars(value: str) -> str:
    r"""Expand ``${VAR}`` and ``${VAR:-default}`` in *value*.

    Missing variables with no default are left as-is (``${VAR}``).
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


def _get_opencode_config_path() -> Path:
    """Return the path to OpenCode's global config file."""
    config_dir = os.environ.get("OPENCODE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "opencode.json"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = (
        Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    )
    return config_home / "opencode" / "opencode.json"


def _get_existing_opencode_config_path() -> Path:
    """Return the OpenCode config path, accepting ``.jsonc`` as fallback."""
    path = _get_opencode_config_path()
    if path.is_file():
        return path
    jsonc_path = path.with_suffix(".jsonc")
    if jsonc_path.is_file():
        return jsonc_path
    return path


def _opencode_mcp_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "opencode" / "mcp-auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "mcp-auth.json"


def _parse_opencode_stdio_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, StdioServerConfig]:
    """Extract local stdio servers from OpenCode's ``mcp`` schema."""
    if not raw_servers:
        return {}

    result: dict[str, StdioServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        if entry.get("type") != "local":
            continue
        command = entry.get("command")
        if not isinstance(command, list) or not command:
            logger.warning(
                "OpenCode MCP server '%s' has no local command list, skipping",
                name,
            )
            continue
        if not all(isinstance(part, str) for part in command):
            logger.warning(
                "OpenCode MCP server '%s' command must contain only strings, "
                "skipping",
                name,
            )
            continue
        raw_env = entry.get("environment", entry.get("env", {}))
        result[name] = StdioServerConfig(
            command=_expand_env_vars(command[0]),
            args=_expand_server_args(command[1:]),
            env=_expand_server_env(
                raw_env if isinstance(raw_env, dict) else {}
            ),
        )
    return result


def _parse_opencode_http_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, HttpServerConfig]:
    """Extract remote HTTP servers from OpenCode's ``mcp`` schema."""
    if not raw_servers:
        return {}

    result: dict[str, HttpServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        if entry.get("type") != "remote":
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            logger.warning(
                "OpenCode MCP server '%s' has no url, skipping", name
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
            transport="http",
            headers=headers,
        )
    return result


def _parse_overlay_stdio_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, StdioServerConfig]:
    """Extract stdio servers from an OpenShrimp/Claude-shaped overlay.

    The per-context overlay uses the OpenShrimp shape: ``type: stdio``
    (default when omitted) with a single-string ``command``.
    """
    if not raw_servers:
        return {}

    result: dict[str, StdioServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        server_type = entry.get("type")
        if server_type is not None and server_type != "stdio":
            continue
        command = entry.get("command")
        if not command or not isinstance(command, str):
            logger.warning("MCP server '%s' has no command, skipping", name)
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


def _parse_overlay_http_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, HttpServerConfig]:
    """Extract HTTP/SSE servers from an OpenShrimp/Claude-shaped overlay."""
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
            logger.warning("MCP server '%s' has no url, skipping", name)
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


class OpenCodeMcpConfigProvider:
    """Read MCP server declarations for the OpenCode backend.

    Merges global OpenCode servers (``opencode.json`` ``mcp`` block)
    with the per-context overlay (``ContextConfig.mcp``).  Holds a
    single ``(path, mtime_ns) → parsed config`` cache for the global
    file so it is re-read whenever the user edits it.
    """

    def __init__(self) -> None:
        self._file_cache: tuple[tuple[str, int], dict[str, Any]] | None = None

    def _load_opencode_config(self) -> dict[str, Any]:
        config_path = _get_existing_opencode_config_path()
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
            text = config_path.read_text(encoding="utf-8")
            if config_path.suffix == ".jsonc":
                import json5

                parsed = json5.loads(text)
                data = parsed if isinstance(parsed, dict) else {}
            else:
                data = json.loads(text)
                if not isinstance(data, dict):
                    data = {}
        except (ValueError, OSError) as exc:
            logger.warning("Failed to read %s: %s", config_path, exc)
            return {}

        self._file_cache = (cache_key, data)
        return data

    def _merge(
        self,
        context: "ContextConfig",
        opencode_parser: Callable[[dict[str, Any] | None], dict[str, _T]],
        overlay_parser: Callable[[dict[str, Any] | None], dict[str, _T]],
        label: str,
    ) -> dict[str, _T]:
        config = self._load_opencode_config()
        opencode_servers = opencode_parser(config.get("mcp")) if config else {}
        overlay_servers = overlay_parser(context.mcp)

        merged = {**opencode_servers, **overlay_servers}
        if merged:
            logger.info(
                "Found %d %s MCP server(s) for %s: %s",
                len(merged),
                label,
                context.directory,
                ", ".join(merged),
            )
        return merged

    def stdio_servers(
        self, context: "ContextConfig"
    ) -> dict[str, StdioServerConfig]:
        return self._merge(
            context,
            _parse_opencode_stdio_servers,
            _parse_overlay_stdio_servers,
            "stdio",
        )

    def http_servers(
        self, context: "ContextConfig"
    ) -> dict[str, HttpServerConfig]:
        return self._merge(
            context,
            _parse_opencode_http_servers,
            _parse_overlay_http_servers,
            "HTTP",
        )


class OpenCodeMcpOAuthProvider:
    """Read MCP OAuth tokens from OpenCode's mcp-auth file.

    The file is a JSON object keyed by MCP server name; each entry may
    contain ``serverUrl`` and ``tokens.{accessToken,expiresAt}``.
    ``expiresAt`` is stored in seconds and is converted to milliseconds
    here.
    """

    def __init__(self) -> None:
        # (path, mtime_ns) → parsed JSON dict.
        self._file_cache: tuple[tuple[str, int], dict[str, Any]] | None = None

    def _load_file(self) -> dict[str, Any]:
        path = _opencode_mcp_auth_path()
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

    @staticmethod
    def _normalise_expires_at_ms(expires_at: Any) -> int | None:
        if not isinstance(expires_at, (int, float)):
            return None
        return int(expires_at * 1000)

    def get(
        self, server_name: str, server_url: str
    ) -> OAuthCredential | None:
        entry = self._load_file().get(server_name)
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
            expires_at_ms=self._normalise_expires_at_ms(
                tokens.get("expiresAt")
            ),
        )


__all__ = [
    "OpenCodeMcpConfigProvider",
    "OpenCodeMcpOAuthProvider",
]
