"""Libvirt/QEMU-based sandbox for isolated Claude CLI execution.

Provides VM-level isolation via KVM/QEMU, managed through libvirt's
``qemu:///session`` (rootless) connection.  The host project directory is
shared with the VM via virtiofs (preferred) or 9p (fallback).

VMs are **persistent**: one long-lived VM per context, kept warm between
Claude sessions.  Cold boot is ~13s, so VMs should stay running.  The CLI
wrapper SSHs into the VM to run the Claude CLI.

Implements the :class:`~open_shrimp.sandbox.base.Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.libvirt_helpers import (
    _fs_tag_for_dir,
    build_cli_wrapper as _build_cli_wrapper,
    cleanup_wrapper as _cleanup_wrapper,
    create_overlay,
    domain_name as _domain_name,
    ensure_base_image,
    ensure_mounts,
    ensure_ssh_key,
    extract_fs_tags_from_xml,
    find_free_port,
    find_virtiofsd,
    generate_cloud_init_iso,
    generate_domain_xml,
    load_ssh_port,
    save_ssh_port,
    ssh_check_alive,
    start_virtiofsd,
    state_dir_for,
    wait_for_ssh,
    _log,
)

logger = logging.getLogger(__name__)

# Graceful shutdown timeout before falling back to destroy.
_SHUTDOWN_TIMEOUT = 10


class LibvirtSandbox:
    """KVM/QEMU virtual machine sandbox implementing the Sandbox protocol.

    Each instance manages one VM's lifecycle for a single context.
    Uses ``libvirt-python`` for domain management (not ``virsh`` CLI).
    """

    def __init__(
        self,
        context_name: str,
        config: SandboxConfig,
        project_dir: str,
        conn: "libvirt.virConnect",  # type: ignore[name-defined]
        additional_directories: list[str] | None = None,
        instance_prefix: str = "openshrimp",
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._additional_directories = additional_directories or []
        self._conn = conn
        self._instance_prefix = instance_prefix
        self._wrapper_path: str | None = None
        self._virtiofsd_procs: list[subprocess.Popen[bytes]] = []
        self._use_virtiofs: bool = find_virtiofsd() is not None

        self._sdir = state_dir_for(context_name)
        self._dom_name = _domain_name(context_name, instance_prefix)
        self._ssh_port: int | None = load_ssh_port(self._sdir)

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def container_name(self) -> str | None:
        # Not a Docker container — return None.
        return None

    def environment_ready(self) -> bool:
        """Check if the VM environment (overlay, cloud-init, SSH key) exists."""
        sdir = self._sdir
        return (
            (sdir / "overlay.qcow2").exists()
            and (sdir / "cloud-init.iso").exists()
            and (sdir / "ssh_key").exists()
        )

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        """Build the VM environment: base image, overlay, cloud-init, SSH key.

        Idempotent — only does real work on first call.
        """
        import libvirt

        sdir = self._sdir
        sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

        _log(log_file, f"Setting up VM environment for '{self._context_name}'...")

        # 1. Base image.
        _log(log_file, "Ensuring base image...")
        base_image = ensure_base_image(
            self._config.base_image, log_file=log_file,
        )

        # 2. SSH key.
        private_key, public_key_path = ensure_ssh_key(sdir)
        public_key = public_key_path.read_text().strip()

        # 3. qcow2 overlay.
        overlay = create_overlay(sdir, base_image, self._config.disk_size)

        # 4. Cloud-init ISO (user + SSH only; mounts handled via SSH).
        cloud_init_iso = generate_cloud_init_iso(
            sdir, public_key,
            provision_script=self._config.provision,
        )

        # 5. Allocate SSH port (persistent across restarts).
        if self._ssh_port is None:
            self._ssh_port = find_free_port()
            save_ssh_port(sdir, self._ssh_port)

        # 6. Generate and define the domain XML.
        serial_log = sdir / "serial.log"

        # Build shared_dirs list for domain XML: (host_dir, socket | None).
        # The domain must declare virtiofs/9p devices for all dirs even
        # though the guest-side mount is managed via SSH later.
        all_dirs = [self._project_dir] + self._additional_directories
        shared_dirs_xml: list[tuple[str, Path | None]] = []
        for host_dir in all_dirs:
            if self._use_virtiofs:
                sock = self._virtiofs_socket_for(host_dir)
                shared_dirs_xml.append((host_dir, sock))
            else:
                shared_dirs_xml.append((host_dir, None))

        xml = generate_domain_xml(
            self._dom_name,
            overlay_path=overlay,
            cloud_init_iso=cloud_init_iso,
            serial_log=serial_log,
            ssh_port=self._ssh_port,
            memory_mb=self._config.memory,
            vcpus=self._config.cpus,
            shared_dirs=shared_dirs_xml,
            use_virtiofs=self._use_virtiofs,
        )

        # Define domain (idempotent — overwrites if exists).
        # If the domain is active but the desired filesystem devices have
        # changed (e.g. additional_directories added/removed), we must
        # gracefully stop the VM, re-define, and let ensure_running()
        # restart it.
        desired_tags = {_fs_tag_for_dir(d) for d in all_dirs}
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                domain.undefine()
                self._conn.defineXML(xml)
                logger.info("Re-defined domain %s", self._dom_name)
            else:
                # Check if the filesystem devices have drifted.
                current_tags = extract_fs_tags_from_xml(domain.XMLDesc(0))
                if current_tags != desired_tags:
                    _log(
                        log_file,
                        "Shared directories changed — restarting VM "
                        "to apply new config...",
                    )
                    logger.info(
                        "Filesystem tags drifted for %s: "
                        "current=%s desired=%s — stopping for re-define",
                        self._dom_name, current_tags, desired_tags,
                    )
                    self._stop_virtiofsd()
                    self.stop()
                    # After stop, domain is inactive — undefine and re-define.
                    try:
                        domain = self._conn.lookupByName(self._dom_name)
                        domain.undefine()
                    except libvirt.libvirtError:
                        pass
                    self._conn.defineXML(xml)
                    logger.info(
                        "Re-defined domain %s with updated filesystems",
                        self._dom_name,
                    )
                else:
                    logger.info(
                        "Domain %s already active, config unchanged",
                        self._dom_name,
                    )
        except libvirt.libvirtError as e:
            if e.get_error_code() == 42:  # VIR_ERR_NO_DOMAIN
                self._conn.defineXML(xml)
                logger.info("Defined new domain %s", self._dom_name)
            else:
                raise

        _log(log_file, "VM environment ready.")

    def ensure_running(self) -> None:
        """Start the VM if not already running, wait for SSH."""
        import libvirt

        assert self._ssh_port is not None, (
            "SSH port not set — call ensure_environment() first"
        )

        # Start virtiofsd instances if needed (must be running before the VM).
        # One virtiofsd per shared directory.
        if self._use_virtiofs and not self._virtiofsd_procs:
            self._start_all_virtiofsd()

        # Start domain if not active.
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                domain.create()
                logger.info("Started domain %s", self._dom_name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == 42:  # VIR_ERR_NO_DOMAIN
                raise RuntimeError(
                    f"Domain {self._dom_name} not defined — "
                    f"call ensure_environment() first"
                ) from e
            if e.get_error_code() == 55:  # VIR_ERR_OPERATION_INVALID (already running)
                pass
            else:
                raise

        # Wait for SSH connectivity.
        ssh_key = self._sdir / "ssh_key"
        if not ssh_check_alive(self._ssh_port, ssh_key):
            logger.info("Waiting for SSH on port %d...", self._ssh_port)
            if not wait_for_ssh(self._ssh_port, ssh_key, timeout=60):
                # SSH unreachable — likely corrupt host keys from a hard
                # kill.  Destroy the VM, delete the overlay to force a
                # fresh cloud-init, and retry once.
                logger.warning(
                    "SSH unreachable for %s — rebuilding VM "
                    "(likely corrupt SSH host keys from hard kill)",
                    self._dom_name,
                )
                self._rebuild_vm()
                if not wait_for_ssh(self._ssh_port, ssh_key, timeout=90):
                    raise RuntimeError(
                        f"VM {self._dom_name} SSH not reachable after "
                        f"rebuild on port {self._ssh_port}"
                    )
        logger.info("VM %s SSH ready on port %d", self._dom_name, self._ssh_port)

        # Configure filesystem mounts via SSH (idempotent).
        # This handles config changes (added/removed additional_directories)
        # without requiring a VM rebuild.
        all_dirs = [self._project_dir] + self._additional_directories
        fs_type = "virtiofs" if self._use_virtiofs else "9p"
        ensure_mounts(
            ssh_port=self._ssh_port,
            ssh_key=self._sdir / "ssh_key",
            shared_dirs=all_dirs,
            fs_type=fs_type,
        )

    def provision_workspace(self) -> None:
        """Provision the workspace: ensure Claude CLI is installed in the VM."""
        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"

        from open_shrimp.sandbox.docker_helpers import find_claude_binary
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        cli_binary = find_claude_binary()
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)

        # Check if claude is already available in the VM.
        result = subprocess.run(
            ["ssh", *ssh_opts, "claude@localhost", "which", "claude"],
            capture_output=True,
        )
        if result.returncode == 0:
            return

        # SCP the Claude CLI binary into the VM.
        # Note: scp uses -P (uppercase) for port, not -p like ssh.
        scp_opts = [
            "-i", str(ssh_key),
            "-P", str(self._ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]
        logger.info("Installing Claude CLI into VM %s...", self._dom_name)
        subprocess.run(
            [
                "scp", *scp_opts,
                str(cli_binary),
                "claude@localhost:/tmp/claude",
            ],
            check=True,
            capture_output=True,
        )
        # Move to /usr/local/bin (needs sudo).
        subprocess.run(
            [
                "ssh", *ssh_opts,
                "claude@localhost",
                "--",
                "sudo mv /tmp/claude /usr/local/bin/claude && sudo chmod +x /usr/local/bin/claude",
            ],
            check=True,
            capture_output=True,
        )
        logger.info("Claude CLI installed in VM %s", self._dom_name)

    def build_cli_wrapper(self) -> str:
        assert self._ssh_port is not None
        self._wrapper_path = _build_cli_wrapper(
            self._context_name,
            self._sdir,
            self._ssh_port,
            project_dir=self._project_dir,
            instance_prefix=self._instance_prefix,
        )
        return self._wrapper_path

    def cleanup(self) -> None:
        if self._wrapper_path:
            _cleanup_wrapper(self._wrapper_path)
            self._wrapper_path = None

    def stop(self) -> None:
        """Gracefully shutdown the VM (ACPI), with destroy fallback."""
        import libvirt

        try:
            domain = self._conn.lookupByName(self._dom_name)
        except libvirt.libvirtError:
            return

        if not domain.isActive():
            return

        # Graceful ACPI shutdown.
        try:
            domain.shutdown()
            logger.info("Sent ACPI shutdown to %s", self._dom_name)
        except libvirt.libvirtError:
            logger.warning(
                "ACPI shutdown failed for %s, falling back to destroy",
                self._dom_name,
            )
            domain.destroy()
            self._stop_virtiofsd()
            return

        # Wait for shutdown to complete.
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if not domain.isActive():
                    logger.info("Domain %s shut down gracefully", self._dom_name)
                    self._stop_virtiofsd()
                    return
            except libvirt.libvirtError:
                self._stop_virtiofsd()
                return
            time.sleep(0.5)

        # Timeout — force destroy.
        logger.warning(
            "Domain %s did not shut down in %ds, destroying",
            self._dom_name, _SHUTDOWN_TIMEOUT,
        )
        try:
            domain.destroy()
        except libvirt.libvirtError:
            pass
        self._stop_virtiofsd()

    def get_screenshots_dir(self) -> Path | None:
        # No computer-use in Phase 3.
        return None

    def get_vnc_port(self) -> int | None:
        # VNC support added in Phase 4.
        return None

    def get_text_input_state_path(self) -> Path | None:
        return None

    def get_text_input_active(self) -> bool:
        return False

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files into the VM via scp."""
        if not host_paths:
            return []

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"

        upload_dir = "/tmp/openshrimp-uploads"

        # Ensure upload directory exists in VM.
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-i", str(ssh_key),
            "-p", str(self._ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "claude@localhost",
            "mkdir", "-p", upload_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to create upload dir in VM %s: %s",
                self._dom_name, stderr.decode().strip(),
            )
            return list(host_paths)

        result: list[Path] = []
        for host_path in host_paths:
            vm_path = Path(upload_dir) / host_path.name
            proc = await asyncio.create_subprocess_exec(
                "scp",
                "-i", str(ssh_key),
                "-P", str(self._ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                str(host_path),
                f"claude@localhost:{vm_path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "scp failed for %s -> %s:%s: %s",
                    host_path, self._dom_name, vm_path,
                    stderr.decode().strip(),
                )
                result.append(host_path)
                continue
            result.append(vm_path)
            logger.info(
                "Copied attachment into VM: %s -> %s:%s",
                host_path, self._dom_name, vm_path,
            )

        return result

    # -- Internal helpers -----------------------------------------------------

    def _virtiofs_socket_for(self, host_dir: str) -> Path:
        """Return the virtiofsd socket path for a host directory."""
        tag = _fs_tag_for_dir(host_dir)
        return self._sdir / f"{tag}.sock"

    def _start_all_virtiofsd(self) -> None:
        """Start virtiofsd instances for all shared directories."""
        all_dirs = [self._project_dir] + self._additional_directories
        for host_dir in all_dirs:
            sock = self._virtiofs_socket_for(host_dir)
            proc = start_virtiofsd(sock, host_dir)
            self._virtiofsd_procs.append(proc)
        # Wait for all sockets to appear.
        import time as _time
        all_socks = [self._virtiofs_socket_for(d) for d in all_dirs]
        for _ in range(20):
            if all(s.exists() for s in all_socks):
                break
            _time.sleep(0.1)

    def _stop_virtiofsd(self) -> None:
        """Stop all virtiofsd processes."""
        for proc in self._virtiofsd_procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                proc.kill()
            logger.info("Stopped virtiofsd (pid=%d)", proc.pid)
        self._virtiofsd_procs.clear()

    def _is_domain_active(self) -> bool:
        """Check if the domain is currently active."""
        import libvirt
        try:
            domain = self._conn.lookupByName(self._dom_name)
            return domain.isActive()
        except libvirt.libvirtError:
            return False

    def _rebuild_vm(self) -> None:
        """Destroy the VM, delete the overlay, and recreate from scratch.

        Used when SSH is unreachable after boot — typically due to corrupt
        SSH host keys from a hard kill (SIGKILL / virsh destroy).
        """
        import libvirt

        # 1. Stop virtiofsd.
        self._stop_virtiofsd()

        # 2. Destroy + undefine the domain.
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if domain.isActive():
                domain.destroy()
            domain.undefine()
            logger.info("Undefined domain %s for rebuild", self._dom_name)
        except libvirt.libvirtError:
            pass

        # 3. Delete the overlay (forces fresh cloud-init on next boot).
        overlay = self._sdir / "overlay.qcow2"
        overlay.unlink(missing_ok=True)
        # Also delete cloud-init ISO so it gets regenerated.
        (self._sdir / "cloud-init.iso").unlink(missing_ok=True)
        logger.info("Deleted overlay and cloud-init for rebuild")

        # 4. Re-run ensure_environment + ensure_running (without SSH wait).
        self.ensure_environment()
        # Re-start virtiofsd instances.
        if self._use_virtiofs:
            self._start_all_virtiofsd()

        # Start the domain.
        try:
            domain = self._conn.lookupByName(self._dom_name)
            domain.create()
            logger.info("Started rebuilt domain %s", self._dom_name)
        except libvirt.libvirtError as e:
            raise RuntimeError(
                f"Failed to start rebuilt domain {self._dom_name}: {e}"
            ) from e
