"""One HCS compute system hosting both agent backends at once.

The 9p share list goes into ``create_compute_system`` and cannot be hot-added,
so the plan is a union over every registered runtime: one home share each
(the first on the fixed ``home`` port, the rest on the extra range), and the
one task-output dir bound at every ``/tmp`` slug the agents write to.  A guest
already running when a runtime joins is caught by reading its own mount table
back, which is what turns a swap into a reboot instead of a rebuild.

The runtimes are the shipped factories, as in the libvirt and Lima equivalents:
the guest homes and task-tmp slugs under test are the ones that reach a real
guest.  No guest and no Windows — ``hcs_win`` is faked and the control channel
is a recorder.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from open_shrimp import paths
from open_shrimp.backend.claude_sdk.runtime import claude_runtime
from open_shrimp.backend.opencode.runtime import opencode_runtime
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import hcs_helpers as H
from open_shrimp.sandbox.hcs import HcsSandbox

RID = "11111111-2222-3333-4444-555555555555"
#: How the kernel writes a space inside a /proc/mounts mount point.
ESCAPED_SPACE = "\\040"
CLAUDE_CHROOT_HOME = f"{H.CHROOT_HOME}/.claude"
OPENCODE_CHROOT_HOME = f"{H.CHROOT_HOME}/.local/share/opencode"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch):
    """Keep the runtime factories' per-context mkdirs inside tmp_path."""
    monkeypatch.setattr(paths, "_data_dir", tmp_path / "data")
    yield


def _claude(sb: HcsSandbox) -> Any:
    """Claude's runtime, homed where ``HcsSandboxManager`` homes it."""
    return claude_runtime(sb._sdir / "claude-home")


def _opencode() -> Any:
    return opencode_runtime(context_name="dev", provider_id=None)


def _sandbox(
    tmp_path,
    monkeypatch,
    *,
    additional_directories: list[str] | None = None,
) -> HcsSandbox:
    monkeypatch.setattr(sys, "platform", "win32")
    sb = HcsSandbox(
        "dev",
        SandboxConfig(backend="hcs", base_image=str(tmp_path / "root.vhdx")),
        str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
        additional_directories=additional_directories,
    )
    sb._runtime_id = RID
    return sb


def _hosting(tmp_path, monkeypatch, *names: str, **kw) -> HcsSandbox:
    """A sandbox hosting *names*, registered in that order."""
    sb = _sandbox(tmp_path, monkeypatch, **kw)
    for name in names:
        sb.add_runtime(_claude(sb) if name == "claude" else _opencode())
    return sb


def _shares(sb: HcsSandbox) -> dict[str, tuple[str, int]]:
    return {name: (path, port) for name, path, port, _f in sb._p9_shares()}


# -- the share plan -----------------------------------------------------------


def test_each_runtime_gets_its_own_home_share(tmp_path, monkeypatch):
    """The ``home`` slot used to mean "whichever agent is active"; with two
    registered it holds one of them and the other has nowhere to land."""
    sb = _hosting(tmp_path, monkeypatch, "claude", "opencode")

    shares = _shares(sb)
    homes = {path for name, (path, _port) in shares.items()
             if name.startswith("home")}
    assert homes == {str(home) for home in sb._agent_home_dirs()}
    assert len(homes) == 2
    # The first keeps the fixed port; the second runs on from the extra range,
    # the way the served shares already do.
    assert shares["home"][1] == H.P9_PORT_HOME
    assert shares["home1"][1] >= H.P9_PORT_EXTRA_BASE
    # Every share port is distinct, or two shares dial one vsock listener.
    ports = [port for _path, port in shares.values()]
    assert len(set(ports)) == len(ports)


def test_the_share_plan_does_not_depend_on_which_agent_dispatched_first(
    tmp_path, monkeypatch,
):
    """Registration order is whichever ChatScope dispatched first and nothing
    persists it, so an arrival-ordered plan would move a runtime's home to a
    different vsock port from one process start to the next."""
    claude_first = _hosting(tmp_path, monkeypatch, "claude", "opencode")
    opencode_first = _hosting(tmp_path, monkeypatch, "opencode", "claude")

    assert claude_first._share_plan() == opencode_first._share_plan()


def test_the_second_home_share_follows_the_additional_directories(
    tmp_path, monkeypatch,
):
    extra = tmp_path / "extra"
    extra.mkdir()
    sb = _hosting(
        tmp_path, monkeypatch, "claude", "opencode",
        additional_directories=[str(extra)],
    )

    shares = _shares(sb)
    assert shares["add0"][1] == H.P9_PORT_EXTRA_BASE
    assert shares["home1"][1] == H.P9_PORT_EXTRA_BASE + 1
    assert shares["srv0"][1] == H.P9_PORT_EXTRA_BASE + 2
    # The reserved set has to grow with them, or a guest service on one of
    # those ports is bridged onto a share's listener.
    reserved = H.reserved_vsock_ports(port for _p, port in shares.values())
    assert {shares["home1"][1], shares["srv0"][1]} <= reserved


def test_one_runtime_keeps_the_layout_that_ships_today(tmp_path, monkeypatch):
    sb = _hosting(tmp_path, monkeypatch, "claude")

    assert [name for name, _p, _port, _f in sb._p9_shares()] == [
        "ws", "home", "cfg", "tasktmp",
    ]
    assert _shares(sb)["home"] == (
        str(sb._sdir / "claude-home"), H.P9_PORT_HOME,
    )


def test_a_runtime_less_guest_still_has_a_home_share(tmp_path, monkeypatch):
    """``create_sandbox`` passes ``runtimes=[]`` when no runtime is resolved."""
    sb = _sandbox(tmp_path, monkeypatch)

    assert _shares(sb)["home"][0] == str(sb._sdir / "claude-home")
    assert sb._task_tmp_guest_paths() == [f"/tmp/claude-{H.CHROOT_UID}"]


# -- the binds the provision pass installs ------------------------------------


class _FakeChannel:
    """Control channel that answers *reply* to every command, recording what
    it was given."""

    def __init__(self, reply: str, *, ok: bool = True) -> None:
        self.reply = reply
        self.ok = ok
        self.commands: list[str] = []

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        return self.ok, self.reply


def _fake_win(monkeypatch, chan: _FakeChannel) -> None:
    fake = type(sys)("open_shrimp.sandbox.hcs_win")
    fake.ControlChannel = lambda *a, **kw: chan
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", fake)


def _provision(sb: HcsSandbox, monkeypatch) -> list[str]:
    chan = _FakeChannel("MOUNT-OK ROOT-MOUNT-OK PV-OK PROVISION-DONE")
    _fake_win(monkeypatch, chan)
    monkeypatch.setattr(sb, "_start_exec_agent", lambda c: None)
    monkeypatch.setattr(sb, "_configure_network", lambda c: None)
    sb._provision_guest(log_file=None)
    return chan.commands


def _binds(commands: list[str]) -> dict[str, list[str]]:
    """Bind targets by source mount point."""
    binds: dict[str, list[str]] = {}
    for cmd in commands:
        if "mount -o bind " not in cmd:
            continue
        src, dst = cmd.split("mount -o bind ", 1)[1].split(" ", 1)
        binds.setdefault(src.strip("'"), []).append(dst.strip().strip("'"))
    return binds


def test_both_homes_are_bound_at_their_own_chroot_paths(tmp_path, monkeypatch):
    """Each agent resolves its home from the one chroot ``HOME``, so the two
    homes cannot share a bind target."""
    sb = _hosting(tmp_path, monkeypatch, "claude", "opencode")

    binds = _binds(_provision(sb, monkeypatch))
    home_targets = {
        src: targets for src, targets in binds.items()
        if src == H.MNT_HOME or src.startswith("/mnt/home")
    }

    assert sorted(home_targets) == [H.MNT_HOME, "/mnt/home1"]
    assert sorted(t for targets in home_targets.values() for t in targets) == [
        f"{H.MNT_ROOT}{CLAUDE_CHROOT_HOME}",
        f"{H.MNT_ROOT}{OPENCODE_CHROOT_HOME}",
    ]


def test_the_task_output_dir_is_bound_at_every_agents_tmp_slug(
    tmp_path, monkeypatch,
):
    """Claude writes /tmp/claude-0, OpenCode /tmp/openshrimp-0, and the host
    terminal mini app reads both from the one shared dir — a bind at only one
    of them leaves "View output" with nothing for the other agent."""
    sb = _hosting(tmp_path, monkeypatch, "claude", "opencode")

    binds = _binds(_provision(sb, monkeypatch))

    assert binds[H.MNT_TASK_TMP] == [
        f"{H.MNT_ROOT}/tmp/claude-{H.CHROOT_UID}",
        f"{H.MNT_ROOT}/tmp/openshrimp-{H.CHROOT_UID}",
    ]


def test_every_share_is_mounted_before_it_is_bound(tmp_path, monkeypatch):
    sb = _hosting(tmp_path, monkeypatch, "claude", "opencode")

    commands = _provision(sb, monkeypatch)
    at = {cmd: i for i, cmd in enumerate(commands)}

    assert len([c for c in commands if c.startswith("@mount ")]) == len(
        sb._share_plan()
    )
    for share in sb._share_plan():
        mount = next(
            at[c] for c in commands
            if c.startswith(f"@mount {share.port} {share.name} "
                            f"{share.guest_mnt} ")
        )
        for src, _target in share.chroot_binds():
            bind = next(at[c] for c in commands if f"mount -o bind {src} " in c)
            assert mount < bind, share


# -- the drift check ----------------------------------------------------------


def _mount_table(sb: HcsSandbox) -> str:
    """What /proc/mounts looks like in a guest carrying *sb*'s whole plan.

    Mount points are written the way the kernel writes them, with a space
    escaped as ``\\040``.
    """
    return "".join(
        f"9p {point.replace(' ', ESCAPED_SPACE)} 9p rw 0 0\n"
        for share in sb._share_plan()
        for point in share.guest_mount_points()
    )


def _probe(sb: HcsSandbox, monkeypatch, table: str) -> list[str]:
    _fake_win(monkeypatch, _FakeChannel(table))
    return sb._missing_guest_mounts()


def test_a_guest_carrying_the_plan_is_left_alone(tmp_path, monkeypatch):
    sb = _hosting(tmp_path, monkeypatch, "claude", "opencode")

    assert _probe(sb, monkeypatch, _mount_table(sb)) == []


def test_a_runtime_that_joined_after_the_boot_is_reported_missing(
    tmp_path, monkeypatch,
):
    """The share list is fixed at ``create_compute_system``; the second
    agent's home can only arrive through a reboot."""
    sb = _hosting(tmp_path, monkeypatch, "claude")
    booted_with_claude_only = _mount_table(sb)
    sb.add_runtime(_opencode())

    missing = _probe(sb, monkeypatch, booted_with_claude_only)

    assert f"{H.MNT_ROOT}{OPENCODE_CHROOT_HOME}" in missing


def test_a_guest_carrying_more_than_the_plan_is_left_alone(
    tmp_path, monkeypatch,
):
    """A process restart that dispatches one backend first meets a guest
    booted for both.  Rebooting it to drop the other's shares would cost a
    restart to lose exactly what the next dispatch asks for."""
    both = _hosting(tmp_path, monkeypatch, "claude", "opencode")
    opencode_only = _hosting(tmp_path, monkeypatch, "opencode")

    assert _probe(opencode_only, monkeypatch, _mount_table(both)) == []


def test_a_mount_point_with_a_space_is_not_reported_missing(
    tmp_path, monkeypatch,
):
    """The kernel writes a space in a mount point as \\040, and a Windows
    workspace path routinely has one."""
    sb = _hosting(tmp_path, monkeypatch, "claude")
    sb._project_dir = str(tmp_path / "My Projects" / "ws")
    table = _mount_table(sb)

    assert " " in sb._guest_workspace()
    assert ESCAPED_SPACE in table
    assert _probe(sb, monkeypatch, table) == []


def test_an_unanswered_probe_reports_nothing_missing(tmp_path, monkeypatch):
    """Rebooting a guest because its control channel dropped a reply would
    cost the session for nothing."""
    sb = _hosting(tmp_path, monkeypatch, "claude")
    _fake_win(monkeypatch, _FakeChannel("", ok=False))

    assert sb._missing_guest_mounts() == []


def test_ensure_running_reboots_a_guest_that_lost_a_share(
    tmp_path, monkeypatch,
):
    sb = _hosting(tmp_path, monkeypatch, "claude")
    monkeypatch.setattr(sb, "running", lambda: True)
    monkeypatch.setattr(
        sb, "_missing_guest_mounts", lambda: [f"{H.MNT_ROOT}/root/x"],
    )
    events: list[str] = []
    monkeypatch.setattr(sb, "stop", lambda: events.append("stop"))
    monkeypatch.setattr(sb, "_boot", lambda *, log_file: events.append("boot"))

    sb.ensure_running()

    assert events == ["stop", "boot"]


def test_ensure_running_leaves_a_matching_guest_alone(tmp_path, monkeypatch):
    sb = _hosting(tmp_path, monkeypatch, "claude")
    monkeypatch.setattr(sb, "running", lambda: True)
    monkeypatch.setattr(sb, "_missing_guest_mounts", lambda: [])
    monkeypatch.setattr(
        sb, "_boot",
        lambda *, log_file: pytest.fail("rebooted a guest that was fine"),
    )

    sb.ensure_running()
