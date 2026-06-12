"""Tests for libvirt sandbox helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from open_shrimp.sandbox.libvirt_helpers import _parse_virtiofsd_version


def test_parse_virtiofsd_version_accepts_openshrimp_build_metadata() -> None:
    result = MagicMock(stdout="virtiofsd 1.13.3+openshrimp.1\n")

    with patch("open_shrimp.sandbox.libvirt_helpers.subprocess.run", return_value=result):
        assert _parse_virtiofsd_version("/usr/bin/virtiofsd") == (1, 13, 3)
