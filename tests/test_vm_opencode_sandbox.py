import subprocess
from pathlib import Path
from unittest.mock import patch

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.libvirt import LibvirtSandbox
from open_shrimp.sandbox.lima import LimaSandbox
from open_shrimp.sandbox.lima_helpers import _build_mounts
from open_shrimp.sandbox.lima_macos_helpers import _build_mounts_macos


class FakeProc:
    def __init__(self, args: list[str], *, tunnel: bool = False) -> None:
        self.args = args
        self.tunnel = tunnel
        self.running = True
        self.returncode: int | None = None
        self.stdout = []
        self.stderr = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else self.returncode or 0

    def wait(self, timeout: float | None = None) -> int:
        if self.tunnel and self.running and timeout == 0.5:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.running = False
        self.returncode = self.returncode or 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.running = False
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.running = False
        self.returncode = -9


def test_lima_mounts_opencode_home_for_linux_and_macos(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")

    linux_mounts = _build_mounts(tmp_path, "/repo", None)
    macos_mounts = _build_mounts_macos(tmp_path, "/repo", None)

    assert {
        "location": str(tmp_path / "opencode-home"),
        "mountPoint": "/home/alice.guest/.local/share/opencode",
        "writable": True,
    } in linux_mounts
    assert {
        "location": str(tmp_path / "opencode-home"),
        "mountPoint": "/Users/alice.guest/Library/Application Support/opencode",
        "writable": True,
    } in macos_mounts


def test_libvirt_ensure_opencode_server_starts_guest_server(tmp_path):
    procs: list[FakeProc] = []

    def fake_popen(args, **kwargs):
        proc = FakeProc(args, tunnel=args[:1] == ["ssh"] and "-N" in args)
        procs.append(proc)
        return proc

    with (
        patch("open_shrimp.sandbox.libvirt.state_dir_for", return_value=tmp_path),
        patch("open_shrimp.sandbox.libvirt.find_virtiofsd", return_value=None),
        patch("open_shrimp.sandbox.libvirt.allocate_host_port", return_value=49152),
        patch("open_shrimp.sandbox.libvirt._sync_opencode_auth") as sync_auth,
        patch("open_shrimp.sandbox.libvirt._wait_for_opencode_ready"),
        patch("open_shrimp.sandbox.libvirt._drain_opencode_output"),
        patch("open_shrimp.sandbox.libvirt.subprocess.Popen", side_effect=fake_popen),
        patch(
            "open_shrimp.sandbox.libvirt_helpers._ssh_common_opts",
            return_value=["-p", "2222", "-i", str(tmp_path / "ssh_key")],
        ),
    ):
        sandbox = LibvirtSandbox(
            "dev",
            SandboxConfig(backend="libvirt"),
            "/repo",
            conn=object(),
        )
        sandbox._ssh_port = 2222
        endpoint = sandbox.ensure_opencode_server(provider_id="anthropic")

    assert endpoint.base_url == "http://127.0.0.1:49152"
    assert endpoint.auth_header.startswith("Basic ")
    sync_auth.assert_called_once_with("anthropic", tmp_path / "opencode-home")
    assert procs[0].args[:2] == ["ssh", "-p"]
    assert procs[0].args[-1] == "claude@localhost"
    assert "-L" in procs[0].args
    assert "opencode serve --hostname 127.0.0.1" in procs[1].args[-1]


def test_lima_ensure_opencode_server_uses_internal_tunnel_and_cache(
    tmp_path, monkeypatch,
):
    lima_home = tmp_path / "lima-home"
    ssh_dir = lima_home / "dev"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "ssh.config").write_text("Host lima-dev\n", encoding="utf-8")
    procs: list[FakeProc] = []

    def fake_popen(args, **kwargs):
        proc = FakeProc(args, tunnel=args[:1] == ["ssh"] and "-N" in args)
        procs.append(proc)
        return proc

    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    with (
        patch("open_shrimp.sandbox.lima.state_dir_for", return_value=tmp_path / "state"),
        patch("open_shrimp.sandbox.lima._lima_env", return_value={"LIMA_HOME": str(lima_home)}),
        patch("open_shrimp.sandbox.lima.limactl_instance_status", return_value="Running"),
        patch("open_shrimp.sandbox.lima.allocate_host_port", return_value=49153),
        patch("open_shrimp.sandbox.lima._sync_opencode_auth"),
        patch("open_shrimp.sandbox.lima._wait_for_opencode_ready"),
        patch("open_shrimp.sandbox.lima._drain_opencode_output"),
        patch("open_shrimp.sandbox.lima.subprocess.Popen", side_effect=fake_popen),
    ):
        sandbox = LimaSandbox(
            "dev",
            SandboxConfig(backend="lima"),
            "/repo",
            "limactl",
            guest_os="linux",
        )
        first = sandbox.ensure_opencode_server(provider_id="anthropic")
        second = sandbox.ensure_opencode_server(provider_id="anthropic")
        sandbox._opencode_forward.running = False
        third = sandbox.ensure_opencode_server(provider_id="anthropic")

    assert first is second
    assert third.base_url == "http://127.0.0.1:49153"
    assert len(procs) == 4
    assert procs[0].args[:3] == ["ssh", "-F", str(ssh_dir / "ssh.config")]
    assert procs[0].args[-1] == "lima-dev"
    assert "-L" in procs[0].args
    assert procs[1].args[:4] == ["limactl", "shell", "dev", "--"]
    assert "OPENCODE_SERVER_PASSWORD=" in procs[1].args[-1]
    assert "opencode serve --hostname 127.0.0.1" in procs[1].args[-1]
