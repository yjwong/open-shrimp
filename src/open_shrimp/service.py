"""Install/uninstall OpenShrimp as a system service (systemd or launchd)."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from open_shrimp.config import DEFAULT_CONFIG_PATH

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


def _detect_platform() -> str:
    """Return 'linux' or 'macos'.

    Raises:
        RuntimeError: On unsupported platforms.
    """
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    raise RuntimeError(
        f"Unsupported platform: {sys.platform}. "
        "Only Linux (systemd) and macOS (launchd) are supported."
    )


def _detect_executable() -> list[str]:
    """Find the best executable path for the service.

    Returns a list of arguments for the ``openshrimp`` command.  Typically a
    single-element list with the absolute path, but falls back to
    ``[sys.executable, "-m", "open_shrimp"]`` if the script is not found.
    """
    # 1. Check if openshrimp is on PATH
    which = shutil.which("openshrimp")
    if which:
        return [str(Path(which).resolve())]

    # 2. Check for the script next to the running Python interpreter
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "openshrimp"
    if candidate.is_file():
        return [str(candidate.resolve())]

    # 3. Fallback: run as a module
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


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output."""
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _service_path(platform: str) -> Path:
    """Return the service file path for the given platform."""
    if platform == "linux":
        return _SYSTEMD_UNIT_PATH
    return _LAUNCHD_PLIST_PATH


def install_service(config_path: str) -> None:
    """Install OpenShrimp as a system service.

    On Linux, installs a systemd user unit and enables it.
    On macOS, installs a launchd user agent and loads it.

    Args:
        config_path: Path to the OpenShrimp config file.
    """
    platform = _detect_platform()
    svc_path = _service_path(platform)

    # Check for existing installation
    if svc_path.exists():
        if sys.stdin.isatty():
            print(f"Service file already exists: {svc_path}")
            try:
                answer = input("Overwrite? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nInstall cancelled.")
                return
            if answer not in ("y", "yes"):
                print("Install cancelled.")
                return
            # Stop existing service before overwriting
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
        else:
            print(f"Service file already exists: {svc_path}", file=sys.stderr)
            print("Run interactively to overwrite.", file=sys.stderr)
            sys.exit(1)

    # Resolve config path
    resolved_config = str(Path(config_path).expanduser().resolve())
    if not Path(resolved_config).exists():
        print(f"Warning: config file does not exist yet: {resolved_config}")
        print("Run 'openshrimp' first to complete the setup wizard.\n")

    # Detect executable
    exec_args = _detect_executable()

    # Generate and write service file
    if platform == "linux":
        content = _generate_systemd_unit(exec_args, resolved_config)
    else:
        content = _generate_launchd_plist(exec_args, resolved_config)
        _LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)

    svc_path.parent.mkdir(parents=True, exist_ok=True)
    svc_path.write_text(content)
    print(f"Service file written to {svc_path}")

    # Enable and start
    if platform == "linux":
        _install_systemd(svc_path)
    else:
        _install_launchd(svc_path)


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

    print("\nOpenShrimp is installed and running as a systemd user service.")
    print("\nUseful commands:")
    print("  systemctl --user status open-shrimp   # check status")
    print("  journalctl --user -u open-shrimp -f   # follow logs")
    print("  systemctl --user restart open-shrimp   # restart")
    print("  openshrimp uninstall                   # remove the service")


def _install_launchd(svc_path: Path) -> None:
    """Load the launchd user agent."""
    result = _run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(svc_path)],
        check=False,
    )
    if result.returncode != 0:
        print(f"Warning: launchctl bootstrap failed: {result.stderr}", file=sys.stderr)

    print("\nOpenShrimp is installed and running as a launchd user agent.")
    print("\nUseful commands:")
    print(f"  launchctl list | grep {_LAUNCHD_LABEL}           # check status")
    print(f"  tail -f ~/Library/Logs/OpenShrimp/openshrimp.stderr.log  # follow logs")
    print(f"  launchctl kickstart gui/{os.getuid()}/{_LAUNCHD_LABEL}  # restart")
    print("  openshrimp uninstall                                     # remove the service")


def uninstall_service() -> None:
    """Remove the OpenShrimp system service.

    On Linux, stops, disables, and removes the systemd user unit.
    On macOS, unloads and removes the launchd user agent.
    """
    platform = _detect_platform()
    svc_path = _service_path(platform)

    if not svc_path.exists():
        print("OpenShrimp service is not installed.")
        return

    if platform == "linux":
        _uninstall_systemd(svc_path)
    else:
        _uninstall_launchd(svc_path)


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
