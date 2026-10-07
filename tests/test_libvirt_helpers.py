"""Tests for libvirt sandbox helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from open_shrimp.sandbox.libvirt_helpers import (
    _parse_virtiofsd_version,
    extract_sizing_from_xml,
    generate_domain_xml,
)


def test_extract_sizing_reads_libvirt_kib_memory() -> None:
    live_xml = (
        "<domain type='kvm'><name>openshrimp-dev</name>"
        "<memory unit='KiB'>8388608</memory>"
        "<currentMemory unit='KiB'>8388608</currentMemory>"
        "<vcpu placement='static'>4</vcpu></domain>"
    )
    assert extract_sizing_from_xml(live_xml) == (8192, 4)


def test_extract_sizing_matches_generated_domain_xml(tmp_path) -> None:
    xml = generate_domain_xml(
        "openshrimp-dev",
        overlay_path=tmp_path / "overlay.qcow2",
        cloud_init_iso=tmp_path / "cloud-init.iso",
        serial_log=tmp_path / "serial.log",
        ssh_port=2222,
        memory_mb=6144,
        vcpus=3,
    )
    assert extract_sizing_from_xml(xml) == (6144, 3)


def test_parse_virtiofsd_version_accepts_openshrimp_build_metadata() -> None:
    result = MagicMock(stdout="virtiofsd 1.13.3+openshrimp.1\n")

    with patch("open_shrimp.sandbox.libvirt_helpers.subprocess.run", return_value=result):
        assert _parse_virtiofsd_version("/usr/bin/virtiofsd") == (1, 13, 3)
