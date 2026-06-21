"""Tests for the AgentRuntime profile and the WrappedCLI start_agent dispatch."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from open_shrimp.backend.claude_sdk.runtime import claude_runtime
from open_shrimp.sandbox.agent_runtime import (
    AgentHandle,
    WrappedCLI,
)
from open_shrimp.sandbox.docker import DockerSandbox
from open_shrimp.sandbox.libvirt import LibvirtSandbox
from open_shrimp.sandbox.lima import LimaSandbox


def test_claude_runtime_profile(tmp_path: Path):
    """The Claude runtime declares a session-state home and a WrappedCLI launch."""
    rt = claude_runtime(tmp_path / "claude-home")

    assert rt.name == "claude"
    assert rt.home_mount.host_dir == tmp_path / "claude-home"
    assert rt.home_mount.holds_session_state is True
    assert isinstance(rt.launch, WrappedCLI)
    assert rt.launch.kind == "wrapped_cli"


def test_claude_runtime_inject_copies_credentials(tmp_path: Path, monkeypatch):
    """The inject hook copies host credentials into the target home dir."""
    fake_home = tmp_path / "host"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text('{"t": 1}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    rt = claude_runtime(tmp_path / "claude-home")
    target = tmp_path / "guest-home"
    rt.inject(target)

    assert (target / ".credentials.json").read_text() == '{"t": 1}'


def test_claude_runtime_env_forwards_api_key(tmp_path: Path, monkeypatch):
    """ANTHROPIC_API_KEY is declared on the runtime env when set on the host."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rt = claude_runtime(tmp_path)
    assert rt.env["ANTHROPIC_API_KEY"] == "sk-test"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rt2 = claude_runtime(tmp_path)
    assert "ANTHROPIC_API_KEY" not in rt2.env


@pytest.mark.parametrize("cls", [DockerSandbox, LibvirtSandbox, LimaSandbox])
def test_start_agent_wrapped_cli_returns_handle(cls, tmp_path: Path):
    """start_agent dispatches WrappedCLI to build_cli_wrapper and wraps the result."""
    sb = object.__new__(cls)
    sb.build_cli_wrapper = MagicMock(return_value=("/tmp/wrapper.sh", ["/tmp/wrapper.sh"]))

    rt = claude_runtime(tmp_path)
    handle = sb.start_agent(rt)

    assert isinstance(handle, AgentHandle)
    assert handle.cli_path == "/tmp/wrapper.sh"
    assert handle.cleanup_paths == ["/tmp/wrapper.sh"]
    sb.build_cli_wrapper.assert_called_once()
