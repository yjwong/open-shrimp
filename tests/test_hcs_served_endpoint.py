"""HCS served-endpoint launch: the second launcher variant, the credential's
lifetime, the stale-serve reap, the reuse guard, and teardown ordering.

No guest and no Windows: ``hcs_win`` is faked (the shape the port-bridge tests
use), ``guest_exec`` is a scriptable recorder, the launcher build is stubbed
(it shells to ``csc``), and ``subprocess.Popen`` is replaced by a fake process.
The shared ``run_served_endpoint`` body runs for real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import hcs as hcs_mod
from open_shrimp.sandbox import hcs_helpers as H
from open_shrimp.sandbox.agent_runtime import (
    AgentRuntime,
    GuestMount,
    HomeMount,
    ImageBundle,
    ServedEndpoint,
    WrappedCLI,
)
from open_shrimp.sandbox.hcs import HcsSandbox

RID = "11111111-2222-3333-4444-555555555555"
GUEST_HOME = "/home/openshrimp"
GUEST_CONFIG = f"{GUEST_HOME}/.local/share/openshrimp/managed/plugin.json"
SERVE_ARGV = ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]


# -- doubles ------------------------------------------------------------------


class _FakeProc:
    """Enough of ``Popen`` for the served launch body and its teardown."""

    def __init__(self, argv, env=None, **kwargs):
        self.argv = argv
        self.env = env or {}
        self.kwargs = kwargs
        self.stdout = None
        self.terminated = False
        self.killed = False
        self._returncode: int | None = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9


def _fake_win(monkeypatch, events: list[str] | None = None):
    """Install a fake ``hcs_win`` covering both the bridge and the stop path."""

    class _HcsError(Exception):
        pass

    class _Channel:
        def __init__(self, runtime_id, port):
            self.runtime_id = runtime_id

        def run(self, command, **kwargs):
            if events is not None and "sync" in command:
                events.append("flush")
            return True, ""

    module = type(sys)("open_shrimp.sandbox.hcs_win")
    module.HcsError = _HcsError
    module.ControlChannel = _Channel
    module.vsock_port_reachable = lambda rid, port, timeout=3.0: True
    module.open_guest_port_bridge = lambda rid, host_port, guest_port: None
    module.close_guest_port_bridge = lambda rid, host_port: True
    module.close_guest_port_bridges = lambda rid: None
    module.close_host_relay = lambda rid: None

    def _op():
        raise _HcsError("no HCS here")

    module.HcsOperation = _op
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", module)
    return module


def _bundle() -> ImageBundle:
    return ImageBundle(
        tag_suffix="served", guest_home=GUEST_HOME, guest_argv0="opencode",
    )


def _served_runtime(tmp_path, *, endpoints: list | None = None) -> AgentRuntime:
    home = tmp_path / "agent-home"
    data = tmp_path / "agent-data"

    def make_endpoint(base_url, auth_header, owner):
        endpoint = SimpleNamespace(
            base_url=base_url, auth_header=auth_header, owner=owner,
        )
        if endpoints is not None:
            endpoints.append(endpoint)
        return endpoint

    return AgentRuntime(
        name="served-agent",
        home_mount=HomeMount(
            host_dir=home,
            guest_dir=f"{GUEST_HOME}/.local/share/opencode",
            holds_session_state=True,
        ),
        inject=lambda target: None,
        env={"AGENT_CONFIG": GUEST_CONFIG},
        launch=ServedEndpoint(
            serve_argv=SERVE_ARGV,
            guest_port=4096,
            home_mounts=(
                GuestMount(
                    host_dir=home,
                    guest_mount_point=f"{GUEST_HOME}/.local/share/opencode",
                ),
                GuestMount(
                    host_dir=data,
                    guest_mount_point=f"{GUEST_HOME}/.local/share/openshrimp",
                ),
            ),
            auth_username="agent",
            password_env_var="AGENT_SERVER_PASSWORD",
            make_endpoint=make_endpoint,
            wait_ready=lambda proc: None,
            drain_output=lambda proc: None,
        ),
        image_bundle=_bundle(),
    )


def _wrapped_runtime(tmp_path) -> AgentRuntime:
    return AgentRuntime(
        name="wrapped-agent",
        home_mount=HomeMount(
            host_dir=tmp_path / "agent-home",
            guest_dir=f"{GUEST_HOME}/.claude",
            holds_session_state=True,
        ),
        inject=lambda target: None,
        env={},
        launch=WrappedCLI(),
        image_bundle=_bundle(),
    )


def _make_sandbox(tmp_path, monkeypatch, runtime, **config_extra):
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
    sb._sdir.mkdir(parents=True, exist_ok=True)
    sb._runtime_id = RID

    built: list[tuple[Path, Path]] = []

    def build(*, launch_json, exe):
        built.append((launch_json, exe))
        exe.write_bytes(b"MZ")
        return exe

    monkeypatch.setattr(sb, "_build_launcher_exe", build)
    sb._built = built

    execs: list[list[str]] = []
    monkeypatch.setattr(
        sb, "guest_exec",
        lambda argv, **kw: (execs.append(argv), (0, ""))[1],
    )
    sb._execs = execs

    # Only the launcher spawn is faked; build_cli_wrapper's git lookups are
    # left to run for real (they tolerate a missing git).
    procs: list[_FakeProc] = []
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        if str(argv[0]) != str(sb._served_launcher_exe()):
            return real_popen(argv, **kw)
        procs.append(_FakeProc(argv, **kw))
        return procs[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    sb._procs = procs
    return sb


def _served_cfg(sb) -> dict:
    return json.loads(
        sb._served_launch_json_file().read_text(encoding="utf-8")
    )


# -- the launch ---------------------------------------------------------------


def test_the_serve_argv_runs_in_the_workspace_under_a_pid_recording_prologue(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    handle = sb.start_agent(runtime)

    cfg = _served_cfg(sb)
    assert cfg["argv_prefix"][:2] == ["/bin/sh", "-c"]
    # The pidfile is $0 and the serve argv is "$@", so the prologue's exec
    # replaces the shell with the server and the recorded pid is the server's.
    assert cfg["argv_prefix"][3] == hcs_mod._SERVE_PIDFILE
    assert cfg["argv_prefix"][4:] == SERVE_ARGV
    assert cfg["cwd"] == sb._guest_workspace()
    assert cfg["port"] == H.EXEC_PORT
    assert handle.endpoint is sb._served_endpoint
    assert handle.cli_path is None


def test_the_endpoint_reaches_the_guest_port_over_a_host_bridge(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    handle = sb.start_agent(runtime)

    host_port = sb._reached_ports[4096]
    assert handle.endpoint.base_url == f"http://127.0.0.1:{host_port}"
    assert handle.endpoint.owner is sb
    assert handle.endpoint.auth_header.startswith("Basic ")


def test_the_served_process_is_the_owner_the_liveness_check_reads(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)

    assert sb._served_proc is sb._procs[0]
    assert sb._procs[0].kwargs["stdin"] is subprocess.DEVNULL
    assert sb._procs[0].kwargs["stdout"] is subprocess.PIPE
    assert sb._procs[0].kwargs["stderr"] is subprocess.STDOUT
    assert sb._procs[0].kwargs["text"] is True


# -- the credential -----------------------------------------------------------


def test_the_live_credential_never_lands_in_the_launch_config(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)

    password = sb._procs[0].env["AGENT_SERVER_PASSWORD"]
    assert password
    on_disk = sb._served_launch_json_file().read_text(encoding="utf-8")
    assert password not in on_disk
    # The name is on disk; only the value is confined to the process.
    assert "AGENT_SERVER_PASSWORD" in _served_cfg(sb)["env_passthrough"]
    assert "AGENT_SERVER_PASSWORD" not in _served_cfg(sb)["env"]


def test_each_launch_mints_a_fresh_credential(tmp_path, monkeypatch):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)
    first = sb._procs[0].env["AGENT_SERVER_PASSWORD"]
    sb._served_proc.terminate()
    sb.start_agent(runtime)

    assert sb._procs[1].env["AGENT_SERVER_PASSWORD"] != first


def test_guest_env_paths_are_rebased_onto_the_chroot_home(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)

    env = sb._procs[0].env
    # A guest path under the runtime's guest home has to move with the mount.
    assert env["AGENT_CONFIG"] == (
        f"{H.CHROOT_HOME}/.local/share/openshrimp/managed/plugin.json"
    )
    # HOME/PATH are the chroot's: pinned in the config, and kept out of the
    # launcher's own Windows environment.
    cfg = _served_cfg(sb)
    assert cfg["env"] == {"HOME": H.CHROOT_HOME, "PATH": H.CHROOT_PATH}
    assert "HOME" not in cfg["env_passthrough"]
    assert env.get("HOME") != H.CHROOT_HOME


# -- the two launcher flavours ------------------------------------------------


def test_the_two_launcher_flavours_do_not_clobber_each_other(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    cli_path, _cleanup = sb.build_cli_wrapper()
    sb.start_agent(runtime)

    wrapped_json, wrapped_exe = sb._built[0]
    served_json, served_exe = sb._built[1]
    assert wrapped_json != served_json
    assert wrapped_exe != served_exe
    assert cli_path == str(wrapped_exe)
    # The launch-config path is compiled in, so a shared exe would run the
    # wrong argv; both configs must survive the other's launch.
    assert json.loads(wrapped_json.read_text(encoding="utf-8"))["argv_prefix"] == [
        "opencode",
    ]
    assert json.loads(served_json.read_text(encoding="utf-8"))["argv_prefix"][0] == (
        "/bin/sh"
    )


def test_the_wrapped_launcher_still_builds_after_a_served_launch(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)
    cli_path, _ = sb.build_cli_wrapper()

    assert cli_path == str(sb._launcher_exe())
    assert sb._served_launcher_exe().exists()


# -- reuse, reap, teardown ----------------------------------------------------


def test_a_live_endpoint_is_reused_without_a_second_spawn(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    endpoints: list = []
    runtime = _served_runtime(tmp_path, endpoints=endpoints)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    first = sb.start_agent(runtime)
    second = sb.start_agent(runtime)

    assert second.endpoint is first.endpoint
    assert len(sb._procs) == 1
    assert len(endpoints) == 1


def test_a_dead_served_process_is_relaunched(tmp_path, monkeypatch):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)
    sb._served_proc.terminate()
    sb.start_agent(runtime)

    assert len(sb._procs) == 2
    assert sb._served_proc is sb._procs[1]


def test_a_stale_guest_serve_process_is_reaped_before_spawning(
    tmp_path, monkeypatch,
):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    sb.start_agent(runtime)

    assert sb._execs, "no reap ran before the spawn"
    argv = sb._execs[0]
    assert argv[:3] == ["/bin/sh", "-c", hcs_mod._SERVE_REAP]
    # $0 is the pidfile the prologue writes; $1 is the argv the /proc entry is
    # checked against, so a pid reused after a reboot is left alone.
    assert argv[3] == hcs_mod._SERVE_PIDFILE
    assert argv[4] == "opencode"
    assert "/proc/$p/cmdline" in hcs_mod._SERVE_REAP


def test_a_launch_without_a_running_guest_is_refused(tmp_path, monkeypatch):
    _fake_win(monkeypatch)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)
    sb._runtime_id = None

    with pytest.raises(RuntimeError, match="not running"):
        sb.start_agent(runtime)
    assert sb._procs == []


def test_stop_drops_the_served_process_before_flushing_the_guest(
    tmp_path, monkeypatch,
):
    events: list[str] = []
    _fake_win(monkeypatch, events)
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)
    sb._rdp_session = SimpleNamespace(stop=lambda: events.append("rdp"))
    sb.start_agent(runtime)
    proc = sb._served_proc
    monkeypatch.setattr(
        proc, "terminate",
        lambda: (events.append("served"), setattr(proc, "_returncode", -15))[0],
    )

    sb.stop()

    # Every host process holding an hvsocket goes before the flush, and the
    # flush stays adjacent to the terminate that follows it.
    assert events == ["rdp", "served", "flush"]
    assert sb._served_proc is None
    assert sb._served_endpoint is None


def test_stop_is_clean_when_nothing_was_served(tmp_path, monkeypatch):
    _fake_win(monkeypatch)
    sb = _make_sandbox(tmp_path, monkeypatch, _wrapped_runtime(tmp_path))

    sb.stop()

    assert sb._served_proc is None


# -- the served launch's extra shares -----------------------------------------


def test_served_home_mounts_get_their_own_shares_and_binds(
    tmp_path, monkeypatch,
):
    runtime = _served_runtime(tmp_path)
    sb = _make_sandbox(tmp_path, monkeypatch, runtime)

    # The agent home is already the "home" share; only the extra mount is new.
    assert [m.guest_mount_point for m in sb._served_mounts] == [
        f"{GUEST_HOME}/.local/share/openshrimp",
    ]
    shares = {name: (path, port) for name, path, port, _f in sb._p9_shares()}
    assert shares["home"][0] == str(tmp_path / "agent-home")
    assert shares["srv0"][0] == str(tmp_path / "agent-data")
    assert shares["srv0"][1] == H.P9_PORT_EXTRA_BASE
    assert sb._extra_share_count() == 1


def test_served_share_ports_follow_the_additional_directories(
    tmp_path, monkeypatch,
):
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    runtime = _served_runtime(tmp_path)
    sb = HcsSandbox(
        "default",
        SandboxConfig(backend="hcs", base_image=str(tmp_path / "root.vhdx")),
        str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
        additional_directories=[str(extra)],
        runtime=runtime,
    )
    shares = {name: port for name, _p, port, _f in sb._p9_shares()}
    assert shares["add0"] == H.P9_PORT_EXTRA_BASE
    assert shares["srv0"] == H.P9_PORT_EXTRA_BASE + 1
    assert sb._extra_share_count() == 2


def test_a_wrapped_cli_runtime_adds_no_served_shares(tmp_path, monkeypatch):
    sb = _make_sandbox(tmp_path, monkeypatch, _wrapped_runtime(tmp_path))
    assert sb._served_mounts == ()
    assert [n for n, _p, _port, _f in sb._p9_shares()] == [
        "ws", "home", "cfg", "tasktmp",
    ]
