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
from typing import Any

from open_shrimp.sandbox.agent_runtime import (
    AgentHandle,
    AgentRuntime,
    ImageBundle,
    ServedEndpoint,
    WrappedCLI,
    run_served_endpoint,
    terminate_served_proc,
)
from open_shrimp.sandbox.base import PortForward, VncQuirk

import open_shrimp.sandbox.docker_helpers as _dh

from open_shrimp.sandbox.docker_helpers import (
    build_cli_wrapper as _build_cli_wrapper,
    container_name as _container_name_fn,
    ensure_container_running as _ensure_container_running,
    ensure_image as _ensure_image,
    ensure_layered_computer_use_image as _ensure_layered_computer_use_image,
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
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._context_name = context_name
        self._project_dir = project_dir
        self._additional_directories = additional_directories
        self._docker_in_docker = docker_in_docker
        self._computer_use = computer_use
        self._custom_dockerfile = custom_dockerfile

        # The bundle carries the image-build inputs and guest user/home; the
        # launch carries any served-endpoint home mounts and the published
        # guest port.  All decisions key off the bundle, not a flavour string.
        # The runtime is required for Docker: callers (the SandboxManager
        # factory) always have one in hand by the time they instantiate.
        if runtime is None or runtime.image_bundle is None:
            raise ValueError(
                "DockerSandbox requires a runtime with an ImageBundle; "
                "callers should construct a runtime before instantiating."
            )
        self._bundle: ImageBundle = runtime.image_bundle
        launch = runtime.launch if runtime else None
        if isinstance(launch, ServedEndpoint):
            self._served_home_mounts = launch.home_mounts
            self._served_guest_port: int | None = launch.guest_port
        else:
            self._served_home_mounts = ()
            self._served_guest_port = None

        # Custom Dockerfile and computer-use take precedence over the
        # bundle's base tag.
        if custom_dockerfile:
            base = _dh._base_image_for(self._bundle)
            repo = base.rsplit(":", 1)[0]
            self._image_name = f"{repo}:{context_name}"
        elif computer_use:
            image = self._bundle.computer_use_image
            if image is None:
                raise ValueError(
                    f"Runtime bundle {self._bundle.tag_suffix!r} has no "
                    f"computer-use image",
                )
            self._image_name = image
        else:
            self._image_name = _dh._base_image_for(self._bundle)

        # Served-endpoint state, read by the served-endpoint client's liveness
        # check via the endpoint's ``owner`` (``owner._served_proc``).
        self._served_proc: subprocess.Popen[str] | None = None
        self._served_endpoint: Any = None

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def host_address(self) -> str:
        return "host.docker.internal"

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
            _ensure_layered_computer_use_image(
                self._bundle, log_file=log_file,
            )
            _ensure_image(
                bundle=self._bundle,
                image_name=self._image_name,
                dockerfile=self._custom_dockerfile,
                base_image=self._bundle.computer_use_image,
                log_file=log_file,
            )
        elif self._computer_use:
            _ensure_layered_computer_use_image(
                self._bundle, log_file=log_file,
            )
        else:
            _ensure_image(
                image_name=self._image_name,
                dockerfile=self._custom_dockerfile,
                log_file=log_file,
                bundle=self._bundle,
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
            bundle=self._bundle,
            served_home_mounts=self._served_home_mounts,
            served_guest_port=self._served_guest_port,
        )

    def provision_workspace(self, *, log_file: Path | None = None) -> None:
        # Docker uses bind mounts — workspace is already in place.
        pass

    def start_agent(self, runtime: AgentRuntime) -> AgentHandle:
        if isinstance(runtime.launch, WrappedCLI):
            cli_path, cleanup_paths = self.build_cli_wrapper()
            return AgentHandle(cli_path=cli_path, cleanup_paths=cleanup_paths)
        if isinstance(runtime.launch, ServedEndpoint):
            return self._start_served_endpoint(runtime, runtime.launch)
        raise NotImplementedError(
            f"Unsupported launch strategy: {runtime.launch!r}"
        )

    def _start_served_endpoint(
        self, runtime: AgentRuntime, launch: ServedEndpoint,
    ) -> AgentHandle:
        """Run the serve argv inside the container and reach its port.

        The runtime supplies the serve argv, the home/env contributions, and the
        inject hook; this sandbox owns only the ``docker exec`` spawn and the
        published-port :meth:`reach`.  The shared launch body lives in
        :func:`run_served_endpoint`.  The served image (binary + home + extra
        home mounts + published ``launch.guest_port``) is built and run by
        ``docker_helpers``.
        """
        # Reuse a healthy server if one is already up for this container.
        if self._served_proc is not None and self._served_proc.poll() is None:
            if self._served_endpoint is not None:
                return AgentHandle(endpoint=self._served_endpoint)

        def spawn(
            serve_argv: list[str], env: dict[str, str],
        ) -> subprocess.Popen[str]:
            env_args: list[str] = []
            for key, value in env.items():
                env_args.extend(["-e", f"{key}={value}"])
            cmd = [
                "docker", "exec",
                *env_args,
                "-w", self._project_dir,
                self.container_name,
                *serve_argv,
            ]
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        # The sandbox owns reach: a ``docker port`` lookup for the published
        # guest port → ``"127.0.0.1:<host_port>"``.
        proc, endpoint = run_served_endpoint(
            runtime,
            launch,
            spawn=spawn,
            reach=self.reach,
            owner=self,
            log_label=f"Sandbox context '{self._context_name}'",
        )
        self._served_proc = proc
        self._served_endpoint = endpoint
        return AgentHandle(endpoint=endpoint)

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

    def reach(self, guest_port: int) -> str:
        # Docker publishes container ports with dynamic host mapping; query
        # the actual mapped host port (same lookup ``get_vnc_port`` uses).
        name = self.container_name
        assert name is not None
        result = subprocess.run(
            ["docker", "port", name, str(guest_port)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"No published host port for guest port {guest_port} on "
                f"container {name}: {result.stderr.strip()}"
            )
        for line in result.stdout.strip().splitlines():
            port_str = line.rsplit(":", 1)[-1]
            try:
                host_port = int(port_str)
            except ValueError:
                continue
            return f"127.0.0.1:{host_port}"
        raise RuntimeError(
            f"Could not parse published host port for guest port "
            f"{guest_port} on container {name}: {result.stdout.strip()!r}"
        )

    def stop(self) -> None:
        # Tear down any served process before removing the container.
        terminate_served_proc(self._served_proc)
        self._served_proc = None
        self._served_endpoint = None
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

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Docker computer-use runs wayvnc with no authentication.
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        return frozenset()

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
        rc, stdout, stderr = self._exec_in_container_sync(
            ["wl-paste", "--no-newline", "--primary"],
        )
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

    def start_security_key_helper(
        self,
        *,
        relay_url: str,
        session_id: str,
        token: str,
    ) -> None:
        if not self._computer_use:
            raise NotImplementedError("security-key helper requires computer use")
        name = self.container_name
        if not name:
            raise RuntimeError("Cannot start security-key helper: container unknown")
        result = subprocess.run(
            [
                "docker", "exec", "-d", "-u", "0", name,
                "openshrimp-security-key-vm-helper",
                "--relay-url", relay_url,
                "--session-id", session_id,
                "--token", token,
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"security-key helper failed to start: {result.stderr.strip()}"
            )

    # -- Port forwarding ------------------------------------------------------

    def supports_port_forwarding(self) -> bool:
        return False

    def add_port_forward(
        self,
        guest_port: int,
        requested_host_port: int | None,
        scope_key: str | None,
        description: str | None,
    ) -> PortForward:
        raise NotImplementedError(
            "Runtime port forwarding is not supported for Docker sandboxes."
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return False

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        return []

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        pass

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
