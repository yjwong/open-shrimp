"""Sandbox manager: global lifecycle, factory, and build logging.

The :class:`SandboxManager` protocol abstracts global sandbox concerns
(reaper lifecycle, instance naming, container cleanup, build logging)
away from the per-instance :class:`~open_shrimp.sandbox.base.Sandbox`
protocol.  Callers interact with a single manager instance threaded
through ``bot_data``; individual sandboxes are obtained via
:meth:`SandboxManager.create_sandbox`.

Use :func:`create_sandbox_managers` to instantiate the correct backends
for the current platform and configuration.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from platformdirs import user_data_path

from open_shrimp.config import Config, ContextConfig
from open_shrimp.sandbox.base import Sandbox

logger = logging.getLogger(__name__)

# Graceful shutdown timeout before falling back to destroy.
_SHUTDOWN_TIMEOUT = 180


# ---------------------------------------------------------------------------
# Global build registry
# ---------------------------------------------------------------------------
# Authoritative source of truth for active builds.  Each
# ``register_build`` / ``unregister_build`` call updates this registry so
# that ``resolve_container_build`` can look up the owning manager without
# iterating managers and guessing based on shared file paths.

_build_registry: dict[str, tuple[Path, SandboxManager]] = {}
_build_registry_lock = threading.Lock()


def register_active_build(
    context_name: str, log_path: Path, manager: SandboxManager,
) -> None:
    """Record an active build in the global registry."""
    with _build_registry_lock:
        _build_registry[context_name] = (log_path, manager)


def unregister_active_build(context_name: str) -> None:
    """Remove a build from the global registry."""
    with _build_registry_lock:
        _build_registry.pop(context_name, None)


def lookup_active_build(
    context_name: str,
) -> tuple[Path, SandboxManager] | None:
    """Look up an active build by context name.

    Returns ``(log_path, manager)`` if the build is registered, else
    ``None``.
    """
    with _build_registry_lock:
        return _build_registry.get(context_name)


@runtime_checkable
class SandboxManager(Protocol):
    """Manages global sandbox lifecycle and acts as a factory for sandboxes."""

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        """Configure instance-specific naming for multi-instance deployments."""
        ...

    @property
    def instance_prefix(self) -> str:
        """The current instance prefix (e.g. ``"openshrimp"`` or
        ``"openshrimp-mybot"``)."""
        ...

    @property
    def container_label(self) -> str:
        """Docker label used to tag managed containers."""
        ...

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Start crash-safety reaper (Ryuk for Docker, no-op for others)."""
        ...

    def stop_reaper(self) -> None:
        """Stop the crash-safety reaper."""
        ...

    def stop_all(self) -> None:
        """Stop and remove all managed sandbox runtimes."""
        ...

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        """Evict the cached sandbox for *context_name*.

        Stops the runtime (container/VM) and removes it from the cache so
        the next ``create_sandbox`` call builds a fresh instance with
        updated configuration (e.g. new additional directories).
        """
        ...

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        """Return a cached or new per-context :class:`Sandbox` instance.

        The same instance is returned for the same *context_name* across
        multiple calls.  The sandbox's lifecycle (VM/container) is
        independent of individual sessions.
        """
        ...

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        """Register an active build, return the log file path."""
        ...

    def unregister_build(self, context_name: str) -> None:
        """Mark a build as no longer active."""
        ...

    def is_build_active(self, context_name: str) -> bool:
        """Check whether a build is currently active."""
        ...

    @property
    def build_log_dir(self) -> Path:
        """Directory containing build log files."""
        ...

    @property
    def state_dir(self) -> Path:
        """Base directory for per-context sandbox state."""
        ...

    def claude_home_dir(self, context_name: str) -> Path:
        """Host-side directory mapped to ``~/.claude`` inside the sandbox.

        Used to locate session ``.jsonl`` files without creating a full
        :class:`Sandbox` instance (e.g. for ``/resume`` session listing).
        """
        ...


# ---------------------------------------------------------------------------
# Docker implementation
# ---------------------------------------------------------------------------


class DockerSandboxManager:
    """Docker-backed :class:`SandboxManager` implementation.

    Lifts the module-level globals from :mod:`open_shrimp.sandbox.docker_helpers` into
    instance attributes so the manager can be injected and tested.
    """

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"
        self._ryuk_socket: socket.socket | None = None
        self._ryuk_container_id: str | None = None
        self._sandbox_cache: dict[str, Sandbox] = {}

        # Build logging state.
        self._active_builds: dict[str, Path] = {}
        self._active_builds_lock = threading.Lock()

        self._build_log_dir = Path(tempfile.gettempdir()) / "openshrimp-builds"
        self._state_dir = user_data_path("openshrimp") / "containers"

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"
        # Keep the legacy module globals in sync so that free functions in
        # container.py (called by DockerSandbox) see the right prefix.
        import open_shrimp.sandbox.docker_helpers as _c
        _c._INSTANCE_PREFIX = self._instance_prefix  # noqa: SLF001
        _c._CONTAINER_LABEL = self._container_label  # noqa: SLF001

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Start Testcontainers Ryuk and register a label filter.

        Ryuk watches a TCP connection as a liveness signal.  When the
        connection drops (bot crash/exit), Ryuk reaps labelled containers.
        """
        from open_shrimp.sandbox.docker_helpers import RYUK_IMAGE, check_docker_available

        if not check_docker_available():
            return

        prefix = self._instance_prefix
        label = self._container_label

        try:
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name", f"{prefix}-ryuk",
                    "-v", "/var/run/docker.sock:/var/run/docker.sock",
                    "-p", "127.0.0.1::8080",
                    "--label", f"{label}.ryuk=true",
                    RYUK_IMAGE,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                if "Conflict" in result.stderr or "already in use" in result.stderr:
                    subprocess.run(
                        ["docker", "rm", "-f", f"{prefix}-ryuk"],
                        capture_output=True,
                    )
                    result = subprocess.run(
                        [
                            "docker", "run", "-d",
                            "--name", f"{prefix}-ryuk",
                            "-v", "/var/run/docker.sock:/var/run/docker.sock",
                            "-p", "127.0.0.1::8080",
                            "--label", f"{label}.ryuk=true",
                            RYUK_IMAGE,
                        ],
                        capture_output=True,
                        text=True,
                    )
                if result.returncode != 0:
                    logger.warning(
                        "Failed to start Ryuk container: %s",
                        result.stderr.strip(),
                    )
                    return

            self._ryuk_container_id = result.stdout.strip()
            logger.info(
                "Started Ryuk container: %s", self._ryuk_container_id[:12],
            )

            # Discover the mapped host port.
            port_result = subprocess.run(
                ["docker", "port", f"{prefix}-ryuk", "8080"],
                capture_output=True,
                text=True,
            )
            if port_result.returncode != 0:
                logger.warning(
                    "Failed to get Ryuk port: %s",
                    port_result.stderr.strip(),
                )
                self._cleanup_ryuk_container()
                return

            port_str = port_result.stdout.strip().rsplit(":", 1)[-1]
            port = int(port_str)

            # Connect and register our label filter.
            import time as _time

            filter_msg = f"label={label}=true\n".encode()
            sock: socket.socket | None = None
            for _attempt in range(10):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(5)
                    sock.connect(("127.0.0.1", port))
                    sock.sendall(filter_msg)
                    ack = sock.recv(1024).decode().strip()
                    if ack == "ACK":
                        break
                    logger.warning("Unexpected Ryuk response: %s", ack)
                    sock.close()
                    sock = None
                except (ConnectionResetError, ConnectionRefusedError, OSError):
                    if sock is not None:
                        sock.close()
                        sock = None
                    _time.sleep(0.2)
            else:
                logger.warning("Could not connect to Ryuk after retries")
                self._cleanup_ryuk_container()
                return

            sock.settimeout(None)
            self._ryuk_socket = sock
            logger.info(
                "Ryuk connected on port %d, label filter registered", port,
            )

        except Exception:
            logger.warning(
                "Failed to start Ryuk (continuing without crash cleanup)",
                exc_info=True,
            )
            self._cleanup_ryuk_container()

    def stop_reaper(self) -> None:
        """Close the Ryuk connection and remove the Ryuk container."""
        if self._ryuk_socket is not None:
            try:
                self._ryuk_socket.close()
            except OSError:
                pass
            self._ryuk_socket = None
        self._cleanup_ryuk_container()

    def _cleanup_ryuk_container(self) -> None:
        if self._ryuk_container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", f"{self._instance_prefix}-ryuk"],
                capture_output=True,
            )
            logger.info("Removed Ryuk container")
            self._ryuk_container_id = None

    def stop_all(self) -> None:
        """Stop and remove all OpenShrimp-managed containers."""
        self._sandbox_cache.clear()
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label={self._container_label}=true",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
        )
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if name:
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True,
                )
                logger.info("Removed container %s", name)

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            try:
                cached.stop()
            except Exception:
                logger.debug("Error stopping sandbox %s", context_name, exc_info=True)
            logger.info("Invalidated Docker sandbox for context '%s'", context_name)

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        assert context.container is not None
        from open_shrimp.sandbox.docker import DockerSandbox

        sandbox = DockerSandbox(
            context_name=context_name,
            project_dir=context.directory,
            additional_directories=context.additional_directories or None,
            docker_in_docker=context.container.docker_in_docker,
            computer_use=context.container.computer_use,
            custom_dockerfile=context.container.dockerfile,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        with self._active_builds_lock:
            self._active_builds[context_name] = log_path
        register_active_build(context_name, log_path, self)
        logger.info(
            "Registered build log for context '%s': %s",
            context_name, log_path,
        )
        return log_path

    def unregister_build(self, context_name: str) -> None:
        with self._active_builds_lock:
            self._active_builds.pop(context_name, None)
        unregister_active_build(context_name)
        logger.info("Unregistered build for context '%s'", context_name)

        log_path = self._build_log_dir / f"{context_name}.log"

        def _cleanup() -> None:
            try:
                log_path.unlink(missing_ok=True)
                logger.debug("Cleaned up build log %s", log_path)
            except Exception:
                logger.debug("Failed to clean up build log %s", log_path)

        timer = threading.Timer(3600, _cleanup)
        timer.daemon = True
        timer.start()

    def is_build_active(self, context_name: str) -> bool:
        with self._active_builds_lock:
            return context_name in self._active_builds

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def claude_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name


# ---------------------------------------------------------------------------
# macOS (no-op) implementation
# ---------------------------------------------------------------------------


class MacOSSandboxManager:
    """No-op :class:`SandboxManager` for macOS (sandbox-exec, no Docker)."""

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"
        self._sandbox_cache: dict[str, Sandbox] = {}
        self._build_log_dir = Path(tempfile.gettempdir()) / "openshrimp-builds"
        self._state_dir = user_data_path("openshrimp") / "containers"

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    def start_reaper(self) -> None:
        pass

    def stop_reaper(self) -> None:
        pass

    def stop_all(self) -> None:
        self._sandbox_cache.clear()

    def invalidate_sandbox(self, context_name: str) -> None:
        # macOS sandbox-exec has no persistent runtime; just evict cache.
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            logger.info("Invalidated macOS sandbox for context '%s'", context_name)

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        from open_shrimp.sandbox.macos import MacOSSandbox

        sandbox = MacOSSandbox(
            context_name=context_name,
            project_dir=context.directory,
            additional_directories=context.additional_directories or None,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        register_active_build(context_name, log_path, self)
        return log_path

    def unregister_build(self, context_name: str) -> None:
        unregister_active_build(context_name)

    def is_build_active(self, context_name: str) -> bool:
        return lookup_active_build(context_name) is not None

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def claude_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name


# ---------------------------------------------------------------------------
# Lima implementation
# ---------------------------------------------------------------------------


class LimaSandboxManager:
    """Lima-backed :class:`SandboxManager` for macOS VM isolation.

    Uses Lima (Apple Virtualization.framework via the VZ driver) for full
    VM isolation.  The ``limactl`` binary is auto-downloaded on first use.
    """

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"  # unused, but protocol requires it
        self._limactl_path: str | None = None
        self._sandbox_cache: dict[str, Sandbox] = {}

        self._active_builds: dict[str, Path] = {}
        self._active_builds_lock = threading.Lock()

        self._build_log_dir = Path(tempfile.gettempdir()) / "openshrimp-builds"
        self._state_dir = user_data_path("openshrimp") / "lima"

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Ensure limactl binary is available (auto-download if needed)."""
        from open_shrimp.sandbox.lima_helpers import ensure_limactl_sync

        self._limactl_path = ensure_limactl_sync()

    def stop_reaper(self) -> None:
        pass

    def stop_all(self) -> None:
        """Stop all OpenShrimp-managed Lima instances."""
        if self._limactl_path is None:
            self._sandbox_cache.clear()
            return

        from open_shrimp.sandbox.lima_helpers import (
            limactl_list_json,
            limactl_stop,
            _lima_env,
        )

        prefix = self._instance_prefix + "-"
        for inst in limactl_list_json(self._limactl_path):
            name = inst.get("name", "")
            if not name.startswith(prefix):
                continue
            if inst.get("status") == "Running":
                limactl_stop(self._limactl_path, name)
                logger.info("Stopped Lima instance %s", name)

        self._sandbox_cache.clear()

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            try:
                cached.stop()
            except Exception:
                logger.debug("Error stopping Lima sandbox %s", context_name, exc_info=True)
            logger.info("Invalidated Lima sandbox for context '%s'", context_name)

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        if self._limactl_path is None:
            raise RuntimeError(
                "Lima not available — either start_reaper() was not called "
                "or limactl could not be downloaded. Install with: "
                "brew install lima"
            )
        assert context.sandbox is not None

        from open_shrimp.sandbox.lima import LimaSandbox

        sandbox = LimaSandbox(
            context_name=context_name,
            config=context.sandbox,
            project_dir=context.directory,
            limactl_path=self._limactl_path,
            additional_directories=context.additional_directories or None,
            instance_prefix=self._instance_prefix,
            computer_use=context.sandbox.computer_use,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        with self._active_builds_lock:
            self._active_builds[context_name] = log_path
        register_active_build(context_name, log_path, self)
        logger.info(
            "Registered build log for context '%s': %s",
            context_name, log_path,
        )
        return log_path

    def unregister_build(self, context_name: str) -> None:
        with self._active_builds_lock:
            self._active_builds.pop(context_name, None)
        unregister_active_build(context_name)
        logger.info("Unregistered build for context '%s'", context_name)

        log_path = self._build_log_dir / f"{context_name}.log"

        def _cleanup() -> None:
            try:
                log_path.unlink(missing_ok=True)
            except Exception:
                pass

        timer = threading.Timer(3600, _cleanup)
        timer.daemon = True
        timer.start()

    def is_build_active(self, context_name: str) -> bool:
        with self._active_builds_lock:
            return context_name in self._active_builds

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def claude_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name / "claude-home"


# ---------------------------------------------------------------------------
# Libvirt implementation
# ---------------------------------------------------------------------------


class LibvirtSandboxManager:
    """Libvirt/QEMU-backed :class:`SandboxManager` implementation.

    Manages VM lifecycle via ``qemu:///session`` (rootless libvirt).
    One persistent ``libvirt.virConnect`` connection for the process lifetime.
    """

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"  # not used, but protocol requires it
        self._conn: "libvirt.virConnect | None" = None  # type: ignore[name-defined]
        self._sandbox_cache: dict[str, Sandbox] = {}

        self._active_builds: dict[str, Path] = {}
        self._active_builds_lock = threading.Lock()

        self._build_log_dir = Path(tempfile.gettempdir()) / "openshrimp-builds"
        self._state_dir = user_data_path("openshrimp") / "vms"

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Open a persistent connection to ``qemu:///session``.

        Also ensures a suitable virtiofsd binary is available,
        downloading one from GitHub releases if the system version
        is missing or too old.

        No Ryuk equivalent needed — libvirt session domains don't survive
        user logout, and we track domain names for cleanup.
        """
        # Ensure virtiofsd is available (auto-download if needed).
        from open_shrimp.sandbox.libvirt_helpers import ensure_virtiofsd

        try:
            ensure_virtiofsd()
        except Exception:
            logger.warning(
                "virtiofsd not available — VMs will fall back to 9p "
                "filesystem sharing (slower)",
                exc_info=True,
            )

        try:
            import libvirt
        except ImportError:
            logger.error(
                "libvirt-python not installed — install with: "
                "pip install libvirt-python  (and: sudo apt install "
                "libvirt-daemon qemu-system-x86)"
            )
            raise

        try:
            import libvirtaio
            import asyncio

            # Register libvirt events on the asyncio event loop if one is
            # running.  This is non-blocking; individual API calls are fast
            # local socket RPCs (~sub-10ms) so true async/await isn't needed.
            try:
                loop = asyncio.get_running_loop()
                libvirtaio.virEventRegisterAsyncIOImpl(loop)
            except RuntimeError:
                # No running event loop — skip async registration.
                pass
        except ImportError:
            pass

        self._conn = libvirt.open("qemu:///session")
        if self._conn is None:
            raise RuntimeError("Failed to connect to qemu:///session")
        logger.info("Connected to qemu:///session")

    def stop_reaper(self) -> None:
        """Close the libvirt connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            logger.info("Closed qemu:///session connection")

    def stop_all(self) -> None:
        """Gracefully shutdown all openshrimp-* domains (with destroy fallback)."""
        if self._conn is None:
            return

        import libvirt
        import time

        prefix = self._instance_prefix + "-"
        try:
            domains = self._conn.listAllDomains()
        except libvirt.libvirtError:
            return

        # Send ACPI shutdown to all active domains first.
        pending: list[tuple[object, str]] = []
        for domain in domains:
            name = domain.name()
            if not name.startswith(prefix):
                continue

            if not domain.isActive():
                continue

            try:
                domain.shutdown()
                logger.info("Sent ACPI shutdown to %s", name)
                pending.append((domain, name))
            except libvirt.libvirtError:
                try:
                    domain.destroy()
                except libvirt.libvirtError:
                    pass
                logger.info("Force-destroyed %s", name)

        # Wait for all domains to shut down in parallel.
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
        while pending and time.monotonic() < deadline:
            still_alive: list[tuple[object, str]] = []
            for domain, name in pending:
                try:
                    if domain.isActive():
                        still_alive.append((domain, name))
                    else:
                        logger.info("Domain %s shut down", name)
                except libvirt.libvirtError:
                    logger.info("Domain %s shut down", name)
            pending = still_alive
            if pending:
                time.sleep(0.5)

        # Force-destroy any remaining domains.
        for domain, name in pending:
            try:
                domain.destroy()
                logger.warning("Force-destroyed %s after timeout", name)
            except libvirt.libvirtError:
                pass

        self._sandbox_cache.clear()

        # Kill any orphaned virtiofsd processes whose sockets live under
        # our state directory.
        self._stop_all_virtiofsd()

    def _stop_all_virtiofsd(self) -> None:
        """Kill virtiofsd processes with socket paths under our state dir.

        Walks ``/proc`` directly instead of shelling out to ``pgrep``.
        """
        import os
        import signal

        state_prefix = str(self._state_dir)
        proc = Path("/proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            # /proc/<pid>/cmdline uses \0 as separator.
            parts = cmdline.decode(errors="replace").split("\0")
            if not parts or not parts[0].endswith("virtiofsd"):
                continue
            if not any(state_prefix in arg for arg in parts):
                continue
            try:
                pid = int(entry.name)
                os.kill(pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to orphaned virtiofsd (pid=%d)", pid)
            except (ProcessLookupError, PermissionError):
                pass

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            try:
                cached.stop()
            except Exception:
                logger.debug("Error stopping libvirt sandbox %s", context_name, exc_info=True)
            logger.info("Invalidated libvirt sandbox for context '%s'", context_name)

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        if self._conn is None:
            raise RuntimeError(
                "Libvirt connection not available — either start_reaper() was "
                "not called or libvirt-python is not installed. Install with: "
                "pip install libvirt-python"
            )
        assert context.sandbox is not None

        from open_shrimp.sandbox.libvirt import LibvirtSandbox

        sandbox = LibvirtSandbox(
            context_name=context_name,
            config=context.sandbox,
            project_dir=context.directory,
            conn=self._conn,
            additional_directories=context.additional_directories or None,
            instance_prefix=self._instance_prefix,
            computer_use=context.sandbox.computer_use,
            virgl=context.sandbox.virgl,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        with self._active_builds_lock:
            self._active_builds[context_name] = log_path
        register_active_build(context_name, log_path, self)
        logger.info(
            "Registered build log for context '%s': %s",
            context_name, log_path,
        )
        return log_path

    def unregister_build(self, context_name: str) -> None:
        with self._active_builds_lock:
            self._active_builds.pop(context_name, None)
        unregister_active_build(context_name)
        logger.info("Unregistered build for context '%s'", context_name)

        log_path = self._build_log_dir / f"{context_name}.log"

        def _cleanup() -> None:
            try:
                log_path.unlink(missing_ok=True)
            except Exception:
                pass

        timer = threading.Timer(3600, _cleanup)
        timer.daemon = True
        timer.start()

    def is_build_active(self, context_name: str) -> bool:
        with self._active_builds_lock:
            return context_name in self._active_builds

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def claude_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name / "claude-home"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_sandbox_managers(config: Config) -> dict[str, SandboxManager]:
    """Instantiate one :class:`SandboxManager` per backend used in the config.

    On macOS, always returns a single ``"macos"`` manager.
    On Linux, returns one manager per backend (``"docker"``, ``"libvirt"``)
    that is actually referenced by at least one context.

    Returns:
        A dict mapping backend name to its :class:`SandboxManager` instance.
    """
    if sys.platform == "darwin":
        managers: dict[str, SandboxManager] = {}
        uses_lima = any(
            ctx.sandbox is not None
            and ctx.sandbox.enabled
            and ctx.sandbox.backend == "lima"
            for ctx in config.contexts.values()
        )
        if uses_lima:
            managers["lima"] = LimaSandboxManager()
        managers["macos"] = MacOSSandboxManager()
        return managers

    # Collect all backends used by sandboxed contexts.
    backends: set[str] = set()
    for ctx in config.contexts.values():
        if ctx.sandbox is not None and ctx.sandbox.enabled:
            backends.add(ctx.sandbox.backend)
        elif ctx.container is not None and ctx.container.enabled:
            backends.add("docker")

    managers: dict[str, SandboxManager] = {}
    if "docker" in backends or not backends:
        managers["docker"] = DockerSandboxManager()
    if "libvirt" in backends:
        managers["libvirt"] = LibvirtSandboxManager()
    return managers
