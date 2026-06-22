"""Tests for ``StdioManager.restart_server`` subprocess restart semantics."""

from __future__ import annotations

import sys

import pytest

from open_shrimp.mcp_proxy.stdio_manager import StdioManager
from open_shrimp.mcp_proxy.types import StdioServerConfig

pytestmark = pytest.mark.asyncio


def _sleeper() -> StdioServerConfig:
    # A process that just blocks on stdin forever — stands in for a live
    # but idle MCP server.
    return StdioServerConfig(
        command=sys.executable,
        args=["-c", "import sys; sys.stdin.read()"],
        env={},
    )


async def test_restart_terminates_and_allows_respawn() -> None:
    mgr = StdioManager()
    try:
        first = await mgr.get_or_spawn("ctx", "srv", _sleeper())
        assert first.alive
        first_pid = first.process.pid

        restarted = await mgr.restart_server("ctx", "srv")
        assert restarted is True
        assert first.process.returncode is not None

        second = await mgr.get_or_spawn("ctx", "srv", _sleeper())
        assert second.alive
        assert second.process.pid != first_pid
    finally:
        await mgr.stop_all()


async def test_restart_missing_server_is_noop() -> None:
    mgr = StdioManager()
    assert await mgr.restart_server("ctx", "never-spawned") is False
