"""Unit tests for the opencode MCP config + OAuth providers."""

from __future__ import annotations

import json

import pytest

from open_shrimp.backend.opencode.mcp_config import (
    OpenCodeMcpConfigProvider,
    OpenCodeMcpOAuthProvider,
)
from open_shrimp.config import ContextConfig


def _context(directory: str, mcp: dict | None = None) -> ContextConfig:
    return ContextConfig(
        directory=directory,
        description="t",
        allowed_tools=[],
        mcp=mcp or {},
    )


def _write_opencode_config(tmp_path, body, *, suffix=".json"):
    config_dir = tmp_path / "opencode_config" / "opencode"
    config_dir.mkdir(parents=True)
    config_file = config_dir / f"opencode{suffix}"
    if suffix == ".json":
        config_file.write_text(json.dumps(body), encoding="utf-8")
    else:
        # Write JSON5 / JSONC style content with a comment.
        config_file.write_text(
            "// trailing-comment config\n" + json.dumps(body),
            encoding="utf-8",
        )
    return config_dir.parent


def test_stdio_local_server_parsing(tmp_path, monkeypatch):
    cfg_root = _write_opencode_config(
        tmp_path,
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["fs-tool", "--root", "/tmp"],
                    "environment": {"DEBUG": "1"},
                },
                "disabled": {
                    "type": "local",
                    "command": ["nope"],
                    "enabled": False,
                },
                "remote-ignored": {
                    "type": "remote",
                    "url": "https://x",
                },
            },
        },
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))

    provider = OpenCodeMcpConfigProvider()
    servers = provider.stdio_servers(_context(str(tmp_path)))

    assert set(servers) == {"fs"}
    assert servers["fs"].command == "fs-tool"
    assert servers["fs"].args == ["--root", "/tmp"]
    assert servers["fs"].env == {"DEBUG": "1"}


def test_remote_http_server_parsing(tmp_path, monkeypatch):
    cfg_root = _write_opencode_config(
        tmp_path,
        {
            "mcp": {
                "figma": {
                    "type": "remote",
                    "url": "https://mcp.figma.com",
                    "headers": {"X-Custom": "v"},
                },
                "disabled": {
                    "type": "remote",
                    "url": "https://nope",
                    "enabled": False,
                },
            },
        },
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))

    provider = OpenCodeMcpConfigProvider()
    servers = provider.http_servers(_context(str(tmp_path)))

    assert set(servers) == {"figma"}
    assert servers["figma"].url == "https://mcp.figma.com"
    assert servers["figma"].transport == "http"
    assert servers["figma"].headers == {"X-Custom": "v"}


def test_jsonc_fallback(tmp_path, monkeypatch):
    cfg_root = _write_opencode_config(
        tmp_path,
        {
            "mcp": {
                "fs": {"type": "local", "command": ["fs-tool"]},
            },
        },
        suffix=".jsonc",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))

    provider = OpenCodeMcpConfigProvider()
    servers = provider.stdio_servers(_context(str(tmp_path)))
    assert "fs" in servers


def test_context_overlay_wins_on_conflict(tmp_path, monkeypatch):
    cfg_root = _write_opencode_config(
        tmp_path,
        {
            "mcp": {
                "shared": {"type": "local", "command": ["global-cmd"]},
            },
        },
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))

    overlay = {
        "shared": {
            "type": "stdio",
            "command": "overlay-cmd",
            "args": ["--from-overlay"],
        },
        "extra": {"type": "stdio", "command": "extra-cmd"},
    }
    provider = OpenCodeMcpConfigProvider()
    servers = provider.stdio_servers(_context(str(tmp_path), mcp=overlay))

    assert servers["shared"].command == "overlay-cmd"
    assert servers["shared"].args == ["--from-overlay"]
    assert servers["extra"].command == "extra-cmd"


def test_overlay_http_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing"))
    overlay = {
        "remote": {
            "type": "http",
            "url": "https://overlay.example.com",
            "headers": {"X-Tenant": "abc"},
        },
        "stream": {
            "type": "sse",
            "url": "https://overlay-stream.example.com",
        },
    }
    provider = OpenCodeMcpConfigProvider()
    servers = provider.http_servers(_context(str(tmp_path), mcp=overlay))

    assert servers["remote"].url == "https://overlay.example.com"
    assert servers["remote"].transport == "http"
    assert servers["stream"].transport == "sse"


def test_missing_config_returns_overlay_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing"))
    overlay = {"only": {"type": "stdio", "command": "only-cmd"}}
    provider = OpenCodeMcpConfigProvider()
    assert provider.stdio_servers(
        _context(str(tmp_path), mcp=overlay)
    ) == {
        "only": provider.stdio_servers(
            _context(str(tmp_path), mcp=overlay)
        )["only"]
    }


def test_oauth_get_match(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    (data_home / "opencode").mkdir(parents=True)
    (data_home / "opencode" / "mcp-auth.json").write_text(
        json.dumps(
            {
                "figma": {
                    "serverUrl": "https://mcp.figma.com",
                    "tokens": {
                        "accessToken": "tok-1",
                        "expiresAt": 1700000000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    provider = OpenCodeMcpOAuthProvider()
    cred = provider.get("figma", "https://mcp.figma.com")
    assert cred is not None
    assert cred.access_token == "tok-1"
    # Seconds converted to milliseconds.
    assert cred.expires_at_ms == 1700000000 * 1000


def test_oauth_get_url_mismatch_returns_none(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    (data_home / "opencode").mkdir(parents=True)
    (data_home / "opencode" / "mcp-auth.json").write_text(
        json.dumps(
            {
                "figma": {
                    "serverUrl": "https://mcp.figma.com",
                    "tokens": {"accessToken": "tok"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    provider = OpenCodeMcpOAuthProvider()
    assert provider.get("figma", "https://other.example.com") is None


def test_oauth_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nope"))
    provider = OpenCodeMcpOAuthProvider()
    assert provider.get("any", "https://any") is None
