"""Install/uninstall OpenShrimp as a system service.

One command per platform for the same question — what starts the core when
nobody is at a terminal: a systemd user unit, a launchd user agent, or a
Windows logon task.
"""

from __future__ import annotations

import csv
import ctypes
import getpass
import io
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent
from typing import NoReturn
from xml.sax.saxutils import escape

from open_shrimp.config import Config, load_config

logger = logging.getLogger("open_shrimp")

# Service file locations
_SYSTEMD_UNIT_PATH = (
    Path.home() / ".config" / "systemd" / "user" / "open-shrimp.service"
)
_LAUNCHD_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / "com.openshrimp.bot.plist"
)
_LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "OpenShrimp"
_LAUNCHD_LABEL = "com.openshrimp.bot"

# GetUserNameEx's NameSamCompatible: the DOMAIN\user spelling a logon task's
# UserId must resolve from.
_NAME_SAM_COMPATIBLE = 2


def _detect_platform() -> str:
    """Return 'linux', 'macos', or 'windows'.

    Raises:
        RuntimeError: On unsupported platforms.
    """
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    raise RuntimeError(
        f"Unsupported platform: {sys.platform}. "
        "Only Linux (systemd), macOS (launchd), and Windows are supported."
    )


def _detect_executable() -> list[str]:
    """Find the best executable path for the service.

    Returns a list of arguments for the ``openshrimp`` command.  Typically a
    single-element list with the absolute path, but falls back to
    ``[sys.executable, "-m", "open_shrimp"]`` if the script is not found.
    """
    # 1. PyApp binary: PYAPP_PASS_LOCATION=1 sets PYAPP to the binary path.
    from open_shrimp.updater import pyapp_binary_path

    pyapp = pyapp_binary_path()
    if pyapp:
        return [str(pyapp)]

    # 2. Check if openshrimp is on PATH
    which = shutil.which("openshrimp")
    if which:
        return [str(Path(which).resolve())]

    # 3. Check for the script next to the running Python interpreter
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "openshrimp"
    if candidate.is_file():
        return [str(candidate.resolve())]

    # 4. Fallback: run as a module
    return [sys.executable, "-m", "open_shrimp"]


def _generate_systemd_unit(
    exec_args: list[str],
    config_path: str,
) -> str:
    """Generate a systemd user unit file."""
    exec_start = " ".join(shlex.quote(a) for a in exec_args)
    return dedent(f"""\
        [Unit]
        Description=OpenShrimp Telegram Bot
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart={exec_start} --config {shlex.quote(config_path)}
        Restart=on-failure
        RestartSec=5
        KillMode=mixed

        [Install]
        WantedBy=default.target
    """)


def _generate_launchd_plist(
    exec_args: list[str],
    config_path: str,
) -> str:
    """Generate a launchd user agent plist."""
    indent = "        "
    args_lines = [f"{indent}<string>{p}</string>" for p in exec_args]
    args_lines.append(f"{indent}<string>--config</string>")
    args_lines.append(f"{indent}<string>{config_path}</string>")
    args_xml = "\n".join(args_lines)

    log_dir = _LAUNCHD_LOG_DIR

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"',
        '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "    <key>Label</key>",
        f"    <string>{_LAUNCHD_LABEL}</string>",
        "    <key>ProgramArguments</key>",
        "    <array>",
        args_xml,
        "    </array>",
        "    <key>RunAtLoad</key>",
        "    <true/>",
        "    <key>KeepAlive</key>",
        "    <true/>",
        "    <key>StandardOutPath</key>",
        f"    <string>{log_dir}/openshrimp.stdout.log</string>",
        "    <key>StandardErrorPath</key>",
        f"    <string>{log_dir}/openshrimp.stderr.log</string>",
        "</dict>",
        "</plist>",
        "",
    ]
    return "\n".join(lines)


def _current_user() -> str:
    """The account the logon task triggers for, as Windows resolves it.

    Asked of Windows rather than assembled out of ``%USERDOMAIN%``: on a
    machine that is not domain-joined that variable reads ``WORKGROUP``, which
    names no authority, and a ``UserId`` that resolves to no SID is refused —
    taking the whole definition with it, not just the trigger.  The fallback
    is bare for the same reason: a name with no domain resolves against
    whichever authority owns the account, and a guessed one does not.
    """
    try:
        get_user_name_ex = ctypes.WinDLL("secur32").GetUserNameExW
        size = ctypes.c_ulong(0)
        get_user_name_ex(_NAME_SAM_COMPATIBLE, None, ctypes.byref(size))
        buf = ctypes.create_unicode_buffer(size.value or 256)
        size = ctypes.c_ulong(len(buf))
        if get_user_name_ex(_NAME_SAM_COMPATIBLE, buf, ctypes.byref(size)):
            return buf.value
    except (AttributeError, OSError):
        pass
    try:
        return os.environ.get("USERNAME") or getpass.getuser()
    except (KeyError, OSError):
        return ""


def _generate_windows_task_xml(
    exec_args: list[str],
    config_path: str,
) -> str:
    """Generate the logon task definition that starts the core.

    The mechanism is the tray's — schtasks fed an XML definition — and the
    target is not: this task runs the core itself, so the settings describe a
    supervisor that must never be timed out or hard-terminated rather than a
    job that runs and ends.
    """
    user = escape(_current_user())
    command = escape(exec_args[0])
    arguments = escape(
        " ".join(
            f'"{a}"' if " " in a else a
            for a in [*exec_args[1:], "--config", config_path]
        )
    )
    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>Starts OpenShrimp when you sign in.</Description>
          </RegistrationInfo>
          <Triggers>
            <LogonTrigger>
              <Enabled>true</Enabled>
              <UserId>{user}</UserId>
            </LogonTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <UserId>{user}</UserId>
              <LogonType>InteractiveToken</LogonType>
              <RunLevel>LeastPrivilege</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <AllowHardTerminate>false</AllowHardTerminate>
            <StartWhenAvailable>true</StartWhenAvailable>
            <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
            <IdleSettings>
              <StopOnIdleEnd>false</StopOnIdleEnd>
              <RestartOnIdle>false</RestartOnIdle>
            </IdleSettings>
            <AllowStartOnDemand>true</AllowStartOnDemand>
            <Enabled>true</Enabled>
            <Hidden>false</Hidden>
            <RunOnlyIfIdle>false</RunOnlyIfIdle>
            <WakeToRun>false</WakeToRun>
            <!-- A long-running supervisor, not a job: never time it out. -->
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
            <Priority>7</Priority>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>3</Count>
            </RestartOnFailure>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{command}</Command>
              <Arguments>{arguments}</Arguments>
            </Exec>
          </Actions>
        </Task>
    """)


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output."""
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _unit_file_path(platform: str) -> Path:
    """The unit file's path on a platform that installs one.

    Windows registers a task rather than writing a file and has no answer to
    this question, so it never asks it.
    """
    if platform == "linux":
        return _SYSTEMD_UNIT_PATH
    return _LAUNCHD_PLIST_PATH


def _may_replace(what: str) -> bool:
    """Whether the operator consents to replacing *what*.

    Autostart is registered under one name per instance on every platform, so
    whatever holds that name was put there by somebody — possibly a different
    owner, as on Windows, where the name is also the tray's.  Consent is asked
    for once here rather than per platform, so that one command cannot answer
    the same question three ways.  Silence is a no, and there is no way to say
    yes without a terminal.
    """
    if not sys.stdin.isatty():
        print(what, file=sys.stderr)
        print("Run interactively to overwrite.", file=sys.stderr)
        sys.exit(1)

    print(what)
    try:
        answer = input("Overwrite? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nInstall cancelled.")
        return False
    if answer not in ("y", "yes"):
        print("Install cancelled.")
        return False
    return True


def _print_banner(headline: str, commands: list[tuple[str, str]]) -> None:
    """Say what was installed, then how to look after it.

    The columns are computed rather than spaced by hand: three platforms print
    this and hand-alignment drifts the moment one of them gains a command.
    """
    print(f"\n{headline}")
    print("\nUseful commands:")
    width = max(len(command) for command, _ in commands)
    for command, note in commands:
        print(f"  {command:<{width}}   # {note}")


def _capture(args: list[str]) -> str | None:
    """*args*' stdout when it exits zero, or ``None`` when it did not run.

    Bounded and non-raising because the readiness card asks these questions
    at boot: a query tool that is missing, slow or angry must cost an unknown
    answer, never a stall or a traceback.
    """
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _schtasks_query(args: list[str]) -> str | None:
    """*args*' stdout, or ``None`` when schtasks did not answer.

    schtasks may hand back UTF-16 decoded as if it were not.  Its output is
    only ever matched against, never re-encoded, so the nulls are dropped here
    once rather than the encoding guessed at each call site.
    """
    out = _capture(args)
    return None if out is None else out.replace("\x00", "")


def _task_name(instance_name: str | None) -> str:
    """The Windows logon task's name, which the tray app computes identically.

    Instance-scoped, and reduced to characters ``schtasks`` cannot misread
    rather than escaped — the same reduction as ``Autostart.TaskName`` in the
    tray, because whoever asks must arrive at the name whoever registered it
    used.
    """
    if not instance_name:
        return "OpenShrimp"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in instance_name)
    return f"OpenShrimp-{safe}"


def is_service_installed(instance_name: str | None = None) -> bool:
    """Whether the bot is registered to come back without a terminal.

    The question is not whether a file was written but whether closing the
    window keeps the bot alive, so each platform is asked the version of that
    question it can answer: systemd whether the unit is *enabled*, launchd
    whether the label is actually loaded, and Windows whether the task will
    run rather than merely exist.  A plist on disk that launchd refused, a
    disabled logon task and a disabled unit all start nothing.
    """
    try:
        platform = _detect_platform()
    except RuntimeError:
        return False

    if platform == "windows":
        xml = _schtasks_query(
            ["schtasks", "/Query", "/TN", _task_name(instance_name), "/XML"]
        )
        if xml is None:
            return False
        return "<enabled>false</enabled>" not in xml.lower()

    if not _unit_file_path(platform).exists():
        return False
    if platform == "linux":
        return (
            _capture(["systemctl", "--user", "is-enabled", "open-shrimp.service"])
            is not None
        )
    return (
        _capture(["launchctl", "print", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"])
        is not None
    )


def _refuse(reason: str, remedy: str) -> NoReturn:
    """Stop the install, saying what is wrong and what to do about it.

    Both halves or neither: an operator told only that the install stopped has
    been left exactly where the install would have left them.
    """
    print(reason, file=sys.stderr)
    print(remedy, file=sys.stderr)
    sys.exit(1)


def _config_to_install_against(resolved_config: str) -> Config:
    """The config the installed service will start from.

    The unit restarts the core on failure, so a core that cannot start is a
    core that restarts forever — visible to the operator as a machine that does
    nothing and says nothing.  A config the core can load is therefore a
    precondition of installing, settled before anything is written, asked or
    registered.

    Absent and unreadable are one decision and two remedies: the wizard writes
    a config only where there is none, so sending the operator to it is the
    answer to the first and a round trip back to here for the second.
    """
    try:
        return load_config(resolved_config)
    except FileNotFoundError:
        _refuse(
            f"No config file at {resolved_config}",
            "Run 'openshrimp' first to complete the setup wizard.",
        )
    except Exception as exc:
        _refuse(
            f"Config file cannot be loaded: {resolved_config}\n  {exc}",
            "Fix it, or delete it and run 'openshrimp' to set up again.",
        )


def install_service(config_path: str) -> None:
    """Install OpenShrimp as a system service.

    On Linux, installs a systemd user unit and enables it.
    On macOS, installs a launchd user agent and loads it.
    On Windows, registers a logon task that starts the core.

    Args:
        config_path: Path to the OpenShrimp config file.
    """
    resolved_config = str(Path(config_path).expanduser().resolve())
    config = _config_to_install_against(resolved_config)

    platform = _detect_platform()
    if platform == "windows":
        _install_windows(
            config.instance_name, _detect_executable(), resolved_config
        )
        return
    svc_path = _unit_file_path(platform)

    if svc_path.exists():
        if not _may_replace(f"Service file already exists: {svc_path}"):
            return
        # Stop what the file describes before overwriting the description.
        if platform == "linux":
            _run(
                ["systemctl", "--user", "stop", "open-shrimp.service"],
                check=False,
            )
        else:
            _run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(svc_path)],
                check=False,
            )

    # Detect executable
    exec_args = _detect_executable()

    # Generate and write service file
    if platform == "linux":
        content = _generate_systemd_unit(exec_args, resolved_config)
    else:
        content = _generate_launchd_plist(exec_args, resolved_config)
        _LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)

    svc_path.parent.mkdir(parents=True, exist_ok=True)
    svc_path.write_text(content, encoding="utf-8")
    print(f"Service file written to {svc_path}")

    # Enable and start
    if platform == "linux":
        _install_systemd(svc_path)
    else:
        _install_launchd(svc_path)


def _install_windows(
    instance_name: str | None,
    exec_args: list[str],
    config_path: str,
) -> None:
    """Register the logon task that starts the core.

    A logon task and a systemd unit answer the same question on their
    platform: what starts the core when nobody is at a terminal.  The name is
    the tray's, because whoever asks whether autostart is on must arrive at
    the name whoever registered it used — so an existing task of that name is
    another owner's, and is never replaced without being asked about.
    """
    task = _task_name(instance_name)

    if is_service_installed(instance_name) and not _may_replace(
        f"A logon task already exists: {task}"
    ):
        return

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        xml_path = Path(scratch) / "task.xml"
        # schtasks reads the definition as UTF-16 with a BOM; anything else is
        # rejected with an unhelpful parse error.
        xml_path.write_text(
            _generate_windows_task_xml(exec_args, config_path),
            encoding="utf-16",
        )
        # schtasks has no atomic create-if-absent, so /F is what writing the
        # definition means; the overwrite it can perform was consented to above.
        result = _run(
            ["schtasks", "/Create", "/TN", task, "/XML", str(xml_path), "/F"],
            check=False,
        )

    if result.returncode != 0:
        print(f"Warning: could not register {task}: {result.stderr}", file=sys.stderr)
        return

    _print_banner(
        f"OpenShrimp will start when you sign in, as the task {task}.",
        [
            (f"schtasks /Query /TN {task}", "check status"),
            (f"schtasks /Run /TN {task}", "start it now"),
            ("openshrimp uninstall", "remove the task"),
        ],
    )


def _install_systemd(svc_path: Path) -> None:
    """Enable and start the systemd user service."""
    result = _run(["systemctl", "--user", "daemon-reload"])
    if result.returncode != 0:
        print(f"Warning: daemon-reload failed: {result.stderr}", file=sys.stderr)

    result = _run(["systemctl", "--user", "enable", "open-shrimp.service"])
    if result.returncode != 0:
        print(f"Warning: enable failed: {result.stderr}", file=sys.stderr)

    result = _run(["systemctl", "--user", "start", "open-shrimp.service"])
    if result.returncode != 0:
        print(f"Warning: start failed: {result.stderr}", file=sys.stderr)

    # Enable lingering so the service runs without an active login session
    result = _run(["loginctl", "enable-linger"], check=False)
    if result.returncode != 0:
        print(
            "\nNote: Could not enable login lingering. The service may stop when "
            "you log out. Run manually:\n"
            f"  loginctl enable-linger {os.environ.get('USER', '')}"
        )

    _print_banner(
        "OpenShrimp is installed and running as a systemd user service.",
        [
            ("systemctl --user status open-shrimp", "check status"),
            ("journalctl --user -u open-shrimp -f", "follow logs"),
            ("systemctl --user restart open-shrimp", "restart"),
            ("openshrimp uninstall", "remove the service"),
        ],
    )


def _install_launchd(svc_path: Path) -> None:
    """Load the launchd user agent."""
    result = _run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(svc_path)],
        check=False,
    )
    if result.returncode != 0:
        print(f"Warning: launchctl bootstrap failed: {result.stderr}", file=sys.stderr)

    _print_banner(
        "OpenShrimp is installed and running as a launchd user agent.",
        [
            (f"launchctl list | grep {_LAUNCHD_LABEL}", "check status"),
            ("tail -f ~/Library/Logs/OpenShrimp/openshrimp.stderr.log", "follow logs"),
            (f"launchctl kickstart gui/{os.getuid()}/{_LAUNCHD_LABEL}", "restart"),
            ("openshrimp uninstall", "remove the service"),
        ],
    )


def _loadable_config(resolved_config: str) -> Config | None:
    """The config at *resolved_config*, or ``None`` when the core cannot use it.

    Absent and unparseable arrive as one answer because removing a service
    asks only what the config can still tell us, never whether it is fit to
    start from — the reason it is unusable changes nothing about what has to
    be removed.
    """
    try:
        return load_config(resolved_config)
    except Exception as exc:
        logger.debug("config at %s is unusable: %r", resolved_config, exc)
        return None


def uninstall_service(config_path: str) -> None:
    """Remove the OpenShrimp system service.

    On Linux, stops, disables, and removes the systemd user unit.
    On macOS, unloads and removes the launchd user agent.
    On Windows, deletes the logon task.

    Args:
        config_path: Path to the OpenShrimp config file, read only for the
            instance name the Windows task is scoped by.
    """
    platform = _detect_platform()
    if platform == "windows":
        # Installing demands a config the core can start from; removing must
        # not.  Without one the instance name is unknowable, and guessing the
        # unscoped one removes a task nobody asked about while leaving the
        # operator's own registered and still reported as on — so every
        # OpenShrimp task is removed instead.  Uninstall is the one command
        # entitled to that: it is asked to leave nothing starting at logon.
        config = _loadable_config(str(Path(config_path).expanduser().resolve()))
        if config is not None:
            _uninstall_windows([_task_name(config.instance_name)])
        else:
            _uninstall_windows(_registered_task_names())
        return
    svc_path = _unit_file_path(platform)

    if not svc_path.exists():
        print("OpenShrimp service is not installed.")
        return

    if platform == "linux":
        _uninstall_systemd(svc_path)
    else:
        _uninstall_launchd(svc_path)


def _registered_task_names() -> list[str]:
    """Every OpenShrimp logon task registered at the root, whoever owns it.

    Only the root folder, because that is where a task of this name is one of
    ours or the tray's; a task nested in a folder shares the prefix by
    coincidence.
    """
    listing = _schtasks_query(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if listing is None:
        return []
    names: list[str] = []
    for row in csv.reader(io.StringIO(listing)):
        if not row:
            continue
        name = row[0].strip().lstrip("\\")
        if "\\" in name or name in names:
            continue
        if name == "OpenShrimp" or name.startswith("OpenShrimp-"):
            names.append(name)
    return names


def _uninstall_windows(task_names: list[str]) -> None:
    """Delete *task_names*, so nothing starts the core at the next sign-in.

    Deleted without asking whether each is there first: a disabled task still
    exists and still has to go, and ``is_service_installed`` reports one as
    absent.
    """
    if not task_names:
        print("OpenShrimp logon task is not installed.")
        return
    for task in task_names:
        result = _run(["schtasks", "/Delete", "/TN", task, "/F"], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(f"No OpenShrimp logon task {task} was removed. {detail}".rstrip())
            continue
        print(f"OpenShrimp logon task {task} has been removed.")


def _uninstall_systemd(svc_path: Path) -> None:
    """Stop, disable, and remove the systemd user service."""
    _run(["systemctl", "--user", "stop", "open-shrimp.service"], check=False)
    _run(["systemctl", "--user", "disable", "open-shrimp.service"], check=False)
    svc_path.unlink()
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    print("OpenShrimp systemd service has been removed.")


def _uninstall_launchd(svc_path: Path) -> None:
    """Unload and remove the launchd user agent."""
    _run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(svc_path)],
        check=False,
    )
    svc_path.unlink()
    print("OpenShrimp launchd agent has been removed.")
    print(f"Log files remain at {_LAUNCHD_LOG_DIR} — delete manually if desired.")
