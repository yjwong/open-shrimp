"""OpenCode plugin configuration used by OpenShrimp-managed sessions."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import json5


APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN = (
    "@openshrimp/opencode-apply-patch-large-delete-guard"
)

logger = logging.getLogger(__name__)


def _host_opencode_config_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "opencode"
    return Path.home() / ".config" / "opencode"


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _host_provider_config() -> dict[str, Any] | None:
    provider: dict[str, Any] = {}
    for filename in ("config.json", "opencode.json", "opencode.jsonc"):
        path = _host_opencode_config_dir() / filename
        if not path.is_file():
            continue
        try:
            data = json5.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read OpenCode config %s", path, exc_info=True)
            continue
        if not isinstance(data, dict):
            continue
        next_provider = data.get("provider")
        if isinstance(next_provider, dict):
            provider = _merge_dicts(provider, next_provider)
    return provider or None


def ensure_opencode_plugin_config(openshrimp_data_dir: Path) -> Path:
    """Write an OpenCode config file that loads OpenShrimp's npm plugins.

    Returns the host-side config file path. OpenShrimp points OpenCode at this
    explicit file via ``OPENCODE_CONFIG`` rather than placing it in OpenCode's
    default config tree.
    """
    config_dir = openshrimp_data_dir / "managed-opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "plugin-config.json"
    config = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN],
    }
    provider = _host_provider_config()
    if provider:
        config["provider"] = provider
    config_source = json.dumps(config, indent=2) + "\n"
    if (
        not config_path.exists()
        or config_path.read_text(encoding="utf-8") != config_source
    ):
        config_path.write_text(config_source, encoding="utf-8")
    return config_path
