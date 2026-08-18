"""Tests for the service install/uninstall module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from open_shrimp.config import write_config
from open_shrimp.service import (
    _detect_executable,
    _detect_platform,
    _generate_launchd_plist,
    _generate_systemd_unit,
    install_service,
    uninstall_service,
)
from open_shrimp.setup import build_config_dict, build_context_dict


def _write_config(tmp_path: Path) -> Path:
    """A config the core would really start from, which install requires.

    Built by the same functions the wizard builds a first config with, so this
    cannot go on passing once the shape they write stops loading.
    """
    config = tmp_path / "config.yaml"
    write_config(
        config,
        build_config_dict(
            "123456:token",
            1,
            "default",
            build_context_dict(str(tmp_path), "test"),
        ),
    )
    return config


class TestDetectPlatform:
    def test_linux(self) -> None:
        with patch("open_shrimp.service.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert _detect_platform() == "linux"

    def test_macos(self) -> None:
        with patch("open_shrimp.service.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _detect_platform() == "macos"

    def test_windows(self) -> None:
        with patch("open_shrimp.service.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert _detect_platform() == "windows"

    def test_unsupported(self) -> None:
        with patch("open_shrimp.service.sys") as mock_sys:
            mock_sys.platform = "sunos5"
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                _detect_platform()


class TestDetectExecutable:
    def test_found_on_path(self, tmp_path: Path) -> None:
        exe = tmp_path / "openshrimp"
        exe.touch()
        with patch("open_shrimp.service.shutil.which", return_value=str(exe)):
            result = _detect_executable()
        assert result == [str(exe.resolve())]

    def test_found_next_to_python(self, tmp_path: Path) -> None:
        fake_python = tmp_path / "python"
        fake_python.touch()
        exe = tmp_path / "openshrimp"
        exe.touch()
        with (
            patch("open_shrimp.service.shutil.which", return_value=None),
            patch("open_shrimp.service.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_python)
            result = _detect_executable()
        assert result == [str(exe.resolve())]

    def test_fallback_to_module(self, tmp_path: Path) -> None:
        fake_python = tmp_path / "python"
        fake_python.touch()
        with (
            patch("open_shrimp.service.shutil.which", return_value=None),
            patch("open_shrimp.service.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_python)
            result = _detect_executable()
        assert result == [str(fake_python), "-m", "open_shrimp"]


class TestGenerateSystemdUnit:
    def test_basic(self) -> None:
        unit = _generate_systemd_unit(["/usr/bin/openshrimp"], "/etc/config.yaml")
        assert "ExecStart=/usr/bin/openshrimp --config /etc/config.yaml" in unit
        assert "WantedBy=default.target" in unit
        assert "Restart=on-failure" in unit
        assert "ANTHROPIC_API_KEY" not in unit


class TestGenerateLaunchdPlist:
    def test_basic(self) -> None:
        plist = _generate_launchd_plist(["/usr/bin/openshrimp"], "/etc/config.yaml")
        assert "<string>/usr/bin/openshrimp</string>" in plist
        assert "<string>--config</string>" in plist
        assert "<string>/etc/config.yaml</string>" in plist
        assert "com.openshrimp.bot" in plist
        assert "<key>KeepAlive</key>" in plist
        assert "openshrimp.stderr.log" in plist
        assert "ANTHROPIC_API_KEY" not in plist

    def test_module_fallback_args(self) -> None:
        plist = _generate_launchd_plist(
            ["/usr/bin/python", "-m", "open_shrimp"], "/etc/config.yaml"
        )
        assert "<string>/usr/bin/python</string>" in plist
        assert "<string>-m</string>" in plist
        assert "<string>open_shrimp</string>" in plist


class TestInstallService:
    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_executable", return_value=["/usr/bin/openshrimp"])
    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_install_linux(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = _write_config(tmp_path)
        svc_path = tmp_path / "open-shrimp.service"

        with patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path):
            install_service(str(config))

        assert svc_path.exists()
        content = svc_path.read_text()
        assert "ExecStart=/usr/bin/openshrimp" in content

        # Verify systemctl calls
        calls = mock_run.call_args_list
        cmd_lists = [c[0][0] for c in calls]
        assert ["systemctl", "--user", "daemon-reload"] in cmd_lists
        assert ["systemctl", "--user", "enable", "open-shrimp.service"] in cmd_lists
        assert ["systemctl", "--user", "start", "open-shrimp.service"] in cmd_lists

    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_executable", return_value=["/usr/bin/openshrimp"])
    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_install_macos(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = _write_config(tmp_path)
        svc_path = tmp_path / "com.openshrimp.bot.plist"
        log_dir = tmp_path / "logs"

        with (
            patch("open_shrimp.service._LAUNCHD_PLIST_PATH", svc_path),
            patch("open_shrimp.service._LAUNCHD_LOG_DIR", log_dir),
        ):
            install_service(str(config))

        assert svc_path.exists()
        content = svc_path.read_text()
        assert "<string>/usr/bin/openshrimp</string>" in content
        assert log_dir.exists()

    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_install_existing_declines(
        self,
        _plat: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = _write_config(tmp_path)
        svc_path = tmp_path / "open-shrimp.service"
        svc_path.write_text("existing")

        with (
            patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path),
            patch("open_shrimp.service.sys") as mock_sys,
            patch("builtins.input", return_value="n"),
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_sys.platform = "linux"
            install_service(str(config))

        # Should not have been overwritten
        assert svc_path.read_text() == "existing"

    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_a_missing_config_installs_nothing(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        svc_path = tmp_path / "open-shrimp.service"
        missing = tmp_path / "config.yaml"

        with patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path):
            with pytest.raises(SystemExit) as exc:
                install_service(str(missing))

        assert exc.value.code != 0
        assert "setup wizard" in capsys.readouterr().err
        # A unit that exists and has been enabled is the failure being
        # prevented, so neither half may have happened.
        assert not svc_path.exists()
        mock_run.assert_not_called()

    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_an_unparseable_config_installs_nothing(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        svc_path = tmp_path / "open-shrimp.service"
        config = tmp_path / "config.yaml"
        config.write_text("contexts: [", encoding="utf-8")

        with patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path):
            with pytest.raises(SystemExit) as exc:
                install_service(str(config))

        assert exc.value.code != 0
        # The wizard writes a config only where there is none, so this operator
        # is told what is wrong instead of being sent on a round trip.
        err = capsys.readouterr().err
        assert "cannot be loaded" in err
        assert "setup wizard" not in err
        assert not svc_path.exists()
        mock_run.assert_not_called()

    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_the_refusal_precedes_the_overwrite_prompt(
        self,
        _plat: MagicMock,
        _run_: MagicMock,
        tmp_path: Path,
    ) -> None:
        svc_path = tmp_path / "open-shrimp.service"
        svc_path.write_text("existing")
        missing = tmp_path / "config.yaml"

        # A terminal, so that reaching the prompt is what the refusal is being
        # tested against: without one, the non-interactive branch below exits
        # for its own reasons and the test would pass on either code.
        with (
            patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path),
            patch("open_shrimp.service.sys.stdin.isatty", return_value=True),
            patch("builtins.input") as mock_input,
        ):
            with pytest.raises(SystemExit):
                install_service(str(missing))

        mock_input.assert_not_called()
        assert svc_path.read_bytes() == b"existing"


class TestUninstallService:
    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_uninstall_linux(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        svc_path = tmp_path / "open-shrimp.service"
        svc_path.write_text("[Unit]\nDescription=test")

        with patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path):
            uninstall_service()

        assert not svc_path.exists()
        calls = mock_run.call_args_list
        cmd_lists = [c[0][0] for c in calls]
        assert ["systemctl", "--user", "stop", "open-shrimp.service"] in cmd_lists
        assert ["systemctl", "--user", "disable", "open-shrimp.service"] in cmd_lists
        assert ["systemctl", "--user", "daemon-reload"] in cmd_lists

    @patch("open_shrimp.service._run")
    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_uninstall_macos(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        svc_path = tmp_path / "com.openshrimp.bot.plist"
        svc_path.write_text("<plist>test</plist>")

        with patch("open_shrimp.service._LAUNCHD_PLIST_PATH", svc_path):
            uninstall_service()

        assert not svc_path.exists()

    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_uninstall_not_installed(
        self,
        _plat: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        svc_path = tmp_path / "open-shrimp.service"

        with patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path):
            uninstall_service()

        captured = capsys.readouterr()
        assert "not installed" in captured.out
