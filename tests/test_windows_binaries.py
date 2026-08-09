"""Windows-awareness of the downloaded-binary paths.

Covers the three download sites — cloudflared (``tunnel``), moonshine-stt
(``stt``) and the self-updating PyApp binary (``updater``) — plus the shared
naming rules in ``binaries``.

The Windows behaviour these encode was measured on a real Windows 11 host:
``platform.machine()`` reports ``AMD64``, overwriting a running ``.exe`` is
denied with ``WinError 5``, and renaming it out of the way is permitted.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from open_shrimp import binaries
from open_shrimp.stt import _BINARY_MAP as _STT_MAP
from open_shrimp.tunnel import _BINARY_MAP as _TUNNEL_MAP
from open_shrimp.updater import (
    _ASSET_MAP,
    UpdateInfo,
    _install_binary,
    get_platform_asset_name,
    purge_displaced_binary,
)


class TestPlatformMaps:
    """Every platform the release workflow builds for must be mapped."""

    def test_updater_has_windows_asset(self) -> None:
        assert _ASSET_MAP[("Windows", "AMD64")] == "openshrimp-windows-x86_64.exe"

    def test_updater_covers_every_released_asset(self) -> None:
        assert set(_ASSET_MAP.values()) == {
            "openshrimp-linux-x86_64",
            "openshrimp-linux-aarch64",
            "openshrimp-macos-aarch64",
            "openshrimp-macos-x86_64",
            "openshrimp-windows-x86_64.exe",
        }

    def test_stt_has_windows_binary(self) -> None:
        assert _STT_MAP[("Windows", "AMD64")] == "moonshine-stt-windows-x86_64.exe"

    def test_tunnel_has_windows_binary(self) -> None:
        assert _TUNNEL_MAP[("Windows", "AMD64")] == "cloudflared-windows-amd64.exe"

    def test_windows_asset_name_resolves(self) -> None:
        with patch("open_shrimp.updater.platform") as mock_platform:
            mock_platform.system.return_value = "Windows"
            mock_platform.machine.return_value = "AMD64"
            assert get_platform_asset_name() == "openshrimp-windows-x86_64.exe"

    def test_windows_arm64_has_no_asset(self) -> None:
        """No ARM64 Windows binary is built, so auto-update must stay off."""
        with patch("open_shrimp.updater.platform") as mock_platform:
            mock_platform.system.return_value = "Windows"
            mock_platform.machine.return_value = "ARM64"
            assert get_platform_asset_name() is None


class TestBinaryNaming:
    """A downloaded binary must be spelled the way the OS can execute it."""

    def test_posix_name_has_no_suffix(self, tmp_path: Path) -> None:
        with (
            patch.object(binaries, "EXE_SUFFIX", ""),
            patch.object(binaries, "BIN_DIR", tmp_path),
        ):
            assert binaries.local_binary_path("cloudflared") == tmp_path / "cloudflared"

    def test_windows_name_carries_exe(self, tmp_path: Path) -> None:
        with (
            patch.object(binaries, "EXE_SUFFIX", ".exe"),
            patch.object(binaries, "BIN_DIR", tmp_path),
        ):
            assert (
                binaries.local_binary_path("cloudflared") == tmp_path / "cloudflared.exe"
            )

    def test_windows_lookup_finds_the_downloaded_name(self, tmp_path: Path) -> None:
        """What the download writes is what the lookup must find."""
        with (
            patch.object(binaries, "EXE_SUFFIX", ".exe"),
            patch.object(binaries, "BIN_DIR", tmp_path),
            patch("shutil.which", return_value=None),
        ):
            target = binaries.local_binary_path("moonshine-stt")
            assert binaries.find_binary("moonshine-stt") is None
            target.write_bytes(b"MZ")
            target.chmod(0o755)  # Windows infers this from the extension.
            assert binaries.find_binary("moonshine-stt") == str(target)

    def test_windows_lookup_ignores_bare_name(self, tmp_path: Path) -> None:
        """A suffix-less file is not executable on Windows and must not match."""
        (tmp_path / "cloudflared").write_bytes(b"MZ")
        with (
            patch.object(binaries, "EXE_SUFFIX", ".exe"),
            patch.object(binaries, "BIN_DIR", tmp_path),
            patch("shutil.which", return_value=None),
        ):
            assert binaries.find_binary("cloudflared") is None

    def test_make_executable_sets_mode_on_posix(self, tmp_path: Path) -> None:
        # Asserted on the chmod call, not the resulting st_mode: Windows
        # synthesises the execute bits from the file extension, so the mode
        # a real Windows host reports back says nothing about the call.
        target = tmp_path / "cloudflared"
        target.write_bytes(b"#!/bin/sh\n")
        with (
            patch("open_shrimp.binaries.sys.platform", "linux"),
            patch.object(Path, "chmod") as chmod,
        ):
            binaries.make_executable(target)

        mode = chmod.call_args.args[0]
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH

    def test_make_executable_is_a_noop_on_windows(self, tmp_path: Path) -> None:
        """Windows tracks executability by extension, not by a mode bit."""
        target = tmp_path / "cloudflared.exe"
        target.write_bytes(b"MZ")
        with (
            patch("open_shrimp.binaries.sys.platform", "win32"),
            patch.object(Path, "chmod") as chmod,
        ):
            binaries.make_executable(target)

        chmod.assert_not_called()


class TestInstallBinary:
    """The self-replace mechanism used by ``download_and_replace``."""

    def _staged(self, tmp_path: Path) -> tuple[Path, Path]:
        target = tmp_path / "openshrimp.exe"
        target.write_bytes(b"old")
        target.chmod(0o755)
        new = tmp_path / ".openshrimp.exe.update.tmp"
        new.write_bytes(b"new")
        return new, target

    def test_posix_overwrites_in_place(self, tmp_path: Path) -> None:
        new, target = self._staged(tmp_path)
        with patch("open_shrimp.updater.sys.platform", "linux"):
            _install_binary(new, target)

        assert target.read_bytes() == b"new"
        assert target.stat().st_mode & stat.S_IXUSR
        assert not new.exists()
        assert not (tmp_path / "openshrimp.exe.old").exists()

    def test_windows_displaces_the_running_image(self, tmp_path: Path) -> None:
        """Windows refuses the overwrite, so the old image is renamed away."""
        new, target = self._staged(tmp_path)
        with patch("open_shrimp.updater.sys.platform", "win32"):
            _install_binary(new, target)

        assert target.read_bytes() == b"new"
        assert (tmp_path / "openshrimp.exe.old").read_bytes() == b"old"
        assert not new.exists()

    def test_windows_replaces_a_leftover_displaced_binary(
        self, tmp_path: Path
    ) -> None:
        """A displaced binary from an earlier update must not block this one."""
        new, target = self._staged(tmp_path)
        (tmp_path / "openshrimp.exe.old").write_bytes(b"ancient")
        with patch("open_shrimp.updater.sys.platform", "win32"):
            _install_binary(new, target)

        assert target.read_bytes() == b"new"
        assert (tmp_path / "openshrimp.exe.old").read_bytes() == b"old"

    def test_windows_rolls_back_when_the_move_in_fails(self, tmp_path: Path) -> None:
        """A half-done replace would leave no binary at the launched path."""
        new, target = self._staged(tmp_path)
        real_replace = os.replace
        calls: list[tuple[Path, Path]] = []

        def flaky_replace(src: object, dst: object) -> None:
            calls.append((Path(str(src)), Path(str(dst))))
            if len(calls) == 2:
                raise PermissionError(13, "Access is denied")
            real_replace(src, dst)

        with (
            patch("open_shrimp.updater.sys.platform", "win32"),
            patch("open_shrimp.updater.os.replace", flaky_replace),
        ):
            with pytest.raises(PermissionError):
                _install_binary(new, target)

        assert target.read_bytes() == b"old"
        assert not (tmp_path / "openshrimp.exe.old").exists()


class TestPurgeDisplacedBinary:
    """Cleanup of the image a previous self-replace left behind."""

    def test_removes_displaced_binary(self, tmp_path: Path) -> None:
        target = tmp_path / "openshrimp.exe"
        target.write_bytes(b"new")
        displaced = tmp_path / "openshrimp.exe.old"
        displaced.write_bytes(b"old")

        with patch("open_shrimp.updater.pyapp_binary_path", return_value=target):
            purge_displaced_binary()

        assert not displaced.exists()
        assert target.exists()

    def test_tolerates_a_still_mapped_image(self, tmp_path: Path) -> None:
        """Windows denies the unlink while the old process still runs."""
        target = tmp_path / "openshrimp.exe"
        target.write_bytes(b"new")

        def denied(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(13, "Access is denied")

        with (
            patch("open_shrimp.updater.pyapp_binary_path", return_value=target),
            patch("pathlib.Path.unlink", denied),
        ):
            purge_displaced_binary()  # must not raise

    def test_noop_outside_pyapp(self) -> None:
        with patch("open_shrimp.updater.pyapp_binary_path", return_value=None):
            purge_displaced_binary()


class TestDownloadAndReplace:
    """End-to-end wiring of download -> install, without a network."""

    @pytest.mark.asyncio
    async def test_windows_download_leaves_new_binary_at_the_launched_path(
        self, tmp_path: Path
    ) -> None:
        from open_shrimp import updater

        target = tmp_path / "openshrimp.exe"
        target.write_bytes(b"old")

        async def fake_stream_into(_info: object, dest: Path) -> None:
            dest.write_bytes(b"new")

        info = UpdateInfo(
            version="9.9.9",
            download_url="https://example.invalid/openshrimp-windows-x86_64.exe",
            release_url="https://example.invalid/release",
            release_notes="",
            asset_name="openshrimp-windows-x86_64.exe",
        )

        with (
            patch("open_shrimp.updater.pyapp_binary_path", return_value=target),
            patch("open_shrimp.updater.sys.platform", "win32"),
            patch.object(updater, "_stream_to_file", fake_stream_into),
        ):
            await updater.download_and_replace(info)

        assert target.read_bytes() == b"new"
        assert (tmp_path / "openshrimp.exe.old").read_bytes() == b"old"
        assert not (tmp_path / ".openshrimp.exe.update.tmp").exists()
