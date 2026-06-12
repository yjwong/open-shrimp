"""Tests for libvirt sandbox helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from open_shrimp.sandbox.libvirt_helpers import (
    _parse_virtiofsd_version,
    start_virtiofsd,
)


def test_start_virtiofsd_starts_new_session(tmp_path: Path) -> None:
    """virtiofsd must not receive terminal Ctrl-C with OpenShrimp."""
    proc = MagicMock()

    with (
        patch(
            "open_shrimp.sandbox.libvirt_helpers.find_virtiofsd",
            return_value="/usr/bin/virtiofsd",
        ),
        patch("open_shrimp.sandbox.libvirt_helpers.subprocess.Popen", return_value=proc) as popen,
    ):
        assert start_virtiofsd(tmp_path / "fs.sock", "/shared") is proc

    popen.assert_called_once()
    assert popen.call_args.kwargs["start_new_session"] is True


def test_parse_virtiofsd_version_accepts_openshrimp_build_metadata() -> None:
    result = MagicMock(stdout="virtiofsd 1.13.3+openshrimp.1\n")

    with patch("open_shrimp.sandbox.libvirt_helpers.subprocess.run", return_value=result):
        assert _parse_virtiofsd_version("/usr/bin/virtiofsd") == (1, 13, 3)
