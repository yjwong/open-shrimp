"""Docker-based sandbox for isolated Claude CLI execution.

Wraps the free functions in :mod:`open_shrimp.sandbox.docker_helpers` into a
:class:`DockerSandbox` class that implements the :class:`Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

from open_shrimp.sandbox.docker_helpers import (
    COMPUTER_USE_IMAGE,
    CONTAINER_IMAGE,
    build_cli_wrapper as _build_cli_wrapper,
    container_name as _container_name_fn,
    ensure_computer_use_image as _ensure_computer_use_image,
    ensure_container_running as _ensure_container_running,
    ensure_image as _ensure_image,
    get_screenshots_dir as _get_screenshots_dir,
    get_text_input_active as _get_text_input_active,
    get_text_input_state_path as _get_text_input_state_path,
    get_vnc_port as _get_vnc_port,
)

logger = logging.getLogger(__name__)


class DockerSandbox:
    """Docker container sandbox implementing the :class:`Sandbox` protocol.

    Each instance wraps a single context's Docker lifecycle.  The underlying
    functions in :mod:`open_shrimp.sandbox.docker_helpers` are called with the stored
    configuration, so callers only need the protocol methods.
    """

    def __init__(
        self,
        context_name: str,
        project_dir: str,
        additional_directories: list[str] | None = None,
        docker_in_docker: bool = False,
        computer_use: bool = False,
        custom_dockerfile: str | None = None,
    ) -> None:
        self._context_name = context_name
        self._project_dir = project_dir
        self._additional_directories = additional_directories
        self._docker_in_docker = docker_in_docker
        self._computer_use = computer_use
        self._custom_dockerfile = custom_dockerfile

        # Resolve image name (same logic as client_manager.py lines 273-280).
        if computer_use and custom_dockerfile:
            self._image_name = f"openshrimp-claude:{context_name}"
        elif computer_use:
            self._image_name = COMPUTER_USE_IMAGE
        elif custom_dockerfile:
            self._image_name = f"openshrimp-claude:{context_name}"
        else:
            self._image_name = CONTAINER_IMAGE

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def container_name(self) -> str | None:
        return _container_name_fn(self._context_name)

    def environment_ready(self) -> bool:
        result = subprocess.run(
            ["docker", "image", "inspect", self._image_name],
            capture_output=True,
        )
        return result.returncode == 0

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        if self._computer_use and self._custom_dockerfile:
            _ensure_computer_use_image(log_file=log_file)
            _ensure_image(
                image_name=self._image_name,
                dockerfile=self._custom_dockerfile,
                base_image=COMPUTER_USE_IMAGE,
                log_file=log_file,
            )
        elif self._computer_use:
            _ensure_computer_use_image(
                image_name=self._image_name,
                log_file=log_file,
            )
        else:
            _ensure_image(
                image_name=self._image_name,
                dockerfile=self._custom_dockerfile,
                log_file=log_file,
            )

    def running(self) -> bool:
        result = subprocess.run(
            [
                "docker", "inspect", "-f", "{{.State.Running}}",
                _container_name_fn(self._context_name),
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        _ensure_container_running(
            context_name=self._context_name,
            project_dir=self._project_dir,
            additional_directories=self._additional_directories,
            docker_in_docker=self._docker_in_docker,
            computer_use=self._computer_use,
            image_name=self._image_name,
        )

    def provision_workspace(self) -> None:
        # Docker uses bind mounts — workspace is already in place.
        pass

    def build_cli_wrapper(self) -> tuple[str, list[str]]:
        path = _build_cli_wrapper(
            context_name=self._context_name,
            project_dir=self._project_dir,
            additional_directories=self._additional_directories,
            docker_in_docker=self._docker_in_docker,
            computer_use=self._computer_use,
            image_name=self._image_name,
        )
        return path, [path]

    def stop(self) -> None:
        name = self.container_name
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            logger.info("Stopped container %s", name)

    def get_screenshots_dir(self) -> Path | None:
        if self._computer_use:
            return _get_screenshots_dir(self._context_name)
        return None

    def get_vnc_port(self) -> int | None:
        if self._computer_use:
            return _get_vnc_port(self._context_name)
        return None

    def get_text_input_state_path(self) -> Path | None:
        if self._computer_use:
            return _get_text_input_state_path(self._context_name)
        return None

    def get_text_input_active(self) -> bool:
        if self._computer_use:
            return _get_text_input_active(self._context_name)
        return False

    # -- Computer-use operations ------------------------------------------------

    def _exec_in_container_sync(
        self, cmd: list[str], timeout_secs: float = 10.0,
    ) -> tuple[int, str, str]:
        """Run a command inside the container (synchronous)."""
        uid = os.getuid()
        docker_cmd = [
            "docker", "exec",
            "-e", f"XDG_RUNTIME_DIR=/tmp/runtime-{uid}",
            "-e", "WAYLAND_DISPLAY=wayland-0",
            self.container_name,
            *cmd,
        ]
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
        return result.returncode, result.stdout, result.stderr

    def take_screenshot(self, output_path: Path) -> None:
        ts = int(output_path.stem.split("-")[-1]) if "-" in output_path.stem else 0
        container_path = f"/tmp/screenshots/screenshot-{ts}.png"
        rc, _, stderr = self._exec_in_container_sync(["grim", container_path])
        if rc != 0:
            raise RuntimeError(f"grim failed: {stderr.strip()}")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        rc, _, stderr = self._exec_in_container_sync([
            "sh", "-c",
            f"wlrctl pointer move {x} {y} && wlrctl pointer click {button}",
        ])
        if rc != 0:
            raise RuntimeError(f"click failed: {stderr.strip()}")

    def send_type(self, text: str) -> None:
        rc, _, stderr = self._exec_in_container_sync([
            "wlrctl", "keyboard", "type", text,
        ])
        if rc != 0:
            raise RuntimeError(f"type failed: {stderr.strip()}")

    def send_key(self, key_str: str) -> None:
        _named_key_chars: dict[str, str] = {
            "return": "\n", "enter": "\n",
            "tab": "\t", "escape": "\x1b",
            "backspace": "\x08", "space": " ",
        }

        parts = key_str.split("+")
        if len(parts) > 1:
            modifiers = ",".join(parts[:-1])
            key_name = parts[-1]
            char = _named_key_chars.get(key_name.lower(), key_name)
            cmd = ["wlrctl", "keyboard", "type", char, "modifiers", modifiers]
        else:
            char = _named_key_chars.get(key_str.lower())
            if char is not None:
                cmd = ["wlrctl", "keyboard", "type", char]
            else:
                cmd = ["wlrctl", "keyboard", "type", key_str]

        rc, _, stderr = self._exec_in_container_sync(cmd)
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
        rc, _, stderr = self._exec_in_container_sync([
            "sh", "-c",
            f"wlrctl pointer move {x} {y} && wlrctl pointer scroll {dx} {dy}",
        ])
        if rc != 0:
            raise RuntimeError(f"scroll failed: {stderr.strip()}")

    def focus_window(self, name: str) -> None:
        rc, _, stderr = self._exec_in_container_sync([
            "wlrctl", "toplevel", "focus", name,
        ])
        if rc != 0:
            raise RuntimeError(f"focus failed: {stderr.strip()}")

    def get_clipboard(self) -> str:
        rc, stdout, stderr = self._exec_in_container_sync(["wl-paste", "--no-newline"])
        if rc != 0:
            return ""
        return stdout

    def set_clipboard(self, text: str) -> None:
        uid = os.getuid()
        docker_cmd = [
            "docker", "exec", "-i",
            "-e", f"XDG_RUNTIME_DIR=/tmp/runtime-{uid}",
            "-e", "WAYLAND_DISPLAY=wayland-0",
            self.container_name,
            "wl-copy",
        ]
        result = subprocess.run(
            docker_cmd,
            input=text,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode != 0:
            raise RuntimeError(f"wl-copy failed: {result.stderr.strip()}")

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        if not host_paths:
            return []

        name = self.container_name
        assert name is not None

        upload_dir = "/tmp/openshrimp-uploads"

        # Ensure the destination directory exists inside the container.
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", name,
            "mkdir", "-p", upload_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to create upload dir in container %s: %s",
                name, stderr.decode().strip(),
            )
            return list(host_paths)

        result: list[Path] = []
        for host_path in host_paths:
            container_path = Path(upload_dir) / host_path.name
            proc = await asyncio.create_subprocess_exec(
                "docker", "cp", str(host_path),
                f"{name}:{container_path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "docker cp failed for %s -> %s:%s: %s",
                    host_path, name, container_path,
                    stderr.decode().strip(),
                )
                result.append(host_path)
                continue
            result.append(container_path)
            logger.info(
                "Copied attachment into container: %s -> %s:%s",
                host_path, name, container_path,
            )

        return result
