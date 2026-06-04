"""Tests for container image auto-build."""

import json
from unittest.mock import patch, MagicMock

import pytest

from open_shrimp.sandbox.docker import DockerSandbox
from open_shrimp.sandbox.docker_helpers import (
    OPENCODE_GUEST_PORT,
    _build_docker_run_argv,
    ensure_image,
)
from open_shrimp.sandbox.opencode_plugins import APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.stdout = []
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0


def test_ensure_image_skips_when_image_exists():
    """When the image already exists, ensure_image does nothing."""
    with patch("open_shrimp.sandbox.docker_helpers.subprocess.run") as mock_run:
        # docker image inspect succeeds -> image exists
        mock_run.return_value = MagicMock(returncode=0)
        ensure_image()

    mock_run.assert_called_once()
    args = mock_run.call_args
    assert "image" in args[0][0]
    assert "inspect" in args[0][0]


def test_ensure_image_builds_when_missing(tmp_path):
    """When the image is missing, ensure_image builds it."""
    # Create a fake opencode binary to be "found"
    fake_binary = tmp_path / "opencode"
    fake_binary.write_bytes(b"#!/bin/sh\necho fake")
    fake_binary.chmod(0o755)

    # Mock subprocess.run for the inspect check (returns failure)
    mock_run = MagicMock(return_value=MagicMock(returncode=1))

    # Mock subprocess.Popen for the build (returns success)
    mock_stdout = MagicMock()
    mock_stdout.__iter__ = MagicMock(return_value=iter(["Step 1/8\n"]))
    mock_process = MagicMock(returncode=0, stdout=mock_stdout)
    mock_process.wait.return_value = 0
    mock_popen = MagicMock(return_value=mock_process)

    with (
        patch("open_shrimp.sandbox.docker_helpers.subprocess.run", mock_run),
        patch("open_shrimp.sandbox.docker_helpers.subprocess.Popen", mock_popen),
        patch("open_shrimp.sandbox.docker_helpers._find_opencode_binary", return_value=str(fake_binary)),
    ):
        ensure_image()

    # Should have called inspect via run, then build via Popen
    mock_run.assert_called_once()
    assert "inspect" in mock_run.call_args[0][0]
    mock_popen.assert_called_once()
    assert "build" in mock_popen.call_args[0][0]


def test_ensure_image_raises_on_build_failure(tmp_path):
    """When docker build fails, ensure_image raises RuntimeError."""
    fake_binary = tmp_path / "opencode"
    fake_binary.write_bytes(b"#!/bin/sh\necho fake")
    fake_binary.chmod(0o755)

    # Mock subprocess.run for the inspect check (returns failure)
    mock_run = MagicMock(return_value=MagicMock(returncode=1))

    # Mock subprocess.Popen for the build (returns failure)
    mock_stdout = MagicMock()
    mock_stdout.__iter__ = MagicMock(
        return_value=iter(["Step 2/8 : RUN apt-get update\n", "E: Failed to fetch\n"])
    )
    mock_process = MagicMock(stdout=mock_stdout)
    mock_process.wait.return_value = 1
    mock_popen = MagicMock(return_value=mock_process)

    with (
        patch("open_shrimp.sandbox.docker_helpers.subprocess.run", mock_run),
        patch("open_shrimp.sandbox.docker_helpers.subprocess.Popen", mock_popen),
        patch("open_shrimp.sandbox.docker_helpers._find_opencode_binary", return_value=str(fake_binary)),
    ):
        with pytest.raises(RuntimeError, match="Failed to build"):
            ensure_image()


def test_docker_run_mounts_opencode_home_and_port(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "open_shrimp.sandbox.docker_helpers.container_state_dir",
        lambda: tmp_path / "containers",
    )
    with patch(
        "open_shrimp.sandbox.docker_helpers.subprocess.check_output",
        side_effect=FileNotFoundError,
    ):
        argv, _ = _build_docker_run_argv(
            context_name="dev",
            project_dir="/workspace/project",
        )

    joined = "\n".join(argv)
    assert f"{tmp_path}/containers/dev/opencode-home:/home/openshrimp/.local/share/opencode" in joined
    assert f"{tmp_path}/containers/dev/openshrimp-data:/home/openshrimp/.local/share/openshrimp" in joined
    assert f"{tmp_path}/containers/dev:/home/openshrimp/.claude" not in joined
    assert "-p" in argv
    assert f"127.0.0.1::{OPENCODE_GUEST_PORT}" in argv


def test_docker_run_mounts_global_skill_dirs(tmp_path, monkeypatch):
    legacy_skills = tmp_path / ".claude" / "skills"
    agents_skills = tmp_path / ".agents" / "skills"
    opencode_skills = tmp_path / ".config" / "opencode" / "skills"
    opencode_skill = tmp_path / ".config" / "opencode" / "skill"
    for path in (legacy_skills, agents_skills, opencode_skills, opencode_skill):
        path.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        "open_shrimp.sandbox.docker_helpers.container_state_dir",
        lambda: tmp_path / "containers",
    )

    with patch(
        "open_shrimp.sandbox.docker_helpers.subprocess.check_output",
        side_effect=FileNotFoundError,
    ):
        argv, _ = _build_docker_run_argv(
            context_name="dev",
            project_dir="/workspace/project",
        )

    joined = "\n".join(argv)
    assert f"type=bind,source={legacy_skills},target=/home/openshrimp/.claude/skills,readonly" in joined
    assert f"type=bind,source={agents_skills},target=/home/openshrimp/.agents/skills,readonly" in joined
    assert f"type=bind,source={opencode_skills},target=/home/openshrimp/.config/opencode/skills,readonly" in joined
    assert f"type=bind,source={opencode_skill},target=/home/openshrimp/.config/opencode/skill,readonly" in joined


def test_docker_opencode_server_writes_plugin_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "open_shrimp.sandbox.docker_helpers.container_state_dir",
        lambda: tmp_path / "containers",
    )
    procs: list[FakeProc] = []

    def fake_popen(args, **kwargs):
        proc = FakeProc(args)
        procs.append(proc)
        return proc

    with (
        patch("open_shrimp.sandbox.docker._get_opencode_host_port", return_value=49154),
        patch("open_shrimp.sandbox.docker._sync_opencode_auth") as sync_auth,
        patch("open_shrimp.sandbox.docker._wait_for_opencode_ready"),
        patch("open_shrimp.sandbox.docker._drain_opencode_output"),
        patch("open_shrimp.sandbox.docker.subprocess.Popen", side_effect=fake_popen),
    ):
        sandbox = DockerSandbox("dev", "/workspace/project")
        endpoint = sandbox.ensure_opencode_server(provider_id="openai")

    assert endpoint.base_url == "http://127.0.0.1:49154"
    sync_auth.assert_called_once_with("openai", tmp_path / "containers" / "dev" / "opencode-home")
    assert "OPENCODE_CONFIG=/home/openshrimp/.local/share/openshrimp/managed-opencode/plugin-config.json" in procs[0].args
    assert not any(arg.startswith("XDG_CONFIG_HOME=") for arg in procs[0].args)
    config = tmp_path / "containers" / "dev" / "openshrimp-data" / "managed-opencode" / "plugin-config.json"
    assert json.loads(config.read_text(encoding="utf-8"))["plugin"] == [
        APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN
    ]
