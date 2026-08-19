"""Stand-ins for driving the config Mini App endpoints without Starlette.

The endpoints in ``config_app.api`` read four things off the request —
``app.state.config``, ``app.state.config_path``, ``headers`` and
``json()`` — so a namespace carrying those is enough to call them
directly.  Shared rather than copied per test module so that adding a
fifth is one edit, and so the monkeypatch of the private
``_authenticate`` name lives in one place.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from open_shrimp.config_app import api as config_api


def disable_auth(monkeypatch: Any) -> None:
    """Make every endpoint treat the caller as an authorised user."""

    async def fake_auth(request: Any) -> int:
        return 1

    monkeypatch.setattr(config_api, "_authenticate", fake_auth)


def make_request(config: Any, config_path: Any, body: Any = None) -> Any:
    """A request object carrying *config*, *config_path* and *body*."""

    async def _json() -> Any:
        return body

    return types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(
                config=config, config_path=str(config_path)
            ),
        ),
        headers={},
        json=_json,
    )


def write_config(tmp_path: Path, *, mcp: bool = False) -> Path:
    """Write a minimal config file, optionally with a per-context ``mcp``."""
    mcp_block = (
        "    mcp:\n"
        "      playwright:\n"
        "        command: npx\n"
        "        args:\n"
        "          - '@playwright/mcp'\n"
        if mcp
        else ""
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "telegram:\n"
        "  token: t\n"
        "allowed_users:\n"
        "  - 1\n"
        "contexts:\n"
        "  default:\n"
        "    directory: /tmp\n"
        "    description: d\n"
        "    allowed_tools: []\n"
        f"{mcp_block}"
        "default_context: default\n",
        encoding="utf-8",
    )
    return config_file
