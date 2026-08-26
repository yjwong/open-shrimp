"""Which providers the opencode CLI holds credentials for on this host.

``opencode auth login`` writes ``~/.local/share/opencode/auth.json`` and
``opencode auth list`` prints a box-drawn TUI, so the file is the only
machine-readable answer to "is OpenAI connected here".

One reader of it, not two: the sandbox runtime injects a single provider's
entry into a guest and the setup wizards ask whether a login is still owed, and
a wizard that looked in a different place from the sandbox would offer a login
for a credential that is already there.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def host_data_dir() -> Path:
    """Where the opencode CLI keeps its own state on this host.

    One resolution of opencode's XDG data home, so a wizard asking whether a
    provider is connected, a sandbox start injecting that provider, and the MCP
    config reader cannot each look somewhere slightly different.
    """
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return root / "opencode"


def host_auth_path() -> Path:
    """Where the opencode CLI keeps what its logins wrote."""
    return host_data_dir() / "auth.json"


def connected_providers() -> dict[str, str | None]:
    """Provider id → how it authenticates (``api``, ``oauth``), or ``None``.

    Empty for a host that has never logged in, and for a file that cannot be
    read or parsed: a credential nobody can read authenticates nothing, and the
    caller's next move — offer the login — is the same for both.

    The value is what a UI shows beside the provider, so an entry whose shape
    the CLI has since changed maps to ``None`` rather than dropping the
    provider: the entry is still a credential, and its type is the only part
    that went unrecognised.
    """
    path = host_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read the opencode auth file %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: its root is not an object", path)
        return {}

    return {
        str(provider): entry["type"] if isinstance(entry.get("type"), str) else None
        for provider, entry in data.items()
        if isinstance(entry, dict)
    }


__all__ = ["connected_providers", "host_auth_path", "host_data_dir"]
