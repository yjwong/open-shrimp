"""OpenCode plugin configuration used by OpenShrimp-managed sessions."""

from __future__ import annotations

import json
from pathlib import Path


APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN = (
    "@openshrimp/opencode-apply-patch-large-delete-guard"
)


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
    config_source = json.dumps(config, indent=2) + "\n"
    if (
        not config_path.exists()
        or config_path.read_text(encoding="utf-8") != config_source
    ):
        config_path.write_text(config_source, encoding="utf-8")
    return config_path
