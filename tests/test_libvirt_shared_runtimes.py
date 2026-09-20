"""One libvirt domain hosting both agent backends at once.

The guest fixes its virtiofs device set when it boots, so the mount plan is a
union over every registered runtime rather than a mutation of a live domain:
one device per host dir, and one systemd mount unit per guest path that dir is
mounted at.  Two topics bound to the same context can then run Claude and
OpenCode side by side without an ACPI shutdown and a cold boot between them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from open_shrimp import paths
from open_shrimp.backend.claude_sdk.runtime import claude_runtime
from open_shrimp.backend.opencode.runtime import opencode_runtime
from open_shrimp.sandbox.agent_runtime import (
    AgentRuntime,
    HomeMount,
    ImageBundle,
    WrappedCLI,
)
from open_shrimp.sandbox import libvirt_helpers
from open_shrimp.sandbox.libvirt import LibvirtSandbox
from open_shrimp.sandbox.libvirt_helpers import _fs_tag_for_dir, ensure_mounts
from open_shrimp.sandbox.skill_paths import SANDBOX_HOME, SANDBOX_UID


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch):
    """Keep the runtime factories' per-context mkdirs inside tmp_path."""
    monkeypatch.setattr(paths, "_data_dir", tmp_path / "data")
    # The skills share is only mounted when the host has one; pin it absent so
    # the plan does not depend on the developer's own ~/.claude.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    yield


def _sandbox(tmp_path: Path, *runtimes: Any) -> LibvirtSandbox:
    """A LibvirtSandbox with just the attributes the mount plan reads."""
    sb = object.__new__(LibvirtSandbox)
    sb._project_dir = str(tmp_path / "project")
    sb._additional_directories = []
    sb._screenshots_dir = None
    sb._sdir = tmp_path / "vm"
    sb._tmp_dir = sb._sdir / "tmp"
    sb._runtimes = {r.name: r for r in runtimes}
    sb._in_use = {}
    return sb


def _claude(tmp_path: Path) -> Any:
    return claude_runtime(tmp_path / "vm" / "claude-home")


def _opencode() -> Any:
    return opencode_runtime(context_name="dev", provider_id=None)


def test_the_task_output_dir_mounts_at_every_runtimes_task_tmp_path(tmp_path):
    """Claude writes background-task output to /tmp/claude-<uid>, OpenCode to
    /tmp/openshrimp-<uid>.  One host dir backs both, because the terminal mini
    app resolves that output at a single <context state dir>/tmp."""
    sb = _sandbox(tmp_path, _claude(tmp_path), _opencode())

    all_dirs, mounts, _ = sb._shared_dirs_and_overrides()

    tmp_dir = str(sb._tmp_dir)
    assert mounts[tmp_dir] == [
        f"/tmp/claude-{SANDBOX_UID}",
        f"/tmp/openshrimp-{SANDBOX_UID}",
    ]
    # One filesystem device, two mount units against its tag.
    assert all_dirs.count(tmp_dir) == 1


def test_one_runtime_mounts_the_task_output_dir_once(tmp_path):
    sb = _sandbox(tmp_path, _claude(tmp_path))

    _all_dirs, mounts, _ = sb._shared_dirs_and_overrides()

    assert mounts[str(sb._tmp_dir)] == [f"/tmp/claude-{SANDBOX_UID}"]


def test_a_sandbox_with_no_runtime_keeps_the_default_task_tmp_path(tmp_path):
    """``create_sandbox`` passes ``runtimes=[]`` when no runtime is resolved."""
    sb = _sandbox(tmp_path)

    _all_dirs, mounts, _ = sb._shared_dirs_and_overrides()

    assert mounts[str(sb._tmp_dir)] == [f"/tmp/claude-{SANDBOX_UID}"]


def test_both_runtimes_get_their_own_home_in_one_guest(tmp_path):
    """The wrapped-CLI runtime's home is shared at the guest user's own
    ``.claude``; the served runtime's comes from its launch's home mounts.
    Neither displaces the other."""
    claude, opencode = _claude(tmp_path), _opencode()
    sb = _sandbox(tmp_path, claude, opencode)

    all_dirs, mounts, _ = sb._shared_dirs_and_overrides()

    assert mounts[str(claude.home_mount.host_dir)] == [
        f"{SANDBOX_HOME}/.claude"
    ]
    for mount in opencode.launch.home_mounts:
        assert mounts[str(mount.host_dir)] == [mount.guest_mount_point]
        assert str(mount.host_dir) in all_dirs


def test_the_tag_set_does_not_depend_on_which_topic_dispatched_first(tmp_path):
    """Registration order is whichever ChatScope dispatched first and nothing
    persists it, so an arrival-ordered plan would drift the domain's tag set
    after a restart that happened to dispatch the other way round — and drift
    means an ACPI shutdown and a cold boot."""
    claude_first = _sandbox(tmp_path, _claude(tmp_path), _opencode())
    opencode_first = _sandbox(tmp_path, _opencode(), _claude(tmp_path))

    a_dirs, a_mounts, a_ro = claude_first._shared_dirs_and_overrides()
    b_dirs, b_mounts, b_ro = opencode_first._shared_dirs_and_overrides()

    assert {_fs_tag_for_dir(d) for d in a_dirs} == {
        _fs_tag_for_dir(d) for d in b_dirs
    }
    assert a_mounts == b_mounts
    assert a_ro == b_ro


def test_the_plan_covers_a_backend_nobody_has_launched(tmp_path):
    """The domain's filesystem devices are fixed when it is defined, so the
    plan is built from the runtimes the guest is laid out for and never
    consults the ones in use.  A tag that arrived on a backend switch would
    cost an ACPI shutdown and a cold boot, with the running agent going down
    with the guest."""
    claude, opencode = _claude(tmp_path), _opencode()
    sb = _sandbox(tmp_path, claude, opencode)
    sb.add_runtime(claude)

    all_dirs, _mounts, _ro = sb._shared_dirs_and_overrides()

    assert sb.runtimes_in_use == {"claude"}
    for mount in opencode.launch.home_mounts:
        assert str(mount.host_dir) in all_dirs


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
    """One home share per wrapped-CLI runtime, each at the guest path that
    runtime resolves from the guest user's home — the home-relative tail, so
    an XDG-shaped home stays where the CLI looks for it."""
    claude, other = _claude(tmp_path), _second_cli(tmp_path)
    sb = _sandbox(tmp_path, claude, other)

    _all_dirs, mounts, _ = sb._shared_dirs_and_overrides()

    assert mounts[str(claude.home_mount.host_dir)] == [
        f"{SANDBOX_HOME}/.claude"
    ]
    assert mounts[str(other.home_mount.host_dir)] == [
        f"{SANDBOX_HOME}/.config/other"
    ]


def test_the_wrapper_execs_the_runtimes_own_cli(tmp_path):
    """The generated script is per-runtime: a guest hosting two wrapped CLIs
    gets one wrapper each, and each has to exec its own binary."""
    sb = _sandbox(tmp_path, _claude(tmp_path), _second_cli(tmp_path))
    sb._context_name = "dev"
    sb._instance_prefix = "openshrimp"
    sb._ssh_port = 2222

    scripts = {
        runtime.name: Path(sb.build_cli_wrapper(runtime)[0]).read_text()
        for runtime in sb._runtimes.values()
    }

    assert "&& claude\"" in scripts["claude"]
    assert "&& other\"" in scripts["other"]


# -- ensure_mounts --------------------------------------------------------


class _FakeSsh:
    """Answers ``systemd-escape`` locally and records the guest commands."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "systemd-escape":
            # One line per path, like the real thing.
            stems = "\n".join(
                path.strip("/").replace("-", "\\x2d").replace("/", "-")
                for path in argv[2:]
            )
            return subprocess.CompletedProcess(argv, 0, stdout=stems, stderr="")
        self.commands.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


def test_one_host_dir_mounted_twice_emits_two_units_against_one_tag(
    tmp_path, monkeypatch,
):
    ssh = _FakeSsh()
    monkeypatch.setattr(libvirt_helpers.subprocess, "run", ssh)
    host_dir = str(tmp_path / "tmp")
    tag = _fs_tag_for_dir(host_dir)

    ensure_mounts(
        ssh_port=2222,
        ssh_key=tmp_path / "ssh_key",
        shared_dirs=[host_dir],
        mount_overrides={host_dir: ["/tmp/claude-1000", "/tmp/openshrimp-1000"]},
    )

    units = [c for c in ssh.commands if "tee" in c]
    assert len(units) == 2
    assert [u for u in units if "tmp-claude\\x2d1000.mount" in u]
    assert [u for u in units if "tmp-openshrimp\\x2d1000.mount" in u]
    # Both units mount the one share: same What=, different Where=.
    assert sum(f"What={tag}" in u for u in units) == 2


def test_every_guest_path_is_escaped_in_one_call(tmp_path, monkeypatch):
    """systemd-escape is a fork per call, and the union adds three mount units
    per guest, so the whole plan goes through one invocation."""
    calls: list[list[str]] = []
    ssh = _FakeSsh()

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return ssh(argv, **kwargs)

    monkeypatch.setattr(libvirt_helpers.subprocess, "run", run)
    tmp_dir, home = str(tmp_path / "tmp"), str(tmp_path / "home")

    ensure_mounts(
        ssh_port=2222,
        ssh_key=tmp_path / "ssh_key",
        shared_dirs=[tmp_dir, home],
        mount_overrides={tmp_dir: ["/tmp/claude-1000", "/tmp/openshrimp-1000"]},
    )

    escapes = [c for c in calls if c[0] == "systemd-escape"]
    assert len(escapes) == 1
    assert escapes[0][2:] == ["/tmp/claude-1000", "/tmp/openshrimp-1000", home]


def test_no_shares_escapes_nothing(tmp_path, monkeypatch):
    """systemd-escape rejects being called with no paths."""
    calls: list[list[str]] = []
    ssh = _FakeSsh()

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return ssh(argv, **kwargs)

    monkeypatch.setattr(libvirt_helpers.subprocess, "run", run)

    ensure_mounts(
        ssh_port=2222, ssh_key=tmp_path / "ssh_key", shared_dirs=[],
    )

    assert [c for c in calls if c[0] == "systemd-escape"] == []


def test_a_host_dir_without_an_override_mounts_at_its_own_path(
    tmp_path, monkeypatch,
):
    ssh = _FakeSsh()
    monkeypatch.setattr(libvirt_helpers.subprocess, "run", ssh)
    host_dir = str(tmp_path / "project")

    ensure_mounts(
        ssh_port=2222,
        ssh_key=tmp_path / "ssh_key",
        shared_dirs=[host_dir],
        mount_overrides={},
    )

    units = [c for c in ssh.commands if "tee" in c]
    assert len(units) == 1
    assert f"Where={host_dir}" in units[0]


def test_the_host_binary_digest_is_read_once_per_binary(tmp_path, monkeypatch):
    """Both runtimes' CLIs are compared against the guest on every sandbox
    start, and these binaries run to hundreds of megabytes."""
    from open_shrimp.sandbox.libvirt_helpers import _host_binary_digest

    binary = tmp_path / "claude"
    binary.write_bytes(b"v1")
    monkeypatch.setattr(libvirt_helpers, "_host_digests", {})

    reads = 0
    real_sha256 = libvirt_helpers.hashlib.sha256

    def counting_sha256(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(libvirt_helpers.hashlib, "sha256", counting_sha256)

    first = _host_binary_digest(str(binary))
    assert _host_binary_digest(str(binary)) == first
    assert reads == 1

    # A re-pinned binary re-hashes: size and mtime both move.
    binary.write_bytes(b"v2-longer")
    assert _host_binary_digest(str(binary)) != first
    assert reads == 2
