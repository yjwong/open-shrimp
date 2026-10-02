"""One Lima instance hosting both agent backends at once.

Lima fixes the guest's mount set when the VM boots and merges mount entries by
``location``, so the plan is a union over every registered runtime with one
guest path per host dir: the second agent's task-output path arrives as a
symlink rather than a second mount, and gaining a runtime rewrites the
instance's mount list and restarts it instead of deleting the VM.

That plan is settled against ``LIMA_HOME/<instance>/lima.yaml`` rather than a
saved hash of the last one written, because a hash cannot say which runtimes
the *running* guest was booted for — and a bot process that restarts against a
running guest registers its runtimes one dispatch at a time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from open_shrimp import paths
from open_shrimp.backend.claude_sdk.runtime import claude_runtime
from open_shrimp.backend.opencode.runtime import opencode_runtime
from open_shrimp.sandbox.agent_runtime import (
    AgentRuntime,
    HomeMount,
    ImageBundle,
    WrappedCLI,
)
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import lima_helpers
from open_shrimp.sandbox.lima import LimaSandbox
from open_shrimp.sandbox.lima_helpers import LIMA_GUEST_UID, lima_guest_home
from open_shrimp.sandbox.skill_paths import SANDBOX_HOME


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
    sb._runtimes = {r.name: r for r in runtimes}
    return sb


def _mounts(sb: LimaSandbox) -> dict[str, dict]:
    """The generated mount entries, keyed by host location."""
    return {m["location"]: m for m in sb._template()["mounts"]}


def _mounts_for(tmp_path: Path, *runtimes: Any) -> list[dict]:
    """The mount set a guest booted for *runtimes* carries."""
    return _sandbox(tmp_path, *runtimes)._template()["mounts"]


def _instance_config(tmp_path: Path, monkeypatch, mounts: list[dict]) -> Path:
    """Write ``LIMA_HOME/<instance>/lima.yaml`` for a guest carrying *mounts*.

    Lima fills a missing ``mountPoint`` in with the location before saving, so
    the file spells a context directory's share differently from the template
    that asked for it.
    """
    inst_dir = tmp_path / "lima-home" / "openshrimp-dev"
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "lima.yaml").write_text(
        yaml.dump({
            "cpus": 4,
            "ssh": {"localPort": 60022},
            "mounts": [
                {"mountPoint": m["location"], **m} for m in mounts
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lima_helpers, "_lima_state_dir", lambda: tmp_path / "lima-home",
    )
    return inst_dir / "lima.yaml"


def _record_guest_commands(monkeypatch) -> list[str]:
    commands: list[str] = []
    monkeypatch.setattr(
        LimaSandbox,
        "_exec_in_vm_sync",
        lambda self, cmd, **kw: (commands.append(cmd), (0, "", ""))[1],
    )
    return commands


def _reconcile(sb: LimaSandbox, monkeypatch) -> list[dict] | None:
    """The mount set ``_reconcile_mounts`` gives the instance, or ``None``
    when it leaves the running guest alone."""
    remounted: list[list[dict]] = []
    monkeypatch.setattr(
        LimaSandbox,
        "_remount",
        lambda self, template, **kw: (
            remounted.append(template["mounts"]), True,
        )[1],
    )
    sb._reconcile_mounts(sb._template())
    return remounted[0] if remounted else None


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
    """The wrapped-CLI runtime's home is mounted under the guest user's own
    home; the served runtime's comes from its launch's home mounts, under the
    ``HOME`` its serve process is given.  Neither displaces the other."""
    claude, opencode = _claude(tmp_path), _opencode()
    sb = _sandbox(tmp_path, claude, opencode)

    mounts = _mounts(sb)

    assert mounts[str(claude.home_mount.host_dir)]["mountPoint"] == (
        f"{lima_guest_home()}/.claude"
    )
    for mount in opencode.launch.home_mounts:
        assert mounts[str(mount.host_dir)]["mountPoint"] == mount.guest_mount_point


def test_the_second_agents_task_output_path_is_symlinked_onto_the_mounted_one(
    tmp_path, monkeypatch,
):
    """Lima merges mount entries by location, so the host dir cannot be
    mounted twice; without the link OpenCode writes to guest-local disk and
    "View output" finds nothing."""
    sb = _sandbox(tmp_path, _claude(tmp_path), _opencode())
    commands = _record_guest_commands(monkeypatch)

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


def test_the_link_targets_the_path_the_running_guest_actually_mounts(
    tmp_path, monkeypatch,
):
    """A guest booted for both agents mounts the task-output dir at
    /tmp/claude-<uid>.  A process that has only registered OpenCode would pick
    /tmp/openshrimp-<uid> for a fresh guest, and linking that path onto itself
    leaves OpenCode's output on guest-local disk."""
    sb = _sandbox(tmp_path, _opencode())
    _instance_config(tmp_path, monkeypatch, _mounts_for(
        tmp_path, _claude(tmp_path), _opencode(),
    ))
    commands = _record_guest_commands(monkeypatch)

    sb._link_task_tmp_aliases()

    assert len(commands) == 1
    assert (
        f"ln -sfn /tmp/claude-{LIMA_GUEST_UID} /tmp/openshrimp-{LIMA_GUEST_UID}"
        in commands[0]
    )


def _second_cli(tmp_path: Path) -> Any:
    """A second wrapped-CLI agent: its own host home, guest home and argv0."""
    return AgentRuntime(
        name="other",
        home_mount=HomeMount(
            host_dir=tmp_path / "vm" / "other-home",
            guest_dir="/home/other/.config/other",
            holds_session_state=True,
        ),
        inject=lambda home: None,
        env={},
        launch=WrappedCLI(),
        image_bundle=ImageBundle(
            guest_home="/home/other",
            guest_argv0="other",
            task_tmp_prefix="other",
        ),
    )


def test_a_second_wrapped_cli_agent_gets_a_home_of_its_own(tmp_path):
    """One mount per wrapped-CLI runtime's home, each under the guest user's
    own home — which is where ``limactl shell`` lands."""
    claude, other = _claude(tmp_path), _second_cli(tmp_path)
    sb = _sandbox(tmp_path, claude, other)

    mounts = _mounts(sb)

    assert mounts[str(claude.home_mount.host_dir)]["mountPoint"] == (
        f"{lima_guest_home()}/.claude"
    )
    assert mounts[str(other.home_mount.host_dir)]["mountPoint"] == (
        f"{lima_guest_home()}/.config/other"
    )


def test_the_served_runtimes_home_stays_under_the_home_it_serves_with(tmp_path):
    """``run_served_endpoint`` gives the serve process ``HOME=SANDBOX_HOME``,
    not the Lima guest user's home, so its data dir has to arrive under that
    one — and Lima merges by ``location``, so it cannot also be mounted at the
    guest user's."""
    opencode = _opencode()
    sb = _sandbox(tmp_path, _claude(tmp_path), opencode)

    mounts = _mounts(sb)

    for mount in opencode.launch.home_mounts:
        assert mounts[str(mount.host_dir)]["mountPoint"] == (
            mount.guest_mount_point
        )
        assert mount.guest_mount_point.startswith(SANDBOX_HOME)


def test_the_wrapper_execs_the_runtimes_own_cli(tmp_path):
    """The generated script is per-runtime: a guest hosting two wrapped CLIs
    gets one wrapper each, and each has to exec its own binary."""
    sb = _sandbox(tmp_path, _claude(tmp_path), _second_cli(tmp_path))

    scripts = {
        runtime.name: Path(sb.build_cli_wrapper(runtime)[0]).read_text()
        for runtime in sb._runtimes.values()
    }

    assert "&& claude\"" in scripts["claude"]
    assert "&& other\"" in scripts["other"]


# -- drift ----------------------------------------------------------------


def _fingerprint(sb: LimaSandbox) -> str:
    return lima_helpers.config_fingerprint(
        sb._template(), applied_in_place=sb._sizing_fields(),
    )


def test_gaining_a_runtime_leaves_the_rebuild_trigger_alone(tmp_path):
    """The fingerprint is what deletes the VM and builds it again, and nothing
    it hashes may move with the runtime set: which agents a guest hosts is
    settled against the instance's own mount list instead."""
    before = _sandbox(tmp_path, _claude(tmp_path))
    after = _sandbox(tmp_path, _claude(tmp_path), _opencode())

    assert _fingerprint(after) == _fingerprint(before)
    assert set(_mounts(after)) > set(_mounts(before))


def test_a_new_provision_script_moves_the_fingerprint(tmp_path):
    """Lima runs provision scripts only when it creates the instance, so a
    changed one still rebuilds the VM."""
    before = _sandbox(tmp_path, _claude(tmp_path))
    after = _sandbox(tmp_path, _claude(tmp_path))
    after._config = SandboxConfig(backend="lima", provision="apt-get install -y jq")

    assert _fingerprint(after) != _fingerprint(before)


def test_sizing_leaves_the_fingerprint_alone(tmp_path):
    """cpus, memory and disk are applied to the existing instance with
    ``limactl edit``; hashing them would delete the guest instead."""
    before = _sandbox(tmp_path, _claude(tmp_path))
    after = _sandbox(tmp_path, _claude(tmp_path))
    after._config = SandboxConfig(
        backend="lima", cpus=8, memory=8192, disk_size=80,
    )

    assert _fingerprint(after) == _fingerprint(before)


def test_a_macos_guests_disk_still_moves_the_fingerprint(tmp_path):
    """Nothing in a macOS guest grows APFS onto a grown image."""
    before = _sandbox(tmp_path, _claude(tmp_path))
    before._guest_os = "macos"
    after = _sandbox(tmp_path, _claude(tmp_path))
    after._guest_os = "macos"
    after._config = SandboxConfig(backend="lima", disk_size=80)

    assert "disk" not in after._sizing_fields()
    assert lima_helpers.config_fingerprint(
        after._template(), applied_in_place=after._sizing_fields(),
    ) != lima_helpers.config_fingerprint(
        before._template(), applied_in_place=before._sizing_fields(),
    )


def test_a_guest_booted_for_both_agents_survives_a_process_restart(
    tmp_path, monkeypatch,
):
    """The case two VM restarts per process start came from: the bot comes back
    up against a running guest and the first topic to dispatch has registered
    one agent.  The guest already carries both agents' shares, so there is
    nothing to rewrite — and the second topic's first dispatch, which registers
    the other agent, must not rewrite anything either."""
    _instance_config(tmp_path, monkeypatch, _mounts_for(
        tmp_path, _claude(tmp_path), _opencode(),
    ))

    for registered in (
        (_opencode(),),
        (_claude(tmp_path),),
        (_claude(tmp_path), _opencode()),
    ):
        sb = _sandbox(tmp_path, *registered)
        assert _reconcile(sb, monkeypatch) is None


def test_gaining_a_runtime_rewrites_the_mount_set(tmp_path, monkeypatch):
    """A guest booted for one agent has no home for the other, and Lima cannot
    hot-add one."""
    _instance_config(tmp_path, monkeypatch, _mounts_for(tmp_path, _claude(tmp_path)))
    opencode = _opencode()
    sb = _sandbox(tmp_path, _claude(tmp_path), opencode)

    plan = _reconcile(sb, monkeypatch)

    assert plan is not None
    locations = {m["location"] for m in plan}
    for mount in opencode.launch.home_mounts:
        assert str(mount.host_dir) in locations


def test_a_removed_additional_directory_does_not_stay_mounted(
    tmp_path, monkeypatch,
):
    """The approval layer treats the context's directories as the sandbox
    boundary, so a directory dropped from the config must lose its share at the
    next start — a guest carrying more than the plan is only tolerable for the
    shares an agent owns."""
    dropped = str(tmp_path / "notes")
    booted = _sandbox(tmp_path, _claude(tmp_path))
    booted._additional_directories = [dropped]
    _instance_config(tmp_path, monkeypatch, booted._template()["mounts"])
    sb = _sandbox(tmp_path, _claude(tmp_path))

    plan = _reconcile(sb, monkeypatch)

    assert plan is not None
    assert dropped not in {m["location"] for m in plan}


def test_a_boundary_rewrite_keeps_the_other_agents_shares(tmp_path, monkeypatch):
    """Dropping a context directory from a guest booted for both agents must
    not cost the un-registered agent its home: it would be rewritten back in,
    and restarted for, on that agent's next dispatch."""
    dropped = str(tmp_path / "notes")
    opencode = _opencode()
    booted = _sandbox(tmp_path, _claude(tmp_path), opencode)
    booted._additional_directories = [dropped]
    _instance_config(tmp_path, monkeypatch, booted._template()["mounts"])
    sb = _sandbox(tmp_path, _claude(tmp_path))

    plan = _reconcile(sb, monkeypatch)

    assert plan is not None
    locations = {m["location"] for m in plan}
    assert dropped not in locations
    for mount in opencode.launch.home_mounts:
        assert str(mount.host_dir) in locations


def test_the_task_output_share_keeps_the_guest_path_the_instance_gave_it(
    tmp_path, monkeypatch,
):
    """One host dir, one guest path: a process that has registered only
    OpenCode would pick /tmp/openshrimp-<uid> for a fresh guest, and moving a
    running guest's share there buys nothing the symlink does not."""
    _instance_config(tmp_path, monkeypatch, _mounts_for(
        tmp_path, _claude(tmp_path), _opencode(),
    ))
    sb = _sandbox(tmp_path, _opencode())

    plan = sb._mount_plan(sb._template(), lima_helpers.instance_mounts("openshrimp-dev"))

    tmp_share = next(m for m in plan if m["location"] == str(sb._tmp_dir))
    assert tmp_share["mountPoint"] == f"/tmp/claude-{LIMA_GUEST_UID}"


def test_an_unreadable_instance_config_leaves_the_guest_alone(
    tmp_path, monkeypatch,
):
    """A guest whose mount list cannot be read is not a guest missing a share;
    restarting it would cost the session for nothing."""
    monkeypatch.setattr(
        lima_helpers, "_lima_state_dir", lambda: tmp_path / "lima-home",
    )
    sb = _sandbox(tmp_path, _claude(tmp_path), _opencode())

    assert _reconcile(sb, monkeypatch) is None


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


# -- sizing ---------------------------------------------------------------


_GIB = 1024**3


def _reconcile_sizing(
    sb: LimaSandbox, monkeypatch, instance: dict[str, Any], *, edit: Any = None,
) -> tuple[list[str], list[dict], list[str]]:
    """Run ``_reconcile_sizing`` against *instance* as ``limactl list`` reports
    it, returning the stops, edits and rebuilds it asked for."""
    stopped: list[str] = []
    edits: list[dict] = []
    rebuilt: list[str] = []
    monkeypatch.setattr(
        "open_shrimp.sandbox.lima.limactl_instance",
        lambda limactl, name: {"name": name, **instance},
    )
    monkeypatch.setattr(
        "open_shrimp.sandbox.lima.limactl_stop",
        lambda limactl, name: stopped.append(name),
    )
    monkeypatch.setattr(
        "open_shrimp.sandbox.lima.limactl_edit",
        edit or (lambda limactl, name, fields: edits.append(fields)),
    )
    monkeypatch.setattr(sb, "_rebuild_vm", lambda **kw: rebuilt.append("x"))
    sb._reconcile_sizing(sb._template())
    return stopped, edits, rebuilt


def test_a_grown_disk_is_edited_into_the_stopped_instance(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path, _claude(tmp_path))
    sb._config = SandboxConfig(backend="lima", disk_size=40)

    stopped, edits, rebuilt = _reconcile_sizing(sb, monkeypatch, {
        "status": "Running", "cpus": 2, "memory": 2048 * 1024**2,
        "disk": 20 * _GIB,
    })

    assert stopped == ["openshrimp-dev"]
    assert edits == [{"disk": "40GiB"}]
    assert rebuilt == []


def test_matching_sizing_leaves_the_vm_running(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path, _claude(tmp_path))

    stopped, edits, _ = _reconcile_sizing(sb, monkeypatch, {
        "status": "Running", "cpus": 2, "memory": 2048 * 1024**2,
        "disk": 20 * _GIB,
    })

    assert (stopped, edits) == ([], [])


def test_a_smaller_disk_is_never_written(tmp_path, monkeypatch):
    """Lima refuses to start an instance configured below its disk's size, so
    writing one would leave the context unbootable."""
    sb = _sandbox(tmp_path, _claude(tmp_path))
    sb._config = SandboxConfig(backend="lima", disk_size=10, cpus=4)

    stopped, edits, _ = _reconcile_sizing(sb, monkeypatch, {
        "status": "Stopped", "cpus": 2, "memory": 2048 * 1024**2,
        "disk": 20 * _GIB,
    })

    assert stopped == []
    assert edits == [{"cpus": 4}]


def test_a_failed_edit_falls_back_to_a_rebuild(tmp_path, monkeypatch):
    import subprocess

    sb = _sandbox(tmp_path, _claude(tmp_path))
    sb._config = SandboxConfig(backend="lima", memory=4096)

    def refuse(limactl, name, fields):
        raise subprocess.CalledProcessError(1, "limactl", stderr="nope")

    _, _, rebuilt = _reconcile_sizing(sb, monkeypatch, {
        "status": "Stopped", "cpus": 2, "memory": 2048 * 1024**2,
        "disk": 20 * _GIB,
    }, edit=refuse)

    assert rebuilt == ["x"]
