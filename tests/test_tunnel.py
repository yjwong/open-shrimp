"""Tests for the cloudflared tunnel module."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_shrimp import binaries
from open_shrimp.tunnel import (
    _CONFIG_BODY,
    _get_binary_name,
    _parse_tunnel_url,
    _tunnel_env,
    _write_config,
    ensure_cloudflared,
    managed_cloudflared,
    start_tunnel,
    stop_tunnel,
)


class TestGetBinaryName:
    """Tests for _get_binary_name()."""

    def test_linux_x86_64(self) -> None:
        with patch("open_shrimp.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mock_platform.machine.return_value = "x86_64"
            assert _get_binary_name() == "cloudflared-linux-amd64"

    def test_linux_aarch64(self) -> None:
        with patch("open_shrimp.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mock_platform.machine.return_value = "aarch64"
            assert _get_binary_name() == "cloudflared-linux-arm64"

    def test_darwin_arm64(self) -> None:
        with patch("open_shrimp.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Darwin"
            mock_platform.machine.return_value = "arm64"
            assert _get_binary_name() == "cloudflared-darwin-arm64.tgz"

    def test_windows_amd64(self) -> None:
        with patch("open_shrimp.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Windows"
            mock_platform.machine.return_value = "AMD64"
            assert _get_binary_name() == "cloudflared-windows-amd64.exe"

    def test_unsupported_platform(self) -> None:
        with patch("open_shrimp.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "FreeBSD"
            mock_platform.machine.return_value = "x86_64"
            assert _get_binary_name() is None


class TestManagedCloudflared:
    """Tests for managed_cloudflared()."""

    def test_names_the_bin_dir_copy(self, tmp_path: Path) -> None:
        """Should name the managed bin directory's copy, however spelled.

        Named the way this platform's download writes it — bare on POSIX,
        cloudflared.exe on Windows.
        """
        with patch("open_shrimp.binaries.BIN_DIR", tmp_path):
            assert managed_cloudflared() == (
                tmp_path / f"cloudflared{binaries.EXE_SUFFIX}"
            )


class TestTunnelEnv:
    """Tests for _tunnel_env()."""

    def test_strips_cloudflared_settings(self) -> None:
        """Every variable cloudflared reads is dropped, others survive."""
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/someone",
            "TUNNEL_LOGFILE": "/tmp/elsewhere.log",
            "TUNNEL_URL": "http://evil",
            "NO_AUTOUPDATE": "0",
            "NO_TLS_VERIFY": "1",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _tunnel_env()

        assert result == {"PATH": "/usr/bin", "HOME": "/home/someone"}


class TestWriteConfig:
    """Tests for _write_config()."""

    def test_writes_a_non_empty_config(self, tmp_path: Path) -> None:
        """cloudflared errors on an empty config, so ours must have content."""
        target = tmp_path / "nested" / "cloudflared.yml"
        with patch("open_shrimp.tunnel.CONFIG_PATH", target):
            assert _write_config() == target

        assert target.read_text() == _CONFIG_BODY
        assert _CONFIG_BODY.strip()

    def test_overwrites_previous_contents(self, tmp_path: Path) -> None:
        """The file cloudflared reads is ours on every start, not once."""
        target = tmp_path / "cloudflared.yml"
        target.write_text("loglevel: debug\n")

        with patch("open_shrimp.tunnel.CONFIG_PATH", target):
            _write_config()

        assert target.read_text() == _CONFIG_BODY


class TestParseTunnelUrl:
    """Tests for _parse_tunnel_url()."""

    @pytest.mark.asyncio
    async def test_parses_url_from_stderr(self) -> None:
        """Should extract the trycloudflare.com URL from stderr output."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()

        lines = [
            b"2024-01-01 INFO Starting tunnel\n",
            b"2024-01-01 INFO +----------------------------+\n",
            b"2024-01-01 INFO | https://foo-bar-baz.trycloudflare.com |\n",
            b"2024-01-01 INFO +----------------------------+\n",
        ]
        mock_proc.stderr.readline = AsyncMock(side_effect=lines)

        url = await _parse_tunnel_url(mock_proc, timeout=5.0)
        assert url == "https://foo-bar-baz.trycloudflare.com"

    @pytest.mark.asyncio
    async def test_process_exits_before_url(self) -> None:
        """Should raise RuntimeError if process exits without printing URL."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=1)

        with pytest.raises(RuntimeError, match="exited with code 1"):
            await _parse_tunnel_url(mock_proc, timeout=5.0)

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Should raise RuntimeError on timeout."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.terminate = MagicMock()

        # readline never returns a URL, just keeps returning non-matching lines.
        async def slow_readline() -> bytes:
            await asyncio.sleep(10)
            return b"no url here\n"

        mock_proc.stderr.readline = slow_readline

        with pytest.raises(RuntimeError, match="Timed out"):
            await _parse_tunnel_url(mock_proc, timeout=0.1)


class TestEnsureCloudflared:
    """Tests for ensure_cloudflared()."""

    @pytest.mark.asyncio
    async def test_already_downloaded(self, tmp_path: Path) -> None:
        """Should return the managed path when it is already there."""
        fake_bin = tmp_path / f"cloudflared{binaries.EXE_SUFFIX}"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

        with (
            patch("open_shrimp.binaries.BIN_DIR", tmp_path),
            patch("open_shrimp.tunnel._download_cloudflared") as mock_download,
        ):
            result = await ensure_cloudflared()

        assert result == str(fake_bin)
        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_if_absent(self, tmp_path: Path) -> None:
        """Should download when the managed copy is not there."""
        with (
            patch("open_shrimp.binaries.BIN_DIR", tmp_path),
            patch(
                "open_shrimp.tunnel._download_cloudflared",
                return_value=str(tmp_path / "cloudflared"),
            ) as mock_download,
        ):
            result = await ensure_cloudflared()

        assert result == str(tmp_path / "cloudflared")
        mock_download.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_a_system_install(self, tmp_path: Path) -> None:
        """A cloudflared on $PATH is not the one this project runs."""
        with (
            patch("open_shrimp.binaries.BIN_DIR", tmp_path),
            patch("shutil.which", return_value="/usr/bin/cloudflared") as which,
            patch(
                "open_shrimp.tunnel._download_cloudflared",
                return_value=str(tmp_path / "cloudflared"),
            ) as mock_download,
        ):
            result = await ensure_cloudflared()

        assert result != "/usr/bin/cloudflared"
        which.assert_not_called()
        mock_download.assert_called_once()


class TestStartTunnel:
    """Tests for start_tunnel()."""

    @pytest.mark.asyncio
    async def test_starts_and_returns_url(self, tmp_path: Path) -> None:
        """Should start cloudflared and return the tunnel URL."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stdout = AsyncMock()

        lines = [
            b"INFO Starting tunnel\n",
            b"INFO https://test-tunnel-abc.trycloudflare.com\n",
        ]
        mock_proc.stderr.readline = AsyncMock(side_effect=lines)

        config = tmp_path / "cloudflared.yml"
        env = {"PATH": "/usr/bin", "TUNNEL_LOGFILE": "/tmp/elsewhere.log"}

        with (
            patch("open_shrimp.tunnel.CONFIG_PATH", config),
            patch.dict("os.environ", env, clear=True),
            patch(
                "open_shrimp.tunnel.ensure_cloudflared",
                return_value=str(tmp_path / "cloudflared"),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            proc, url = await start_tunnel(8080)
            assert url == "https://test-tunnel-abc.trycloudflare.com"
            assert proc is mock_proc

            # Run against our own config, in an environment carrying none of
            # cloudflared's own settings.
            mock_exec.assert_called_once_with(
                str(tmp_path / "cloudflared"),
                "tunnel",
                "--config",
                str(config),
                "--url",
                "http://localhost:8080",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin"},
            )


class TestStopTunnel:
    """Tests for stop_tunnel()."""

    @pytest.mark.asyncio
    async def test_terminates_running_process(self) -> None:
        """Should terminate a running tunnel process."""
        mock_proc = MagicMock()
        mock_proc.returncode = None  # Still running.
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        await stop_tunnel(mock_proc)
        mock_proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_already_exited(self) -> None:
        """Should not terminate a process that already exited."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # Already exited.
        mock_proc.terminate = MagicMock()

        await stop_tunnel(mock_proc)
        mock_proc.terminate.assert_not_called()
