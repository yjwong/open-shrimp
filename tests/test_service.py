"""Tests for the service install/uninstall module."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from open_shrimp.config import write_config
from open_shrimp.service import (
    _current_user,
    _detect_executable,
    _detect_platform,
    _generate_launchd_plist,
    _generate_systemd_unit,
    _generate_windows_task_xml,
    _install_windows,
    _task_exists,
    _task_name,
    install_service,
    is_service_installed,
    uninstall_service,
)
from open_shrimp.setup import build_config_dict, build_context_dict


def _ok(args: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess:
    """A command that ran and was happy about it."""
    return subprocess.CompletedProcess(args, 0, "", "")


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
            {"default": build_context_dict(str(tmp_path), "test")},
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


class TestGenerateWindowsTaskXml:
    def test_the_task_xml_starts_the_core_with_its_config(self) -> None:
        xml = _generate_windows_task_xml(
            [r"C:\Program Files\OpenShrimp\openshrimp.exe"],
            r"C:\Users\me\config.yaml",
        )
        assert (
            r"<Command>C:\Program Files\OpenShrimp\openshrimp.exe</Command>" in xml
        )
        assert "--config" in xml
        assert r"C:\Users\me\config.yaml" in xml

    def test_the_module_fallback_keeps_its_own_arguments(self) -> None:
        # _detect_executable can hand back [python, "-m", "open_shrimp"], so
        # everything past the executable belongs in <Arguments>, not lost.
        xml = _generate_windows_task_xml(
            [r"C:\Python\python.exe", "-m", "open_shrimp"],
            r"C:\config.yaml",
        )
        assert r"<Command>C:\Python\python.exe</Command>" in xml
        assert (
            r"<Arguments>-m open_shrimp --config C:\config.yaml</Arguments>" in xml
        )

    def test_a_path_with_spaces_stays_one_argument(self) -> None:
        xml = _generate_windows_task_xml(
            [r"C:\openshrimp.exe"], r"C:\Users\my name\config.yaml"
        )
        assert r'"C:\Users\my name\config.yaml"' in xml

    def test_the_task_xml_carries_a_logon_trigger_and_no_time_limit(self) -> None:
        xml = _generate_windows_task_xml([r"C:\openshrimp.exe"], r"C:\config.yaml")
        assert "<LogonTrigger>" in xml
        # A task that times out is a bot that stops mid-afternoon with no
        # message.
        assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
        assert "<LogonType>InteractiveToken</LogonType>" in xml
        assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
        assert "<Interval>PT1M</Interval>" in xml

    def test_the_task_xml_never_carries_the_api_key(self) -> None:
        xml = _generate_windows_task_xml([r"C:\openshrimp.exe"], r"C:\config.yaml")
        assert "ANTHROPIC_API_KEY" not in xml

    def test_an_interpolated_ampersand_is_escaped(self) -> None:
        xml = _generate_windows_task_xml(
            [r"C:\Rock & Roll\openshrimp.exe"], r"C:\config.yaml"
        )
        assert "Rock &amp; Roll" in xml
        assert "Rock & Roll" not in xml


class TestCurrentUser:
    def test_a_workgroup_is_never_spelled_into_the_user_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A machine that is not domain-joined reports USERDOMAIN=WORKGROUP,
        # which names no authority: schtasks resolves no SID for
        # WORKGROUP\me and rejects the whole definition, not just the
        # trigger.  The fallback stays bare rather than guessing a domain.
        monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
        monkeypatch.setenv("USERNAME", "me")
        assert _current_user() == "me"

    def test_the_user_id_reaches_the_definition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "open_shrimp.service._current_user", lambda: r"WIN11SPIKE\me"
        )
        xml = _generate_windows_task_xml([r"C:\openshrimp.exe"], r"C:\config.yaml")
        assert r"<UserId>WIN11SPIKE\me</UserId>" in xml


class TestTaskExists:
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_a_disabled_task_still_holds_the_name(self, _plat: MagicMock) -> None:
        # The two questions must disagree here, and the disagreement is the
        # point: a disabled task starts nothing, so autostart is off, but it
        # still occupies the name and registering over it destroys whatever
        # its owner put there.
        query = "\ufeff" + "".join(
            f"{c}\x00"
            for c in "<Task><Settings><Enabled>false</Enabled></Settings></Task>"
        )
        with patch("open_shrimp.service._capture", return_value=query):
            assert is_service_installed(None) is False
            assert _task_exists(None) is True

    @patch("open_shrimp.service._capture", return_value=None)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_an_absent_task_holds_nothing(
        self,
        _plat: MagicMock,
        _cap: MagicMock,
    ) -> None:
        assert _task_exists(None) is False


class TestTaskName:
    def test_a_hostile_instance_name_cannot_retarget_the_task(self) -> None:
        name = _task_name('a" /Delete /TN "OpenShrimp')
        assert '"' not in name
        assert " " not in name


class TestIsServiceInstalled:
    @patch("open_shrimp.service._capture", return_value=None)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_a_missing_task_is_not_installed(
        self,
        _plat: MagicMock,
        _cap: MagicMock,
    ) -> None:
        assert is_service_installed(None) is False

    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_a_disabled_task_does_not_count_as_installed(
        self,
        _plat: MagicMock,
    ) -> None:
        # schtasks hands back UTF-16 that commonly arrives decoded as if it
        # were not, so the flag has to be found through the interleaved nulls.
        query = "\ufeff" + "".join(
            f"{c}\x00" for c in "<Task><Settings><Enabled>false</Enabled></Settings></Task>"
        )
        with patch("open_shrimp.service._capture", return_value=query):
            assert is_service_installed(None) is False

    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_an_enabled_task_is_installed(self, _plat: MagicMock) -> None:
        query = "\ufeff" + "".join(
            f"{c}\x00" for c in "<Task><Settings><Enabled>true</Enabled></Settings></Task>"
        )
        with patch("open_shrimp.service._capture", return_value=query):
            assert is_service_installed(None) is True

    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_an_agent_registered_without_being_loaded_is_installed(
        self,
        _plat: MagicMock,
        tmp_path: Path,
    ) -> None:
        # The answer is about the next login, not this instant: an agent
        # registered by a wizard that must not start a second core is not
        # loaded, and telling its operator to go and register it again would
        # be telling them to do what they just did.
        plist = tmp_path / "com.openshrimp.bot.plist"
        plist.write_text("<plist/>")

        with (
            patch("open_shrimp.service._LAUNCHD_PLIST_PATH", plist),
            patch(
                "open_shrimp.service._capture",
                return_value='disabled services = {\n\t"com.apple.other" => true\n}\n',
            ),
        ):
            assert is_service_installed(None) is True

    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_a_missing_plist_is_not_installed(
        self,
        _plat: MagicMock,
        tmp_path: Path,
    ) -> None:
        with patch(
            "open_shrimp.service._LAUNCHD_PLIST_PATH", tmp_path / "absent.plist"
        ):
            assert is_service_installed(None) is False

    # The verdict is spelled differently across OS versions; both spellings
    # mean the agent stays down.
    @pytest.mark.parametrize("verdict", ["true", "disabled"])
    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_a_disabled_agent_is_not_installed(
        self,
        _plat: MagicMock,
        verdict: str,
        tmp_path: Path,
    ) -> None:
        plist = tmp_path / "com.openshrimp.bot.plist"
        plist.write_text("<plist/>")
        listing = f'disabled services = {{\n\t"com.openshrimp.bot" => {verdict}\n}}\n'

        with (
            patch("open_shrimp.service._LAUNCHD_PLIST_PATH", plist),
            patch("open_shrimp.service._capture", return_value=listing),
        ):
            assert is_service_installed(None) is False

    @pytest.mark.parametrize("verdict", ["false", "enabled"])
    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_an_enabled_agent_is_installed(
        self,
        _plat: MagicMock,
        verdict: str,
        tmp_path: Path,
    ) -> None:
        plist = tmp_path / "com.openshrimp.bot.plist"
        plist.write_text("<plist/>")
        listing = f'disabled services = {{\n\t"com.openshrimp.bot" => {verdict}\n}}\n'

        with (
            patch("open_shrimp.service._LAUNCHD_PLIST_PATH", plist),
            patch("open_shrimp.service._capture", return_value=listing),
        ):
            assert is_service_installed(None) is True

    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_a_longer_label_is_not_mistaken_for_ours(
        self,
        _plat: MagicMock,
        tmp_path: Path,
    ) -> None:
        # The label is matched whole: a disabled com.openshrimp.bot.helper
        # says nothing about com.openshrimp.bot.
        plist = tmp_path / "com.openshrimp.bot.plist"
        plist.write_text("<plist/>")
        listing = 'disabled services = {\n\t"com.openshrimp.bot.helper" => true\n}\n'

        with (
            patch("open_shrimp.service._LAUNCHD_PLIST_PATH", plist),
            patch("open_shrimp.service._capture", return_value=listing),
        ):
            assert is_service_installed(None) is True

    @patch("open_shrimp.service._capture", return_value=None)
    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_an_unanswerable_query_leaves_the_plist_as_the_evidence(
        self,
        _plat: MagicMock,
        _cap: MagicMock,
        tmp_path: Path,
    ) -> None:
        # A disable is the exception; the plist is the rule.  A launchd that
        # would not answer must not delete the evidence that was in hand.
        plist = tmp_path / "com.openshrimp.bot.plist"
        plist.write_text("<plist/>")

        with patch("open_shrimp.service._LAUNCHD_PLIST_PATH", plist):
            assert is_service_installed(None) is True


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

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch(
        "open_shrimp.service._detect_executable", return_value=["/usr/bin/openshrimp"]
    )
    @patch("open_shrimp.service._detect_platform", return_value="linux")
    def test_the_wizard_path_enables_without_starting(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = _write_config(tmp_path)
        svc_path = tmp_path / "open-shrimp.service"

        with patch("open_shrimp.service._SYSTEMD_UNIT_PATH", svc_path):
            install_service(str(config), start=False)

        argv = [c[0][0] for c in mock_run.call_args_list]
        # Enabling is the whole point, and starts nothing.
        assert ["systemctl", "--user", "enable", "open-shrimp.service"] in argv
        # A start here is a second core polling one bot token.
        assert ["systemctl", "--user", "start", "open-shrimp.service"] not in argv

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch(
        "open_shrimp.service._detect_executable", return_value=["/usr/bin/openshrimp"]
    )
    @patch("open_shrimp.service._detect_platform", return_value="macos")
    def test_the_agent_still_loads_at_the_next_login(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Registering without starting must not degrade into not registering:
        # the plist is the whole of what brings the core back at the next
        # login, and RunAtLoad is the half of it that says so.
        config = _write_config(tmp_path)
        svc_path = tmp_path / "com.openshrimp.bot.plist"

        with (
            patch("open_shrimp.service._LAUNCHD_PLIST_PATH", svc_path),
            patch("open_shrimp.service._LAUNCHD_LOG_DIR", tmp_path / "logs"),
        ):
            install_service(str(config), start=False)

        plist = svc_path.read_text()
        assert "<key>RunAtLoad</key>" in plist
        assert plist.split("<key>RunAtLoad</key>", 1)[1].lstrip().startswith("<true/>")
        # Bootstrapping an agent with RunAtLoad set starts it immediately.
        argv = [c[0][0] for c in mock_run.call_args_list]
        assert not any("bootstrap" in a for a in argv)


class TestInstallWindows:
    @patch("open_shrimp.service._task_exists", return_value=False)
    def test_the_definition_is_utf16_with_a_bom(self, _installed: MagicMock) -> None:
        # Anything else and schtasks rejects it with an unhelpful parse error,
        # so the bytes are read back off disk before the finally deletes them.
        seen: dict[str, object] = {}

        def _read_the_definition(
            args: list[str], **_kw: object
        ) -> subprocess.CompletedProcess:
            xml_path = Path(args[args.index("/XML") + 1])
            seen["bytes"] = xml_path.read_bytes()
            seen["path"] = xml_path
            return _ok(args)

        with patch("open_shrimp.service._run", side_effect=_read_the_definition):
            _install_windows(None, [r"C:\openshrimp.exe"], r"C:\config.yaml")

        assert isinstance(seen["bytes"], bytes)
        assert seen["bytes"].startswith(b"\xff\xfe")
        assert not Path(str(seen["path"])).exists()

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._task_exists", return_value=False)
    def test_a_fresh_install_registers_the_task(
        self,
        _exists: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        _install_windows(None, [r"C:\openshrimp.exe"], r"C:\config.yaml")

        argv = [c[0][0] for c in mock_run.call_args_list]
        assert len(argv) == 1
        assert argv[0][:4] == ["schtasks", "/Create", "/TN", "OpenShrimp"]
        assert argv[0][-1] == "/F"

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._task_exists", return_value=True)
    @pytest.mark.parametrize(
        ("answer", "replaced"),
        [("n", False), ("", False), ("y", True), ("yes", True)],
    )
    def test_an_existing_task_is_replaced_only_when_asked(
        self,
        _exists: MagicMock,
        mock_run: MagicMock,
        answer: str,
        replaced: bool,
    ) -> None:
        # The task of this name may be the tray's, which supervises the core
        # and gives the user something to quit it with.  Anything short of
        # yes must leave it exactly where it is.
        with (
            patch("open_shrimp.service.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value=answer),
        ):
            _install_windows(None, [r"C:\openshrimp.exe"], r"C:\config.yaml")

        argv = [c[0][0] for c in mock_run.call_args_list]
        assert any("/Create" in a for a in argv) is replaced

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_a_disabled_task_is_still_asked_about(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        # Driven through the real predicate rather than a patched one: a
        # disabled task makes is_service_installed answer False, and gating on
        # that would replace the tray's registration without a word.
        query = "\ufeff" + "".join(
            f"{c}\x00"
            for c in "<Task><Settings><Enabled>false</Enabled></Settings></Task>"
        )
        with (
            patch("open_shrimp.service._capture", return_value=query),
            patch("open_shrimp.service.sys.stdin.isatty", return_value=False),
        ):
            with pytest.raises(SystemExit) as exc:
                _install_windows(None, [r"C:\openshrimp.exe"], r"C:\config.yaml")

        assert exc.value.code != 0
        mock_run.assert_not_called()

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._task_exists", return_value=True)
    def test_an_existing_task_stops_a_non_interactive_install(
        self,
        _exists: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        with patch("open_shrimp.service.sys.stdin.isatty", return_value=False):
            with pytest.raises(SystemExit) as exc:
                _install_windows(None, [r"C:\openshrimp.exe"], r"C:\config.yaml")

        assert exc.value.code != 0
        mock_run.assert_not_called()

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._task_exists", return_value=False)
    @patch(
        "open_shrimp.service._detect_executable",
        return_value=[r"C:\openshrimp.exe"],
    )
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_the_instance_name_reaches_the_task_name(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        _exists: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = _write_config(tmp_path)
        config.write_text(
            config.read_text(encoding="utf-8") + "instance_name: work\n",
            encoding="utf-8",
        )

        install_service(str(config))

        argv = [c[0][0] for c in mock_run.call_args_list]
        assert argv[0][argv[0].index("/TN") + 1] == "OpenShrimp-work"


class TestUninstallService:
    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_uninstall_deletes_the_task(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config = _write_config(tmp_path)

        uninstall_service(str(config))

        cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        assert ["schtasks", "/Delete", "/TN", "OpenShrimp", "/F"] in cmd_lists
        # Not a recipe for the user to run themselves.
        assert "schtasks" not in capsys.readouterr().out

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_uninstall_survives_an_unusable_config(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Someone who deleted their config still has a task to be rid of, and
        # the instance name that scopes it is exactly what the lost config
        # carried — so every OpenShrimp task goes, not the guessed one.
        listing = '"\\OpenShrimp","N/A","Ready"\n"\\OpenShrimp-work","N/A","Ready"\n'
        with patch("open_shrimp.service._capture", return_value=listing):
            uninstall_service(str(tmp_path / "gone.yaml"))

        cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        assert ["schtasks", "/Delete", "/TN", "OpenShrimp", "/F"] in cmd_lists
        assert ["schtasks", "/Delete", "/TN", "OpenShrimp-work", "/F"] in cmd_lists

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_a_named_instance_is_the_only_task_removed(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        # A config that loads names the task exactly, so nothing else is
        # touched — the wide removal is what a lost config costs, not the rule.
        config = _write_config(tmp_path)
        config.write_text(
            config.read_text(encoding="utf-8") + "instance_name: work\n",
            encoding="utf-8",
        )

        uninstall_service(str(config))

        cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        assert cmd_lists == [["schtasks", "/Delete", "/TN", "OpenShrimp-work", "/F"]]

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_a_task_in_a_folder_is_not_ours(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        listing = (
            '"\\OpenShrimp","N/A","Ready"\n'
            '"\\Vendor\\OpenShrimp-lookalike","N/A","Ready"\n'
        )
        with patch("open_shrimp.service._capture", return_value=listing):
            uninstall_service(str(tmp_path / "gone.yaml"))

        cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        assert cmd_lists == [["schtasks", "/Delete", "/TN", "OpenShrimp", "/F"]]

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_an_unreadable_task_list_is_not_an_empty_one(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Enumerating every task on the machine can time out; saying "not
        # installed" on the strength of a question nobody managed to ask
        # reports a removal that never happened.
        with patch("open_shrimp.service._capture", return_value=None):
            with pytest.raises(SystemExit) as exc:
                uninstall_service(str(tmp_path / "gone.yaml"))

        assert exc.value.code != 0
        assert "Nothing was removed" in capsys.readouterr().err
        mock_run.assert_not_called()

    @patch("open_shrimp.service._run", side_effect=_ok)
    @patch("open_shrimp.service._detect_platform", return_value="windows")
    def test_nothing_registered_says_so(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with patch("open_shrimp.service._capture", return_value=""):
            uninstall_service(str(tmp_path / "gone.yaml"))

        mock_run.assert_not_called()
        assert "not installed" in capsys.readouterr().out

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
            uninstall_service(str(tmp_path / "config.yaml"))

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
            uninstall_service(str(tmp_path / "config.yaml"))

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
            uninstall_service(str(tmp_path / "config.yaml"))

        captured = capsys.readouterr()
        assert "not installed" in captured.out
