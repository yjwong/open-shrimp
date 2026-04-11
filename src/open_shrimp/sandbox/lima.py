"""Lima-based sandbox for isolated Claude CLI execution on macOS.

Uses Lima (Apple Virtualization.framework via the VZ driver) for full VM
isolation.  VirtioFS provides fast filesystem sharing between the host
and the Linux guest.

VMs are **persistent**: one long-lived VM per context, kept warm between
Claude sessions.  Cold boot is ~30 s, so VMs should stay running.  The
CLI wrapper uses ``limactl shell`` to exec commands inside the VM.

Implements the :class:`~open_shrimp.sandbox.base.Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from pathlib import Path

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.lima_helpers import (
    _lima_env,
    _log,
    _read_credentials_json,
    build_cli_wrapper as _build_cli_wrapper,
    ensure_claude_cli_in_vm,
    generate_lima_yaml,
    instance_name as _instance_name,
    lima_config_fingerprint,
    limactl_create,
    limactl_delete,
    limactl_instance_status,
    limactl_shell_check,
    limactl_start,
    limactl_stop,
    load_config_fingerprint,
    save_config_fingerprint,
    state_dir_for,
)

logger = logging.getLogger(__name__)

# Named key → character mapping for wlrctl keyboard input.
_NAMED_KEY_CHARS: dict[str, str] = {
    "return": "\n", "enter": "\n",
    "tab": "\t", "escape": "\x1b",
    "backspace": "\x08", "space": " ",
}


class LimaSandbox:
    """Lima VM sandbox implementing the Sandbox protocol.

    Uses Lima with the VZ driver (Apple Virtualization.framework) for
    macOS VM isolation.  Each instance manages one Lima VM for a single
    context.
    """

    def __init__(
        self,
        context_name: str,
        config: SandboxConfig,
        project_dir: str,
        limactl_path: str,
        additional_directories: list[str] | None = None,
        instance_prefix: str = "openshrimp",
        computer_use: bool = False,
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._limactl = limactl_path
        self._additional_directories = additional_directories or []
        self._instance_prefix = instance_prefix
        self._computer_use = computer_use

        self._sdir = state_dir_for(context_name)
        self._inst_name = _instance_name(context_name, instance_prefix)
        self._claude_home_dir = self._sdir / "claude-home"
        self._tmp_dir = self._sdir / "tmp"
        self._env = _lima_env()  # cached — LIMA_HOME doesn't change

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def container_name(self) -> str | None:
        return None

    def environment_ready(self) -> bool:
        """Check if the Lima instance exists (any status)."""
        return limactl_instance_status(self._limactl, self._inst_name) is not None

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        """Create the Lima instance from a generated YAML template.

        Idempotent — only creates if the instance doesn't exist.
        Detects config drift and rebuilds if necessary.
        """
        sdir = self._sdir
        sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Detect config drift.
        desired_fp = lima_config_fingerprint(
            self._config,
            self._project_dir,
            self._additional_directories or None,
            self._computer_use,
        )
        saved_fp = load_config_fingerprint(sdir)
        if saved_fp is not None and saved_fp != desired_fp:
            _log(
                log_file,
                "Lima config changed — rebuilding VM from scratch...",
            )
            logger.info(
                "Config fingerprint drifted for %s — triggering rebuild",
                self._inst_name,
            )
            # Delete fingerprint before rebuild.
            (sdir / "config.sha256").unlink(missing_ok=True)
            self._rebuild_vm(log_file=log_file)
            return

        # Check if instance already exists.
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status is not None:
            logger.info(
                "Lima instance %s already exists (status: %s)",
                self._inst_name, status,
            )
            save_config_fingerprint(sdir, desired_fp)
            _log(log_file, "Lima VM environment ready.")
            return

        _log(log_file, f"Setting up Lima VM for '{self._context_name}'...")

        # Ensure shared directories exist on host.
        self._claude_home_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        # Generate YAML template.
        yaml_path = generate_lima_yaml(
            sdir,
            self._config,
            self._project_dir,
            self._additional_directories or None,
            self._computer_use,
        )

        # Create the instance (this downloads the image + boots for cloud-init).
        limactl_create(
            self._limactl, self._inst_name, yaml_path, log_file=log_file,
        )

        save_config_fingerprint(sdir, desired_fp)
        _log(log_file, "Lima VM environment ready.")

    def running(self) -> bool:
        """Check if the Lima instance is running and responsive."""
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status != "Running":
            return False
        return limactl_shell_check(self._limactl, self._inst_name)

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        """Start the Lima instance if not running, wait for shell access."""
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status is None:
            raise RuntimeError(
                f"Lima instance {self._inst_name} not found — "
                f"call ensure_environment() first"
            )

        if status != "Running":
            limactl_start(
                self._limactl, self._inst_name, log_file=log_file,
            )

        # Wait for shell to be responsive.
        if not limactl_shell_check(self._limactl, self._inst_name):
            _log(log_file, "Waiting for VM to be ready...")
            logger.info("Waiting for shell on %s...", self._inst_name)
            import time

            for _ in range(120):
                if limactl_shell_check(self._limactl, self._inst_name):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"Lima instance {self._inst_name} shell not responsive "
                    f"after 120s — instance left running for debugging"
                )

        _log(log_file, "Lima VM ready.")
        logger.info("Lima instance %s is ready", self._inst_name)

    def provision_workspace(self) -> None:
        """Ensure Claude CLI is installed in the VM and credentials are copied."""
        # Install Claude CLI (Linux binary).
        ensure_claude_cli_in_vm(self._limactl, self._inst_name)

        # Copy credentials to host-side shared directory.
        creds = _read_credentials_json()
        if creds:
            dest = self._claude_home_dir / ".credentials.json"
            dest.write_text(creds, encoding="utf-8")
            logger.info("Wrote credentials to %s", dest)

    def build_cli_wrapper(self) -> tuple[str, list[str]]:
        path = _build_cli_wrapper(
            self._context_name,
            self._sdir,
            self._limactl,
            project_dir=self._project_dir,
            inst_name=self._inst_name,
            claude_home_dir=self._claude_home_dir,
        )
        return path, [path]

    def stop(self) -> None:
        """Stop the Lima instance."""
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status == "Running":
            limactl_stop(self._limactl, self._inst_name)

    def get_screenshots_dir(self) -> Path | None:
        if self._computer_use:
            return self._sdir / "screenshots"
        return None

    def get_vnc_port(self) -> int | None:
        if self._computer_use:
            return 5900
        return None

    def get_text_input_state_path(self) -> Path | None:
        if self._computer_use:
            return self._sdir / "text-input-state-dir" / "text-input-state"
        return None

    def get_text_input_active(self) -> bool:
        if not self._computer_use:
            return False
        try:
            path = self._sdir / "text-input-state-dir" / "text-input-state"
            return path.read_text(encoding="utf-8").strip() == "1"
        except (FileNotFoundError, OSError):
            return False

    # -- Computer-use operations ------------------------------------------------

    def _exec_in_vm_sync(
        self, cmd: str, *, timeout_secs: float = 10.0,
        stdin_data: str | None = None,
    ) -> tuple[int, str, str]:
        """Run a shell command inside the VM via ``limactl shell``.

        *cmd* is a shell command string (passed to ``bash -c``).
        The Wayland environment is exported automatically.
        """
        shell_cmd = f"export WAYLAND_DISPLAY=wayland-0; {cmd}"
        result = subprocess.run(
            [
                self._limactl, "shell", self._inst_name,
                "--", "bash", "-c", shell_cmd,
            ],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            env=self._env,
        )
        return result.returncode, result.stdout, result.stderr

    def take_screenshot(self, output_path: Path) -> None:
        ts = int(output_path.stem.split("-")[-1]) if "-" in output_path.stem else 0
        guest_path = f"/tmp/screenshots/screenshot-{ts}.png"
        rc, _, stderr = self._exec_in_vm_sync(f"grim {guest_path}")
        if rc != 0:
            raise RuntimeError(f"grim failed: {stderr.strip()}")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl pointer move {x} {y} && wlrctl pointer click {button}"
        )
        if rc != 0:
            raise RuntimeError(f"click failed: {stderr.strip()}")

    def send_type(self, text: str) -> None:
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl keyboard type {shlex.quote(text)}"
        )
        if rc != 0:
            raise RuntimeError(f"type failed: {stderr.strip()}")

    def send_key(self, key_str: str) -> None:
        parts = key_str.split("+")
        if len(parts) > 1:
            modifiers = ",".join(parts[:-1])
            key_name = parts[-1]
            char = _NAMED_KEY_CHARS.get(key_name.lower(), key_name)
            cmd = f"wlrctl keyboard type {shlex.quote(char)} modifiers {modifiers}"
        else:
            char = _NAMED_KEY_CHARS.get(key_str.lower(), key_str)
            cmd = f"wlrctl keyboard type {shlex.quote(char)}"

        rc, _, stderr = self._exec_in_vm_sync(cmd)
        if rc != 0:
            raise RuntimeError(f"key press failed: {stderr.strip()}")

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        scroll_map = {
            "up": (0, -amount), "down": (0, amount),
            "left": (-amount, 0), "right": (amount, 0),
        }
        dx, dy = scroll_map.get(direction, (0, amount))
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl pointer move {x} {y} && wlrctl pointer scroll {dx} {dy}"
        )
        if rc != 0:
            raise RuntimeError(f"scroll failed: {stderr.strip()}")

    def focus_window(self, name: str) -> None:
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl toplevel focus {shlex.quote(name)}"
        )
        if rc != 0:
            raise RuntimeError(f"focus failed: {stderr.strip()}")

    def get_clipboard(self) -> str:
        rc, stdout, _ = self._exec_in_vm_sync("wl-paste --no-newline --primary")
        if rc != 0:
            return ""
        return stdout

    def set_clipboard(self, text: str) -> None:
        rc, _, stderr = self._exec_in_vm_sync("wl-copy", stdin_data=text)
        if rc != 0:
            raise RuntimeError(f"wl-copy failed: {stderr.strip()}")

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files into the VM via ``limactl copy``."""
        if not host_paths:
            return []

        upload_dir = "/tmp/openshrimp-uploads"

        # Ensure upload directory exists in VM.
        proc = await asyncio.create_subprocess_exec(
            self._limactl, "shell", self._inst_name, "--",
            "mkdir", "-p", upload_dir,
            env=self._env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to create upload dir in VM %s: %s",
                self._inst_name, stderr.decode().strip(),
            )
            return list(host_paths)

        result: list[Path] = []
        for host_path in host_paths:
            vm_path = Path(upload_dir) / host_path.name
            proc = await asyncio.create_subprocess_exec(
                self._limactl, "copy",
                str(host_path),
                f"{self._inst_name}:{vm_path}",
                env=self._env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "limactl copy failed for %s -> %s:%s: %s",
                    host_path, self._inst_name, vm_path,
                    stderr.decode().strip(),
                )
                result.append(host_path)
                continue
            result.append(vm_path)
            logger.info(
                "Copied attachment into VM: %s -> %s:%s",
                host_path, self._inst_name, vm_path,
            )

        return result

    # -- Internal helpers -----------------------------------------------------

    def _rebuild_vm(self, *, log_file: Path | None = None) -> None:
        """Delete the Lima instance and recreate from scratch."""
        _log(log_file, "Deleting existing Lima instance for rebuild...")
        limactl_delete(self._limactl, self._inst_name)

        # Re-run ensure_environment to recreate.
        self.ensure_environment(log_file=log_file)
