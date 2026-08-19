"""Tests for moonshine-stt binary resolution."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from open_shrimp import binaries
from open_shrimp.stt import ensure_moonshine_stt, managed_moonshine_stt


class TestManagedMoonshineStt:
    """Tests for managed_moonshine_stt()."""

    def test_names_the_bin_dir_copy(self, tmp_path: Path) -> None:
        """Named the way this platform's download writes it."""
        with patch("open_shrimp.binaries.BIN_DIR", tmp_path):
            assert managed_moonshine_stt() == (
                tmp_path / f"moonshine-stt{binaries.EXE_SUFFIX}"
            )


class TestEnsureMoonshineStt:
    """Tests for ensure_moonshine_stt()."""

    @pytest.mark.asyncio
    async def test_already_downloaded(self, tmp_path: Path) -> None:
        """Should return the managed path when it is already there."""
        fake_bin = tmp_path / f"moonshine-stt{binaries.EXE_SUFFIX}"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

        with (
            patch("open_shrimp.binaries.BIN_DIR", tmp_path),
            patch("open_shrimp.stt._download_moonshine_stt") as mock_download,
        ):
            result = await ensure_moonshine_stt()

        assert result == str(fake_bin)
        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_if_absent(self, tmp_path: Path) -> None:
        """Should download when the managed copy is not there."""
        with (
            patch("open_shrimp.binaries.BIN_DIR", tmp_path),
            patch(
                "open_shrimp.stt._download_moonshine_stt",
                return_value=str(tmp_path / "moonshine-stt"),
            ) as mock_download,
        ):
            result = await ensure_moonshine_stt()

        assert result == str(tmp_path / "moonshine-stt")
        mock_download.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_a_system_install(self, tmp_path: Path) -> None:
        """A build on $PATH is of unknown vintage and is never run."""
        with (
            patch("open_shrimp.binaries.BIN_DIR", tmp_path),
            patch("shutil.which", return_value="/usr/bin/moonshine-stt") as which,
            patch(
                "open_shrimp.stt._download_moonshine_stt",
                return_value=str(tmp_path / "moonshine-stt"),
            ) as mock_download,
        ):
            result = await ensure_moonshine_stt()

        assert result != "/usr/bin/moonshine-stt"
        which.assert_not_called()
        mock_download.assert_called_once()
