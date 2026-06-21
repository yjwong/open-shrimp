"""Unit tests for the claude_sdk MCP config + OAuth providers."""

from __future__ import annotations

import json
import sys

import pytest

from open_shrimp.backend.claude_sdk.mcp_config import (
    ClaudeMcpConfigProvider,
    ClaudeMcpOAuthProvider,
)
from open_shrimp.config import ContextConfig


def _context(directory: str) -> ContextConfig:
    return ContextConfig(
        directory=directory,
        description="t",
        allowed_tools=[],
    )


def _write_claude_config(tmp_path, body):
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir()
    config_file = config_dir / ".claude.json"
    config_file.write_text(json.dumps(body), encoding="utf-8")
    return config_dir


def test_stdio_servers_user_scope(tmp_path, monkeypatch):
    config_dir = _write_claude_config(
        tmp_path,
        {
            "mcpServers": {
                "global-tool": {
                    "command": "global-cmd",
                    "args": ["--flag"],
                    "env": {"FOO": "bar"},
                },
            },
        },
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    provider = ClaudeMcpConfigProvider()
    servers = provider.stdio_servers(_context(str(tmp_path)))

    assert "global-tool" in servers
    assert servers["global-tool"].command == "global-cmd"
    assert servers["global-tool"].args == ["--flag"]
    assert servers["global-tool"].env == {"FOO": "bar"}


def test_stdio_servers_local_scope_overrides_user(tmp_path, monkeypatch):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    key = str(project_dir.resolve())
    config_dir = _write_claude_config(
        tmp_path,
        {
            "mcpServers": {
                "shared": {"command": "user-cmd"},
            },
            "projects": {
                key: {
                    "mcpServers": {
                        "shared": {"command": "local-cmd"},
                        "extra": {"command": "extra-cmd"},
                    },
                },
            },
        },
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    provider = ClaudeMcpConfigProvider()
    servers = provider.stdio_servers(_context(str(project_dir)))

    assert servers["shared"].command == "local-cmd"
    assert servers["extra"].command == "extra-cmd"


def test_http_servers_parses_url_and_headers(tmp_path, monkeypatch):
    config_dir = _write_claude_config(
        tmp_path,
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://mcp.example.com",
                    "headers": {"X-Custom": "value"},
                },
                "stream": {
                    "type": "sse",
                    "url": "https://stream.example.com",
                },
                "stdio-ignored": {"command": "noop"},
            },
        },
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    provider = ClaudeMcpConfigProvider()
    servers = provider.http_servers(_context(str(tmp_path)))

    assert set(servers) == {"remote", "stream"}
    assert servers["remote"].url == "https://mcp.example.com"
    assert servers["remote"].transport == "http"
    assert servers["remote"].headers == {"X-Custom": "value"}
    assert servers["stream"].transport == "sse"


def test_env_var_expansion(tmp_path, monkeypatch):
    config_dir = _write_claude_config(
        tmp_path,
        {
            "mcpServers": {
                "with-env": {
                    "command": "${MY_CMD}",
                    "args": ["--token", "${MY_TOKEN:-fallback}"],
                    "env": {"BASE": "${MY_BASE}"},
                },
            },
        },
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MY_CMD", "expanded-cmd")
    monkeypatch.setenv("MY_BASE", "expanded-base")
    monkeypatch.delenv("MY_TOKEN", raising=False)

    provider = ClaudeMcpConfigProvider()
    servers = provider.stdio_servers(_context(str(tmp_path)))

    assert servers["with-env"].command == "expanded-cmd"
    assert servers["with-env"].args == ["--token", "fallback"]
    assert servers["with-env"].env == {"BASE": "expanded-base"}


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))

    provider = ClaudeMcpConfigProvider()
    assert provider.stdio_servers(_context(str(tmp_path))) == {}
    assert provider.http_servers(_context(str(tmp_path))) == {}


def test_oauth_get_match(tmp_path, monkeypatch):
    # Force the file-based path even on macOS hosts so the test is portable.
    monkeypatch.setattr(sys, "platform", "linux")
    creds_dir = tmp_path / "claude_config"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "mcpOAuth": {
                    "figma|abcd": {
                        "serverName": "figma",
                        "serverUrl": "https://mcp.figma.com",
                        "accessToken": "tok-1",
                        "expiresAt": 9_999_999_999_000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(creds_dir))

    provider = ClaudeMcpOAuthProvider()
    cred = provider.get("figma", "https://mcp.figma.com")
    assert cred is not None
    assert cred.access_token == "tok-1"
    assert cred.expires_at_ms == 9_999_999_999_000


def test_oauth_get_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    creds_dir = tmp_path / "claude_config"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text(
        json.dumps({"mcpOAuth": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(creds_dir))

    provider = ClaudeMcpOAuthProvider()
    assert provider.get("nope", "https://nope") is None


def test_oauth_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))

    provider = ClaudeMcpOAuthProvider()
    assert provider.get("any", "https://any") is None
