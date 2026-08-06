"""HCS guest layout derived from the agent runtime rather than a hardcoded
agent name, plus the guest-path contract of ``copy_files_in``.

No guest: the control channel is a fake that satisfies every ``expect``
marker, and the launcher build is stubbed out (it shells to ``csc``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

import pytest

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import hcs as hcs_mod
from open_shrimp.sandbox import hcs_helpers as H
from open_shrimp.sandbox.agent_runtime import (
    AgentRuntime,
    HomeMount,
    ImageBundle,
    WrappedCLI,
)
from open_shrimp.sandbox.hcs import HcsSandbox, _chroot_agent_home


def _bundle(**overrides) -> ImageBundle:
    defaults: dict = {
        "tag_suffix": "claude",
        "bundled_dockerfile": "Dockerfile.claude",
        "binary_finder": lambda: "/usr/bin/claude",
        "context_binary_name": "claude",
        "build_arg": ("CLAUDE_CLI", "claude"),
        "guest_home": "/home/claude",
    }
    defaults.update(overrides)
    return ImageBundle(**defaults)


def _runtime(*, guest_dir: str, bundle: ImageBundle | None = None) -> AgentRuntime:
    return AgentRuntime(
        name="test-agent",
        home_mount=HomeMount(
            host_dir=Path("/host/home"), guest_dir=guest_dir,
            holds_session_state=True,
        ),
        inject=lambda home: None,
        env={},
        launch=WrappedCLI(),
        image_bundle=bundle if bundle is not None else _bundle(),
    )


def _claude_runtime() -> AgentRuntime:
    return _runtime(guest_dir="/home/claude/.claude")


def _opencode_runtime() -> AgentRuntime:
    return _runtime(
        guest_dir="/home/claude/.local/share/opencode",
        bundle=_bundle(context_binary_name="opencode", tag_suffix="opencode"),
    )


def _make_sandbox(tmp_path, monkeypatch, runtime=None, **config_extra):
    monkeypatch.setattr(sys, "platform", "win32")
    defaults: dict = {
        "backend": "hcs",
        "base_image": str(tmp_path / "root.vhdx"),
    }
    defaults.update(config_extra)
    sb = HcsSandbox(
        "default", SandboxConfig(**defaults), str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
        runtime=runtime,
    )
    sb._runtime_id = "11111111-2222-3333-4444-555555555555"
    return sb


# -- the home path the chroot binds ------------------------------------------


def test_claude_home_keeps_its_established_chroot_path():
    # The guest layout that ships today must not move.
    assert _chroot_agent_home(_claude_runtime()) == "/root/.claude"


def test_a_runtime_less_sandbox_falls_back_to_the_shipped_layout():
    assert _chroot_agent_home(None) == "/root/.claude"


def test_an_xdg_shaped_home_keeps_its_tail_under_the_chroot_home():
    # Re-rooting the home-relative tail (not its basename) is what keeps
    # ``$HOME/.local/share/opencode`` resolvable from HOME=/root.
    assert (
        _chroot_agent_home(_opencode_runtime())
        == "/root/.local/share/opencode"
    )


def test_the_chroot_home_matches_the_HOME_every_guest_command_gets():
    assert _chroot_agent_home(_claude_runtime()).startswith(H.CHROOT_HOME)


def test_a_home_outside_the_bundle_guest_home_is_refused():
    rt = _runtime(guest_dir="/opt/elsewhere/.agent")
    with pytest.raises(RuntimeError, match="not under its image bundle"):
        _chroot_agent_home(rt)


# -- the bind the provision pass installs ------------------------------------


class _FakeChannel:
    """Control channel that satisfies every ``expect`` marker the provision
    pass checks, recording the commands it was given."""

    def __init__(self, *args, **kwargs):
        self.commands: list[str] = []

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        return True, (
            "MOUNT-OK ROOT-MOUNT-OK GUI-MOUNT-OK PV-OK PROVISION-DONE"
        )


def _provision(tmp_path, monkeypatch, runtime, **config_extra) -> list[str]:
    sb = _make_sandbox(tmp_path, monkeypatch, runtime=runtime, **config_extra)
    fake_win = type(sys)("open_shrimp.sandbox.hcs_win")
    chan = _FakeChannel()
    fake_win.ControlChannel = lambda *a, **kw: chan
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", fake_win)
    monkeypatch.setattr(sb, "_start_exec_agent", lambda c: None)
    monkeypatch.setattr(sb, "_configure_network", lambda c: None)
    sb._provision_guest(log_file=None)
    return chan.commands


def _home_bind(commands: list[str]) -> str:
    binds = [c for c in commands if f"mount -o bind {H.MNT_HOME} " in c]
    assert len(binds) == 1, commands
    return binds[0]


def test_the_home_share_binds_at_the_claude_path_it_always_has(
    tmp_path, monkeypatch,
):
    commands = _provision(tmp_path, monkeypatch, _claude_runtime())
    assert f"{H.MNT_ROOT}/root/.claude" in _home_bind(commands)


def test_the_home_share_binds_where_the_runtime_says(tmp_path, monkeypatch):
    commands = _provision(tmp_path, monkeypatch, _opencode_runtime())
    bind = _home_bind(commands)
    assert f"{H.MNT_ROOT}/root/.local/share/opencode" in bind
    assert ".claude" not in bind


def test_every_chroot_command_exports_the_same_HOME(tmp_path, monkeypatch):
    commands = _provision(
        tmp_path, monkeypatch, _opencode_runtime(), provision="echo hi",
    )
    homes = [c for c in commands if "HOME=" in c]
    assert homes
    for cmd in homes:
        assert f"HOME={H.CHROOT_HOME} " in cmd


# -- the argv the launcher execs ---------------------------------------------


def _launch_cfg(tmp_path, monkeypatch, runtime) -> dict:
    sb = _make_sandbox(tmp_path, monkeypatch, runtime=runtime)
    sb._sdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        sb, "_build_launcher_exe", lambda *, launch_json, exe: exe,
    )
    sb.build_cli_wrapper()
    return json.loads(sb._launch_json_file().read_text(encoding="utf-8"))


def test_argv_prefix_comes_from_the_bundle(tmp_path, monkeypatch):
    cfg = _launch_cfg(tmp_path, monkeypatch, _claude_runtime())
    assert cfg["argv_prefix"] == ["claude"]
    assert cfg["env"]["HOME"] == H.CHROOT_HOME

    cfg = _launch_cfg(tmp_path, monkeypatch, _opencode_runtime())
    assert cfg["argv_prefix"] == ["opencode"]


def test_argv_prefix_falls_back_when_no_runtime_is_bound(tmp_path, monkeypatch):
    cfg = _launch_cfg(tmp_path, monkeypatch, None)
    assert cfg["argv_prefix"] == ["claude"]


# -- copy_files_in returns guest paths ---------------------------------------


@pytest.mark.asyncio
async def test_copy_files_in_returns_guest_posix_paths(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch)
    src = tmp_path / "note.txt"
    src.write_text("hi", encoding="utf-8")

    result = await sb.copy_files_in([src])

    guest_uploads = (
        PurePosixPath(sb._guest_workspace()) / ".openshrimp-uploads"
    )
    assert result == [guest_uploads / "note.txt"]
    assert all(type(p) is PurePosixPath for p in result)
    # The file really landed in the 9p-shared workspace dir.
    assert (
        Path(sb._project_dir) / ".openshrimp-uploads" / "note.txt"
    ).read_text(encoding="utf-8") == "hi"


@pytest.mark.asyncio
async def test_copy_files_in_returns_guest_paths_even_when_staging_fails(
    tmp_path, monkeypatch,
):
    sb = _make_sandbox(tmp_path, monkeypatch)
    missing = tmp_path / "gone.txt"

    result = await sb.copy_files_in([missing])

    # A host path here would name something the guest cannot see.
    guest_uploads = (
        PurePosixPath(sb._guest_workspace()) / ".openshrimp-uploads"
    )
    assert result == [guest_uploads / "gone.txt"]
    assert type(result[0]) is PurePosixPath


@pytest.mark.asyncio
async def test_copy_files_in_is_empty_for_no_attachments(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch)
    assert await sb.copy_files_in([]) == []
