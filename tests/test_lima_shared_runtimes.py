"""One Lima instance hosting both agent backends at once.

Lima fixes the guest's mount set when the VM boots and merges mount entries by
``location``, so the plan is a union over every registered runtime with one
guest path per host dir: the second agent's task-output path arrives as a
symlink rather than a second mount, and gaining a runtime rewrites the
instance's mount list and restarts it instead of deleting the VM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from open_shrimp import paths
from open_shrimp.backend.claude_sdk.runtime import claude_runtime
from open_shrimp.backend.opencode.runtime import opencode_runtime
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import lima_helpers
from open_shrimp.sandbox.lima import LimaSandbox
from open_shrimp.sandbox.lima_helpers import LIMA_GUEST_UID


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch):
    """Keep the runtime factories' per-context mkdirs inside tmp_path."""
    monkeypatch.setattr(paths, "_data_dir", tmp_path / "data")
    # The skills share is only mounted when the host has one; pin it absent so
    # the plan does not depend on the developer's own ~/.claude.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    yield


def _claude(tmp_path: Path) -> Any:
    return claude_runtime(tmp_path / "vm" / "claude-home")


def _opencode() -> Any:
    return opencode_runtime(context_name="dev", provider_id=None)


def _sandbox(tmp_path: Path, *runtimes: Any) -> LimaSandbox:
    sb = object.__new__(LimaSandbox)
    sb._context_name = "dev"
    sb._config = SandboxConfig(backend="lima")
    sb._project_dir = str(tmp_path / "project")
    sb._additional_directories = []
    sb._computer_use = False
    sb._guest_os = "linux"
    sb._limactl = "limactl"
    sb._inst_name = "openshrimp-dev"
    sb._sdir = tmp_path / "vm"
    sb._tmp_dir = sb._sdir / "tmp"
    sb._claude_home_dir = sb._sdir / "claude-home"
    sb._runtimes = {r.name: r for r in runtimes}
    return sb


def _mounts(sb: LimaSandbox) -> dict[str, dict]:
    """The generated mount entries, keyed by host location."""
    return {m["location"]: m for m in sb._template()["mounts"]}


def test_the_task_output_dir_mounts_at_one_path_whoever_registered_first(
    tmp_path,
):
    """Claude writes background-task output to /tmp/claude-<uid>, OpenCode to
    /tmp/openshrimp-<uid>, and Lima gives the one host dir behind both a single
    guest path.  Which one it is cannot depend on registration order, because
    that is whichever ChatScope dispatched first and a change of mind rebuilds
    the guest."""
    claude_first = _sandbox(tmp_path, _claude(tmp_path), _opencode())
    opencode_first = _sandbox(tmp_path, _opencode(), _claude(tmp_path))

    assert claude_first._task_tmp_guest_paths() == [
        f"/tmp/claude-{LIMA_GUEST_UID}",
        f"/tmp/openshrimp-{LIMA_GUEST_UID}",
    ]
    assert (
        opencode_first._task_tmp_guest_paths()
        == claude_first._task_tmp_guest_paths()
    )
    assert _mounts(claude_first) == _mounts(opencode_first)


def test_a_sandbox_with_no_runtime_keeps_the_default_task_tmp_path(tmp_path):
    """``create_sandbox`` passes ``runtimes=[]`` when no runtime is resolved."""
    sb = _sandbox(tmp_path)

    assert sb._task_tmp_guest_paths() == [f"/tmp/claude-{LIMA_GUEST_UID}"]
    assert _mounts(sb)[str(sb._tmp_dir)]["mountPoint"] == (
        f"/tmp/claude-{LIMA_GUEST_UID}"
    )


def test_both_runtimes_get_their_own_home_in_one_guest(tmp_path):
    """The wrapped-CLI runtime's home is the claude-home share; the served
    runtime's comes from its launch's home mounts.  Neither displaces the
    other."""
    opencode = _opencode()
    sb = _sandbox(tmp_path, _claude(tmp_path), opencode)

    mounts = _mounts(sb)

    assert mounts[str(sb._claude_home_dir)]["mountPoint"].endswith("/.claude")
    for mount in opencode.launch.home_mounts:
        assert mounts[str(mount.host_dir)]["mountPoint"] == mount.guest_mount_point


def test_the_second_agents_task_output_path_is_symlinked_onto_the_mounted_one(
    tmp_path, monkeypatch,
):
    """Lima merges mount entries by location, so the host dir cannot be
    mounted twice; without the link OpenCode writes to guest-local disk and
    "View output" finds nothing."""
    sb = _sandbox(tmp_path, _claude(tmp_path), _opencode())
    commands: list[str] = []
    monkeypatch.setattr(
        LimaSandbox,
        "_exec_in_vm_sync",
        lambda self, cmd, **kw: (commands.append(cmd), (0, "", ""))[1],
    )

    sb._link_task_tmp_aliases()

    assert len(commands) == 1
    assert (
        f"ln -sfn /tmp/claude-{LIMA_GUEST_UID} /tmp/openshrimp-{LIMA_GUEST_UID}"
        in commands[0]
    )


def test_one_runtime_needs_no_link(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path, _claude(tmp_path))
    monkeypatch.setattr(
        LimaSandbox,
        "_exec_in_vm_sync",
        lambda self, cmd, **kw: pytest.fail(f"unexpected guest command: {cmd}"),
    )

    sb._link_task_tmp_aliases()


# -- drift ----------------------------------------------------------------


def _fingerprints(sb: LimaSandbox) -> lima_helpers.Fingerprints:
    return lima_helpers.config_fingerprints(sb._template())


def test_gaining_a_runtime_moves_only_the_mounts(tmp_path):
    """The mount-free fingerprint is what decides between a restart and a
    rebuild, so a second runtime must move the whole fingerprint and leave
    that one alone."""
    before = _fingerprints(_sandbox(tmp_path, _claude(tmp_path)))
    after = _fingerprints(_sandbox(tmp_path, _claude(tmp_path), _opencode()))

    assert after.whole != before.whole
    assert after.mount_free == before.mount_free


def test_more_memory_moves_the_mount_free_fingerprint_too(tmp_path):
    """A config change outside the mount set still rebuilds the VM."""
    before = _sandbox(tmp_path, _claude(tmp_path))
    after = _sandbox(tmp_path, _claude(tmp_path))
    after._config = SandboxConfig(backend="lima", memory=before._config.memory * 2)

    assert _fingerprints(after).mount_free != _fingerprints(before).mount_free


def test_a_remount_rewrites_the_instance_config_and_stops_the_vm(
    tmp_path, monkeypatch,
):
    """Lima has no hot-add: the new shares arrive when the VM next starts, so
    the instance keeps its disk and the CLIs installed on it."""
    lima_home = tmp_path / "lima-home"
    inst_dir = lima_home / "openshrimp-dev"
    inst_dir.mkdir(parents=True)
    # What ``limactl create`` resolved into the instance config, which the
    # rewrite must leave alone.
    (inst_dir / "lima.yaml").write_text(
        yaml.dump({"cpus": 4, "ssh": {"localPort": 60022}, "mounts": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(lima_helpers, "_lima_state_dir", lambda: lima_home)
    stopped: list[str] = []
    monkeypatch.setattr(
        "open_shrimp.sandbox.lima.limactl_stop",
        lambda limactl, name: stopped.append(name),
    )
    monkeypatch.setattr(
        "open_shrimp.sandbox.lima.limactl_instance_status",
        lambda limactl, name: "Running",
    )
    sb = _sandbox(tmp_path, _claude(tmp_path), _opencode())

    assert sb._remount(sb._template()) is True

    instance = yaml.safe_load((inst_dir / "lima.yaml").read_text())
    assert instance["ssh"] == {"localPort": 60022}
    assert {m["location"] for m in instance["mounts"]} == set(_mounts(sb))
    assert stopped == ["openshrimp-dev"]


def test_a_remount_without_an_instance_falls_back_to_a_rebuild(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "open_shrimp.sandbox.lima.limactl_instance_status",
        lambda limactl, name: None,
    )
    sb = _sandbox(tmp_path, _claude(tmp_path), _opencode())

    assert sb._remount(sb._template()) is False
