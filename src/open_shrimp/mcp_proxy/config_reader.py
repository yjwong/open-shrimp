"""Read MCP server configurations from ``~/.claude.json``.

Extracts *user-scope* (root ``mcpServers``) and *local-scope*
(``projects[normalised_path].mcpServers``) stdio server configs so the
MCP proxy can spawn them on the host on behalf of a sandboxed context.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StdioServerConfig:
    """Parsed stdio MCP server entry from ``~/.claude.json``."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_claude_config_path() -> Path:
    """Return the path to the Claude global config file.

    Respects ``CLAUDE_CONFIG_DIR`` if set, otherwise defaults to
    ``~/.claude.json``.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def _normalise_path_for_config_key(path: str) -> str:
    """Normalise *path* to match the key format used in ``~/.claude.json``.

    On Linux/macOS this is equivalent to ``os.path.normpath``.  On Windows
    backslashes are also converted to forward slashes for parity with the
    Claude CLI's ``normalizePathForConfigKey``.
    """
    normalised = os.path.normpath(path)
    return normalised.replace("\\", "/")


# ---------------------------------------------------------------------------
# Environment variable expansion
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(value: str) -> str:
    r"""Expand ``${VAR}`` and ``${VAR:-default}`` in *value*.

    Mirrors the behaviour of the Claude CLI's ``expandEnvVarsInString``.
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
        return match.group(0)  # leave as-is

    return _ENV_VAR_RE.sub(_replace, value)


def _expand_server_env(env: dict[str, str]) -> dict[str, str]:
    """Expand environment variables in all *env* values."""
    return {k: _expand_env_vars(v) for k, v in env.items()}


def _expand_server_args(args: list[str]) -> list[str]:
    """Expand environment variables in *args*."""
    return [_expand_env_vars(a) for a in args]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_claude_config() -> dict[str, Any]:
    """Load and return ``~/.claude.json`` as a dict.

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    config_path = get_claude_config_path()
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", config_path, exc)
        return {}


def _parse_stdio_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, StdioServerConfig]:
    """Extract stdio server configs from a raw ``mcpServers`` dict.

    Servers with an explicit ``type`` other than ``"stdio"`` are skipped
    (they are http/sse/sdk servers that don't need proxying).
    """
    if not raw_servers:
        return {}

    result: dict[str, StdioServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        server_type = entry.get("type")
        # stdio is the default when type is omitted
        if server_type is not None and server_type != "stdio":
            continue
        command = entry.get("command")
        if not command or not isinstance(command, str):
            logger.warning(
                "MCP server '%s' in ~/.claude.json has no command, skipping", name
            )
            continue
        raw_args = entry.get("args", [])
        raw_env = entry.get("env", {})
        result[name] = StdioServerConfig(
            command=_expand_env_vars(command),
            args=_expand_server_args(raw_args if isinstance(raw_args, list) else []),
            env=_expand_server_env(raw_env if isinstance(raw_env, dict) else {}),
        )
    return result


def get_mcp_servers_for_directory(
    project_dir: str,
) -> dict[str, StdioServerConfig]:
    """Return stdio MCP servers applicable to *project_dir*.

    Merges user-scope servers (``mcpServers`` at the root of
    ``~/.claude.json``) with local-scope servers
    (``projects[normalised_dir].mcpServers``).  Local-scope entries win
    on name conflicts.
    """
    config = load_claude_config()
    if not config:
        return {}

    # User-scope: root-level mcpServers
    user_servers = _parse_stdio_servers(config.get("mcpServers"))

    # Local-scope: projects[normalised_path].mcpServers
    local_servers: dict[str, StdioServerConfig] = {}
    projects = config.get("projects")
    if isinstance(projects, dict):
        resolved_dir = str(Path(project_dir).resolve())
        key = _normalise_path_for_config_key(resolved_dir)
        project_config = projects.get(key, {})
        if isinstance(project_config, dict):
            local_servers = _parse_stdio_servers(
                project_config.get("mcpServers")
            )

    # Merge: local wins on conflict
    merged = {**user_servers, **local_servers}
    if merged:
        logger.info(
            "Found %d stdio MCP server(s) for %s: %s",
            len(merged),
            project_dir,
            ", ".join(merged),
        )
    return merged
