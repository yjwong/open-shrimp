"""The Lima in-guest install probe.

``install_cli_in_linux_vm`` serves both agents, so the version comparison is
opt-in: opencode pins a version and wants a guest behind it upgraded, while
claude's follows whatever the host has and asks only whether the binary is
there at all.
"""

from __future__ import annotations

import subprocess

import pytest

from open_shrimp.sandbox import lima_helpers as L


class _FakeVM:
    """Scripted ``limactl shell`` replies, recording every command run.

    *probe_reply* is the guest's stdout for the first call; every later call is
    an install and succeeds.
    """

    def __init__(self, probe_reply: str):
        self.probe_reply = probe_reply
        self.commands: list[str] = []

    def __call__(self, limactl, args, *, check=True, timeout=None, **kwargs):
        self.commands.append(args[-1])
        stdout = self.probe_reply if len(self.commands) == 1 else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    @property
    def installs(self) -> list[str]:
        return self.commands[1:]


def _run(monkeypatch, probe_reply: str, **kwargs) -> _FakeVM:
    vm = _FakeVM(probe_reply)
    monkeypatch.setattr(L, "_run_limactl", vm)
    L.install_cli_in_linux_vm(
        "limactl", "inst", "opencode",
        install_cmd_for=lambda arch: f"install {arch}",
        **kwargs,
    )
    return vm


def test_a_guest_at_the_pinned_version_installs_nothing(monkeypatch):
    vm = _run(
        monkeypatch,
        "/usr/local/bin/opencode\n2.0.0\nx86_64\n",
        expected_version="2.0.0",
    )

    assert vm.installs == []


def test_a_guest_behind_the_pin_is_reinstalled(monkeypatch):
    """Without this a guest provisioned before a bump keeps its old binary
    until somebody deletes it by hand."""
    vm = _run(
        monkeypatch,
        "/usr/local/bin/opencode\n1.0.0\nx86_64\n",
        expected_version="2.0.0",
    )

    assert vm.installs == ["install x64"]


def test_a_v_prefixed_version_compares_equal(monkeypatch):
    vm = _run(
        monkeypatch,
        "/usr/local/bin/opencode\nv2.0.0\nx86_64\n",
        expected_version="2.0.0",
    )

    assert vm.installs == []


def test_a_version_padded_with_the_binary_name_compares_equal(monkeypatch):
    vm = _run(
        monkeypatch,
        "/usr/local/bin/opencode\n2.0.0 (build 7)\nx86_64\n",
        expected_version="2.0.0",
    )

    assert vm.installs == []


def test_a_missing_binary_is_installed(monkeypatch):
    vm = _run(monkeypatch, "\n\naarch64\n", expected_version="2.0.0")

    assert vm.installs == ["install arm64"]


def test_a_binary_that_answers_no_version_is_reinstalled(monkeypatch):
    """A guest that cannot say what it has is not a guest that has the pin."""
    vm = _run(
        monkeypatch,
        "/usr/local/bin/opencode\n\nx86_64\n",
        expected_version="2.0.0",
    )

    assert vm.installs == ["install x64"]


def test_without_an_expected_version_presence_is_enough(monkeypatch):
    """What claude relies on: its version follows the host's, so there is no
    pin to compare against and the probe never asks for one."""
    vm = _run(monkeypatch, "/usr/local/bin/opencode\n\nx86_64\n")

    assert vm.installs == []
    assert "--version" not in vm.commands[0]


def test_without_an_expected_version_an_absent_binary_still_installs(monkeypatch):
    vm = _run(monkeypatch, "\n\nx86_64\n")

    assert vm.installs == ["install x64"]


def test_a_probe_that_answers_nothing_is_an_error(monkeypatch):
    with pytest.raises(RuntimeError, match="Failed to probe"):
        _run(monkeypatch, "")


def test_an_unsupported_guest_architecture_is_refused(monkeypatch):
    with pytest.raises(RuntimeError, match="riscv64"):
        _run(monkeypatch, "\n\nriscv64\n")


# ── the opencode installer that drives it ──


def test_the_lima_installer_pins_the_version_and_verifies_the_archive(monkeypatch):
    from open_shrimp.backend.opencode import lima_install
    from open_shrimp.backend.opencode.release import (
        OPENCODE_CHECKSUMS,
        OPENCODE_VERSION,
    )

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        L, "install_cli_in_linux_vm", lambda *a, **kw: seen.update(kw)
    )
    lima_install.ensure_opencode_cli_in_vm("limactl", "inst")

    assert seen["expected_version"] == OPENCODE_VERSION
    script = seen["install_cmd_for"]("arm64")
    assert f"v{OPENCODE_VERSION}/opencode-linux-arm64.tar.gz" in script
    assert OPENCODE_CHECKSUMS["linux-arm64"] in script
    assert script.index("sha256sum -c") < script.index("tar -xzf")
    # A Lima guest is not root; the HCS chroot is.
    assert "sudo install -m 755" in script
