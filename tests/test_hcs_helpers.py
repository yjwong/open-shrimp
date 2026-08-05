"""Unit tests for the platform-neutral HCS backend helpers.

Everything here runs on Linux — the Windows-only plumbing
(:mod:`open_shrimp.sandbox.hcs_win`) is not imported.
"""

from __future__ import annotations

import ipaddress
import json

import pytest

from open_shrimp.sandbox import hcs_helpers as H


def test_windows_to_guest_path_drops_drive_and_flips_separators():
    assert H.windows_to_guest_path(r"C:\Users\spike\repo") == "/Users/spike/repo"
    assert H.windows_to_guest_path(r"D:\work") == "/work"


def test_windows_to_guest_path_handles_forward_slashes():
    assert H.windows_to_guest_path("C:/Users/spike/x") == "/Users/spike/x"


def test_persistent_vol_label_matches_libvirt_scheme():
    label = H.persistent_vol_label("/home/claude/.cache")
    assert label.startswith("pv-")
    assert len(label) <= 16
    # Deterministic.
    assert label == H.persistent_vol_label("/home/claude/.cache")
    assert label != H.persistent_vol_label("/other")


def test_persistent_vol_filename_is_stable_and_scoped():
    fn = H.persistent_vol_filename("/var/lib/docker")
    assert fn.endswith(".vhdx")
    assert fn == H.persistent_vol_filename("/var/lib/docker")
    assert fn != H.persistent_vol_filename("/var/lib/other")


def test_persistent_dev_name_starts_at_sdb():
    # LUN 0 (/dev/sda) is the rootfs; persistent volumes start at sdb.
    assert H.persistent_dev_name(0) == "sdb"
    assert H.persistent_dev_name(1) == "sdc"


def test_vsock_service_id_encodes_port():
    assert H.vsock_service_id(0x5000) == "00005000-facb-11e6-bd58-64006a7986d3"
    assert H.vsock_service_id(0x5001) == "00005001-facb-11e6-bd58-64006a7986d3"


def test_pick_subnet_avoids_collisions():
    taken = [
        ipaddress.ip_network("192.168.222.0/24"),
        ipaddress.ip_network("192.168.223.0/24"),
    ]
    picked = H.pick_subnet(taken)
    assert not any(picked.overlaps(t) for t in taken)


def test_pick_subnet_avoids_wide_overlap():
    # A /20 that swallows the first two /24 candidates.
    taken = [ipaddress.ip_network("192.168.216.0/21")]
    picked = H.pick_subnet(taken)
    assert not picked.overlaps(taken[0])


def test_compose_network_settings_shape():
    subnet = ipaddress.ip_network("192.168.222.0/24")
    doc = json.loads(H.compose_network_settings("ctx-nat", subnet))
    assert doc["Type"] == "NAT"
    assert doc["Name"] == "ctx-nat"
    ipam = doc["Ipams"][0]["Subnets"][0]
    assert ipam["IpAddressPrefix"] == "192.168.222.0/24"
    assert ipam["Routes"][0]["NextHop"] == "192.168.222.1"


def test_compose_vm_config_devices_and_ordering():
    cfg = H.compose_vm_config(
        owner="openshrimp-hcs",
        kernel_path=r"C:\kernel",
        initrd_path=r"C:\initrd.img",
        memory_mb=2048,
        cpus=2,
        console_pipe=r"\\.\pipe\x",
        endpoint_guid="ep-guid",
        endpoint_mac="00-15-5D-00-00-01",
        p9_shares=[
            ("ws", r"C:\repo", 564, 0),
            ("home", r"C:\home", 565, 0),
        ],
        scsi_disks=[r"C:\rootfs.vhdx", r"C:\pv-a.vhdx"],
        connect_sddl="D:P(A;;FA;;;S-1-5-21-1-2-3-1001)",
    )
    vm = cfg["VirtualMachine"]
    devs = vm["Devices"]
    # Never terminate on last handle closed — the guest outlives the bot.
    assert cfg["ShouldTerminateOnLastHandleClosed"] is False
    # The connect SD is the caller-supplied one (narrowed to the bot's SID);
    # the bind SD stays allow-all for the guest→host relay direction.
    sd = devs["HvSocket"]["HvSocketConfig"]
    assert sd["DefaultConnectSecurityDescriptor"] == "D:P(A;;FA;;;S-1-5-21-1-2-3-1001)"
    assert sd["DefaultBindSecurityDescriptor"] == "D:P(A;;FA;;;WD)"
    # Rootfs must be SCSI LUN 0.
    assert devs["Scsi"]["0"]["Attachments"]["0"]["Path"] == r"C:\rootfs.vhdx"
    assert devs["Scsi"]["0"]["Attachments"]["1"]["Path"] == r"C:\pv-a.vhdx"
    # Both shares carried.
    names = {s["Name"] for s in devs["Plan9"]["Shares"]}
    assert names == {"ws", "home"}
    # LinuxKernelDirect chassis.
    kd = vm["Chipset"]["LinuxKernelDirect"]
    assert kd["KernelFilePath"] == r"C:\kernel"
    assert "loglevel=4" in kd["KernelCmdLine"]


def test_compose_vm_config_connect_sddl_defaults_allow_all():
    cfg = H.compose_vm_config(
        owner="o", kernel_path="k", initrd_path="i", memory_mb=1024, cpus=1,
        console_pipe="p", endpoint_guid="e", endpoint_mac="m",
        p9_shares=[], scsi_disks=["r"],
    )
    sd = cfg["VirtualMachine"]["Devices"]["HvSocket"]["HvSocketConfig"]
    assert sd["DefaultConnectSecurityDescriptor"] == "D:P(A;;FA;;;WD)"


def test_config_fingerprint_changes_with_inputs(tmp_path):
    kernel = tmp_path / "kernel"
    kernel.write_bytes(b"k")
    initrd = tmp_path / "initrd"
    initrd.write_bytes(b"i")
    base = dict(
        kernel_path=kernel, initrd_path=initrd, base_image="C:/base.vhdx",
        project_dir="C:/repo", additional_directories=[], persistent_paths=[],
        memory_mb=2048, cpus=2, provision=None,
    )
    fp1 = H.config_fingerprint(**base)
    assert fp1 == H.config_fingerprint(**base)
    changed = dict(base, persistent_paths=["/data"])
    assert H.config_fingerprint(**changed) != fp1
    changed = dict(base, memory_mb=4096)
    assert H.config_fingerprint(**changed) != fp1
    changed = dict(base, computer_use=True)
    assert H.config_fingerprint(**changed) != fp1


def test_gui_image_path_sits_next_to_base():
    assert H.gui_image_path(r"C:\images\root.vhdx") == r"C:\images\root-gui.vhdx"
    assert H.gui_image_path("C:/images/root.vhdx") == r"C:\images\root-gui.vhdx"


def test_rootfs_fingerprint_distinguishes_gui(tmp_path):
    img = tmp_path / "root.vhdx"
    img.write_bytes(b"x")
    base_fp = H.rootfs_fingerprint(str(img), gui=False)
    assert base_fp == H.rootfs_fingerprint(str(img), gui=False)
    # Same template file, different variant flag -> different identity.
    assert H.rootfs_fingerprint(str(img), gui=True) != base_fp
    # Template content drift -> different identity.
    img.write_bytes(b"xy")
    assert H.rootfs_fingerprint(str(img), gui=False) != base_fp


def test_render_launcher_source_substitutes_and_escapes():
    src = H.render_launcher_source(r"C:\state\launch.json")
    assert "__LAUNCH_JSON__" not in src
    assert "__EXIT_CONNECT__" not in src
    assert "__EXIT_PROTOCOL__" not in src
    assert str(H.LAUNCHER_EXIT_CONNECT) in src
    assert r"C:\state\launch.json" in src


def test_render_launcher_doubles_embedded_quotes():
    src = H.render_launcher_source(r'C:\a"b\launch.json')
    # Inside a C# verbatim string a literal quote must be doubled.
    assert r'C:\a""b\launch.json' in src
