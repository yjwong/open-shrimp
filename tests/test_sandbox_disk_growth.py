"""Growing a sandbox's disk in place when ``disk_size`` goes up.

libvirt grows the qcow2 overlay (``blockResize`` on a running domain,
``qemu-img`` on a stopped one) and leaves a marker for ``ensure_running`` to
extend the root filesystem in the guest.  HCS grows each persistent-volume
VHDX with the guest stopped and runs ``resize2fs`` as it mounts them.  Neither
ever shrinks a disk.  Lima's half lives in ``test_lima_shared_runtimes``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from open_shrimp import paths
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox import hcs_helpers as H
from open_shrimp.sandbox import libvirt as libvirt_mod
from open_shrimp.sandbox.hcs import HcsSandbox
from open_shrimp.sandbox.libvirt import LibvirtSandbox

_GIB = 1024**3


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "_data_dir", tmp_path / "data")
    yield


# -- libvirt ------------------------------------------------------------------


class _Domain:
    def __init__(self) -> None:
        self.resized: list[tuple[str, int, int]] = []

    def blockResize(self, disk: str, size: int, flags: int) -> None:
        self.resized.append((disk, size, flags))


class _Conn:
    def __init__(self, domain: _Domain) -> None:
        self.domain = domain

    def lookupByName(self, name: str) -> _Domain:
        return self.domain


def _libvirt_sandbox(
    tmp_path: Path, monkeypatch, *, disk_size: int, have_gib: int, active: bool,
) -> tuple[LibvirtSandbox, _Domain, list[int]]:
    fake_libvirt = type(sys)("libvirt")
    fake_libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES = 4
    monkeypatch.setitem(sys.modules, "libvirt", fake_libvirt)

    domain = _Domain()
    sb = object.__new__(LibvirtSandbox)
    sb._config = SandboxConfig(backend="libvirt", disk_size=disk_size)
    sb._conn = _Conn(domain)
    sb._dom_name = "openshrimp-dev"
    sb._sdir = tmp_path / "vm"
    sb._sdir.mkdir()
    sb._root_fs_grow_marker = sb._sdir / "root-fs-grow-pending"
    monkeypatch.setattr(sb, "_is_domain_active", lambda: active)

    qemu_resized: list[int] = []
    monkeypatch.setattr(
        libvirt_mod, "qcow2_virtual_size", lambda path: have_gib * _GIB,
    )
    monkeypatch.setattr(
        libvirt_mod, "resize_qcow2", lambda path, size: qemu_resized.append(size),
    )
    return sb, domain, qemu_resized


def test_a_stopped_overlay_grows_with_qemu_img(tmp_path, monkeypatch):
    sb, domain, qemu_resized = _libvirt_sandbox(
        tmp_path, monkeypatch, disk_size=40, have_gib=20, active=False,
    )

    sb._grow_overlay(sb._sdir / "overlay.qcow2", log_file=None)

    assert qemu_resized == [40 * _GIB]
    assert domain.resized == []
    assert sb._root_fs_grow_marker.exists()


def test_a_running_domain_grows_without_a_restart(tmp_path, monkeypatch):
    """qemu-img cannot touch an image QEMU holds open, and stopping the VM
    would take down every topic running in it."""
    sb, domain, qemu_resized = _libvirt_sandbox(
        tmp_path, monkeypatch, disk_size=40, have_gib=20, active=True,
    )

    sb._grow_overlay(sb._sdir / "overlay.qcow2", log_file=None)

    assert domain.resized == [("vda", 40 * _GIB, 4)]
    assert qemu_resized == []
    assert sb._root_fs_grow_marker.exists()


@pytest.mark.parametrize("have_gib", [20, 40])
def test_an_overlay_at_or_above_disk_size_is_left_alone(
    tmp_path, monkeypatch, have_gib,
):
    sb, domain, qemu_resized = _libvirt_sandbox(
        tmp_path, monkeypatch, disk_size=20, have_gib=have_gib, active=True,
    )

    sb._grow_overlay(sb._sdir / "overlay.qcow2", log_file=None)

    assert (domain.resized, qemu_resized) == ([], [])
    assert not sb._root_fs_grow_marker.exists()


# -- HCS ----------------------------------------------------------------------


def _hcs_sandbox(
    tmp_path: Path, monkeypatch, *, disk_size: int, sizes: dict[str, Any],
    running: bool,
) -> tuple[HcsSandbox, list[str]]:
    """An HCS sandbox whose persistent volumes report *sizes* (GiB, or an
    exception to raise), recording stops and grows in call order."""
    monkeypatch.setattr(sys, "platform", "win32")
    sb = HcsSandbox(
        "dev",
        SandboxConfig(
            backend="hcs",
            base_image=str(tmp_path / "root.vhdx"),
            disk_size=disk_size,
            persistent_paths=list(sizes),
        ),
        str(tmp_path / "ws"),
        state_dir=tmp_path / "state",
    )
    by_file = {
        str(sb._sdir / H.persistent_vol_filename(gp)): size
        for gp, size in sizes.items()
    }
    calls: list[str] = []

    class HcsError(RuntimeError):
        pass

    def size_of(path: str) -> int:
        size = by_file[path]
        if isinstance(size, BaseException):
            raise HcsError(str(size))
        return size * _GIB

    fake = type(sys)("open_shrimp.sandbox.hcs_win")
    fake.HcsError = HcsError
    fake.vhdx_virtual_size = size_of
    fake.grow_vhdx = lambda path, gb: calls.append(f"grow {path} {gb}")
    monkeypatch.setitem(sys.modules, "open_shrimp.sandbox.hcs_win", fake)
    monkeypatch.setattr(
        sb, "_live_runtime_id", lambda: "rid" if running else None,
    )
    monkeypatch.setattr(sb, "stop", lambda: calls.append("stop"))
    return sb, calls


def test_a_running_guest_is_stopped_before_its_volume_grows(
    tmp_path, monkeypatch,
):
    """HCS holds an attached VHDX open; ResizeVirtualDisk needs it released."""
    sb, calls = _hcs_sandbox(
        tmp_path, monkeypatch, disk_size=40,
        sizes={"/home/claude/.cache": 20}, running=True,
    )

    sb._grow_persistent_volumes(log_file=None)

    vol = sb._sdir / H.persistent_vol_filename("/home/claude/.cache")
    assert calls == ["stop", f"grow {vol} 40"]


def test_only_smaller_volumes_grow_and_none_shrink(tmp_path, monkeypatch):
    sb, calls = _hcs_sandbox(
        tmp_path, monkeypatch, disk_size=40,
        sizes={"/a": 20, "/b": 40, "/c": 80}, running=False,
    )

    sb._grow_persistent_volumes(log_file=None)

    vol = sb._sdir / H.persistent_vol_filename("/a")
    assert calls == [f"grow {vol} 40"]


def test_volumes_at_size_leave_the_guest_running(tmp_path, monkeypatch):
    sb, calls = _hcs_sandbox(
        tmp_path, monkeypatch, disk_size=20,
        sizes={"/a": 20, "/b": OSError("in use")}, running=True,
    )

    sb._grow_persistent_volumes(log_file=None)

    assert calls == []


def test_mounting_a_volume_grows_its_filesystem(tmp_path, monkeypatch):
    sb, _calls = _hcs_sandbox(
        tmp_path, monkeypatch, disk_size=20, sizes={"/a": 20}, running=True,
    )
    commands: list[str] = []

    class _Chan:
        def run(self, cmd: str, **kwargs: Any) -> tuple[bool, str]:
            commands.append(cmd)
            return True, "MOUNT-OK ROOT-MOUNT-OK PV-OK PROVISION-DONE"

    sys.modules["open_shrimp.sandbox.hcs_win"].ControlChannel = (
        lambda *a, **kw: _Chan()
    )
    monkeypatch.setattr(sb, "_start_exec_agent", lambda c: None)
    monkeypatch.setattr(sb, "_configure_network", lambda c: None)
    sb._runtime_id = "rid"

    sb._provision_guest(log_file=None)

    (pv,) = [c for c in commands if "PV-OK" in c]
    assert "resize2fs" in pv
