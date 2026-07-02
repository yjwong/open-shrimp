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
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from open_shrimp.sandbox.agent_runtime import (
    AgentHandle,
    AgentRuntime,
    GuestMount,
    ServedEndpoint,
    WrappedCLI,
    run_served_endpoint,
    terminate_served_proc,
)
from open_shrimp.sandbox.base import PortForward, VncQuirk
from open_shrimp.sandbox.port_forward import (
    SSH_TUNNEL_OPTS,
    PortForwardRegistry,
    allocate_host_port,
    open_ssh_tunnel,
)
from open_shrimp.sandbox.skill_paths import (
    SANDBOX_HOME,
    SANDBOX_UID,
    SANDBOX_USER,
)

from open_shrimp.config import SandboxConfig
from open_shrimp.security_key.vm_helper_binary import (
    BINARY_NAME as SECURITY_KEY_HELPER_BINARY,
    ensure_security_key_vm_helper,
)
from open_shrimp.sandbox.libvirt_helpers import (
    _fs_tag_for_dir,
    build_cli_wrapper as _build_cli_wrapper,
    create_overlay,
    _persistent_dev_name,
    create_persistent_volume,
    domain_name as _domain_name,
    ensure_base_image,
    ensure_mounts,
    ensure_persistent_mounts,
    ensure_ssh_key,
    extract_fs_tags_from_xml,
    extract_persistent_disks_from_xml,
    extract_vnc_port_from_xml,
    find_free_port,
    find_virtiofsd,
    generate_cloud_init_iso,
    generate_domain_xml,
    cloud_init_fingerprint,
    load_cloud_init_fingerprint,
    load_ssh_port,
    qmp_send_key_combo,
    qmp_screendump,
    qmp_send_mouse_event,
    qmp_send_scroll_event,
    qmp_type_text,
    save_cloud_init_fingerprint,
    save_ssh_port,
    ssh_check_alive,
    start_virtiofsd,
    state_dir_for,
    wait_for_cloud_init,
    wait_for_ssh,
    _log,
)

logger = logging.getLogger(__name__)

# Graceful shutdown timeout before falling back to destroy.
_SHUTDOWN_TIMEOUT = 180

# How long to wait for Android (Waydroid) to report a completed boot, and how
# often to poll ``getprop sys.boot_completed`` while waiting.
_ANDROID_BOOT_TIMEOUT_S = 120
_ANDROID_BOOT_POLL_S = 3

# Standard Android shell env vars that `adb shell` gets from init but
# `lxc-attach` (waydroid shell) does not. Legacy tools like `uiautomator`
# read these during class init and crash the VM if they're unset. Exported
# before every phone_shell command.
_ANDROID_SHELL_ENV = (
    "export EXTERNAL_STORAGE=/sdcard ANDROID_STORAGE=/storage "
    "ANDROID_DATA=/data ANDROID_ROOT=/system ANDROID_ASSETS=/system/app; "
)

# Piped to ``python3 -`` in the guest to force software rendering: merge the
# swiftshader/gralloc props into waydroid.cfg's [properties] section without
# disturbing whatever ``waydroid init`` already wrote there.
_WAYDROID_SOFTWARE_GPU_SCRIPT = (
    "import configparser, io\n"
    "p = '/var/lib/waydroid/waydroid.cfg'\n"
    "c = configparser.ConfigParser()\n"
    "c.optionxform = str\n"
    "c.read(p)\n"
    "if not c.has_section('properties'): c.add_section('properties')\n"
    "c['properties']['ro.hardware.gralloc'] = 'default'\n"
    "c['properties']['ro.hardware.egl'] = 'swiftshader'\n"
    "buf = io.StringIO(); c.write(buf)\n"
    "open(p, 'w').write(buf.getvalue())\n"
)


def _tail_file(
    source: Path, dest: Path, stop: threading.Event,
) -> None:
    """Tail *source* and append new content to *dest* until *stop* is set.

    Runs in a background thread during VM boot so serial console output
    streams into the build log for the terminal mini app.  Uses
    ``watchfiles`` (inotify on Linux) to wake immediately on writes,
    like ``tail -f``.
    """
    from watchfiles import watch

    # Wait for the source file to appear (QEMU creates it on domain start).
    while not stop.is_set():
        try:
            src_fh = open(source, "r", encoding="utf-8", errors="replace")
            break
        except FileNotFoundError:
            stop.wait(0.5)
    else:
        return

    try:
        with src_fh, open(dest, "a", encoding="utf-8") as dst_fh:
            # Flush any content already in the file.
            chunk = src_fh.read(8192)
            if chunk:
                dst_fh.write(chunk)
                dst_fh.flush()

            for _changes in watch(
                source, stop_event=stop, rust_timeout=500,
            ):
                while True:
                    chunk = src_fh.read(8192)
                    if not chunk:
                        break
                    dst_fh.write(chunk)
                    dst_fh.flush()
    except Exception:
        pass


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
        computer_use: bool = False,
        virgl: bool = False,
        phone_use: bool = False,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._additional_directories = additional_directories or []
        self._conn = conn
        self._instance_prefix = instance_prefix
        # Phone-use rides on the computer-use desktop; treat it as also
        # requiring the labwc + VNC plumbing.
        self._phone_use = phone_use
        # Set once Android has reported a completed boot, so repeated phone_*
        # calls take a single cheap probe instead of the full self-healing path.
        self._phone_booted = False
        self._computer_use = computer_use or phone_use
        self._virgl = virgl
        self._virtiofsd_procs: list[subprocess.Popen[bytes]] = []
        self._use_virtiofs: bool = find_virtiofsd() is not None

        # Served-endpoint launch's extra home mounts are synced into the
        # guest (the runtime's data home, plugin-config dir, …) so the
        # injected provider ``auth.json`` and the managed plugin config reach
        # the served process.  The wrapped-CLI launch contributes none.  The
        # mount SOURCE must match the inject TARGET (the runtime's host_dir)
        # or the guest never sees the synced files.
        self._runtime = runtime
        launch = runtime.launch if runtime else None
        if isinstance(launch, ServedEndpoint):
            self._served_home_mounts: tuple[GuestMount, ...] = launch.home_mounts
        else:
            self._served_home_mounts = ()

        self._sdir = state_dir_for(context_name)
        self._dom_name = _domain_name(context_name, instance_prefix)
        self._ssh_port: int | None = load_ssh_port(self._sdir)

        # Screenshots directory for computer-use (host-side).
        self._screenshots_dir = (
            self._sdir / "screenshots" if self._computer_use else None
        )
        if self._screenshots_dir:
            self._screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Host-side directories shared into the VM to mirror Docker's
        # bind-mount approach: task output files and .claude session data
        # are written to the host so the terminal mini app can read them.
        self._tmp_dir = self._sdir / "tmp"
        self._claude_home_dir = self._sdir / "claude-home"

        self._port_forwards = PortForwardRegistry()

        # Served-endpoint state.  ``_served_proc`` is read by the
        # served-endpoint client's liveness check via the endpoint's ``owner``.
        self._served_proc: subprocess.Popen[str] | None = None
        self._served_endpoint: Any = None

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def host_address(self) -> str:
        return "10.0.2.2"

    @property
    def container_name(self) -> str | None:
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

        # Detect cloud-init config drift.  Cloud-init only runs on first
        # boot, so if any input that affects the user-data has changed
        # (computer_use, provision script, …) the overlay must be rebuilt.
        desired_fp = cloud_init_fingerprint(
            self._config, self._computer_use, self._phone_use,
        )
        saved_fp = load_cloud_init_fingerprint(sdir)
        if saved_fp is not None and saved_fp != desired_fp:
            _log(
                log_file,
                "Cloud-init config changed — rebuilding VM from scratch...",
            )
            logger.info(
                "Cloud-init fingerprint drifted for %s — triggering rebuild",
                self._dom_name,
            )
            # Delete fingerprint before rebuild to prevent infinite
            # recursion (_rebuild_vm calls ensure_environment again).
            (sdir / "cloud-init.sha256").unlink(missing_ok=True)
            self._rebuild_vm(log_file=log_file)
            return

        _log(log_file, f"Setting up VM environment for '{self._context_name}'...")

        # 1. Base image.
        _log(log_file, "Ensuring base image...")
        base_image = ensure_base_image(
            self._config.base_image, log_file=log_file,
        )

        # 2. SSH key.
        private_key, public_key_path = ensure_ssh_key(sdir)
        public_key = public_key_path.read_text(encoding="utf-8").strip()

        # 3. qcow2 overlay.
        overlay = create_overlay(sdir, base_image, self._config.disk_size)

        # 3a. Persistent volume qcow2 files (survive rebuilds).
        persistent_volumes: list[tuple[str, Path]] = []
        for ppath in self._config.persistent_paths:
            pv_qcow2 = create_persistent_volume(sdir, ppath)
            persistent_volumes.append((ppath, pv_qcow2))

        # 4. Cloud-init ISO (user + SSH only; mounts handled via SSH).
        cloud_init_iso = generate_cloud_init_iso(
            sdir, public_key,
            provision_script=self._config.provision,
            computer_use=self._computer_use,
            phone_use=self._phone_use,
            persistent_paths=self._config.persistent_paths or None,
        )

        # 5. Allocate SSH port (persistent across restarts).
        if self._ssh_port is None:
            self._ssh_port = find_free_port()
            save_ssh_port(sdir, self._ssh_port)

        # 6. Generate and define the domain XML.
        serial_log = sdir / "serial.log"

        # Ensure host-side shared directories exist.
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._claude_home_dir.mkdir(parents=True, exist_ok=True)
        for mount in self._served_home_mounts:
            mount.host_dir.mkdir(parents=True, exist_ok=True)

        # Build shared_dirs list for domain XML: (host_dir, socket | None).
        # The domain must declare virtiofs/9p devices for all dirs even
        # though the guest-side mount is managed via SSH later.
        all_dirs, _, _ = self._shared_dirs_and_overrides()
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
            computer_use=self._computer_use,
            virgl=self._virgl,
            persistent_volumes=persistent_volumes,
        )

        # Define domain (idempotent — overwrites if exists).
        # If the domain is active but the desired filesystem devices or
        # persistent disks have changed, we must gracefully stop the VM,
        # re-define, and let ensure_running() restart it.
        desired_tags = {_fs_tag_for_dir(d) for d in all_dirs}
        desired_pvs = {
            _persistent_dev_name(i)
            for i in range(len(persistent_volumes))
        }
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                domain.undefine()
                self._conn.defineXML(xml)
                logger.info("Re-defined domain %s", self._dom_name)
            else:
                # Check if filesystem devices or persistent disks drifted.
                live_xml = domain.XMLDesc(0)
                current_tags = extract_fs_tags_from_xml(live_xml)
                current_pvs = extract_persistent_disks_from_xml(live_xml)
                config_drifted = (
                    current_tags != desired_tags
                    or current_pvs != desired_pvs
                )
                if config_drifted:
                    _log(
                        log_file,
                        "VM config changed — restarting VM "
                        "to apply new config...",
                    )
                    logger.info(
                        "Config drifted for %s: "
                        "fs_tags current=%s desired=%s, "
                        "pvs current=%s desired=%s — stopping for re-define",
                        self._dom_name,
                        current_tags, desired_tags,
                        current_pvs, desired_pvs,
                    )
                    self.stop()
                    # After stop, domain is inactive — undefine and re-define.
                    try:
                        domain = self._conn.lookupByName(self._dom_name)
                        domain.undefine()
                    except libvirt.libvirtError:
                        pass
                    self._conn.defineXML(xml)
                    logger.info(
                        "Re-defined domain %s with updated config",
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

        save_cloud_init_fingerprint(sdir, desired_fp)
        _log(log_file, "VM environment ready.")

    def running(self) -> bool:
        """Check if the VM is active and SSH-reachable."""
        import libvirt

        if self._ssh_port is None:
            return False
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                return False
        except libvirt.libvirtError:
            return False
        return ssh_check_alive(self._ssh_port, self._sdir / "ssh_key")

    def ensure_running(
        self, *, log_file: Path | None = None, _rebuild_attempted: bool = False,
    ) -> None:
        """Start the VM if not already running, wait for SSH."""
        import libvirt

        assert self._ssh_port is not None, (
            "SSH port not set — call ensure_environment() first"
        )

        # Ensure virtiofsd daemons are available for domain start.
        #
        # virtiofsd removes its socket once a client (QEMU) connects, so
        # socket existence is only meaningful *before* the VM is running.
        # We use process liveness (_virtiofsd_procs / poll()) when the
        # current OpenShrimp process started them, and domain-active
        # status when we inherited a running VM from a previous process.
        if self._use_virtiofs:
            if self._virtiofsd_procs:
                # We started these — check if they're still alive.
                if any(p.poll() is not None for p in self._virtiofsd_procs):
                    self._reap_dead_virtiofsd()
                    _log(log_file, "Starting virtiofs daemons...")
                    self._start_all_virtiofsd()
            elif not self._is_domain_active():
                # Fresh process and VM is not running — need fresh daemons
                # before domain.create().  If the VM *is* active, old
                # virtiofsd (from a previous OpenShrimp process) is already
                # connected and serving the VM.
                _log(log_file, "Starting virtiofs daemons...")
                self._start_all_virtiofsd()

        # Start domain if not active.
        cold_start = False
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                # Truncate serial.log before boot so the log only shows
                # this boot's output.
                serial_log = self._sdir / "serial.log"
                serial_log.write_bytes(b"")

                _log(log_file, "Starting VM...")
                domain.create()
                cold_start = True
                logger.info("Started domain %s", self._dom_name)
                # Modern virtiofsd (Rust) daemonizes on startup: the
                # Popen child we spawned exits once QEMU has attached to
                # the FUSE socket.  Reap those handles now so they don't
                # linger as zombies for the lifetime of the service — we
                # have observed 15+ piling up between restarts.
                self._reap_dead_virtiofsd()
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

        # Wait for SSH connectivity.  While waiting, tail the serial log
        # so the user can see boot progress in the terminal mini app.
        ssh_key = self._sdir / "ssh_key"
        if not ssh_check_alive(self._ssh_port, ssh_key):
            _log(log_file, "Waiting for SSH...")
            logger.info("Waiting for SSH on port %d...", self._ssh_port)

            # Start tailing serial.log to the build log in a background
            # thread so boot output streams to the terminal mini app.
            stop_tail = threading.Event()
            tail_thread: threading.Thread | None = None
            if log_file is not None and cold_start:
                serial_log = self._sdir / "serial.log"
                tail_thread = threading.Thread(
                    target=_tail_file,
                    args=(serial_log, log_file, stop_tail),
                    daemon=True,
                )
                tail_thread.start()

            try:
                if not wait_for_ssh(self._ssh_port, ssh_key, timeout=60):
                    raise RuntimeError(
                        f"VM {self._dom_name} SSH not reachable "
                        f"on port {self._ssh_port} — VM left running "
                        f"for debugging (virsh console, serial.log)"
                    )

                # Wait for cloud-init to finish on cold starts so that all
                # provisioned services (Chrome, compositor, etc.) are
                # running before we declare the VM ready.
                if cold_start:
                    _log(log_file, "Waiting for cloud-init to finish...")
                    logger.info(
                        "Waiting for cloud-init on %s (port %d)...",
                        self._dom_name, self._ssh_port,
                    )
                    if not wait_for_cloud_init(
                        self._ssh_port, self._sdir / "ssh_key",
                    ):
                        if _rebuild_attempted:
                            raise RuntimeError(
                                f"cloud-init failed on {self._dom_name} after "
                                f"rebuild — VM may require manual intervention"
                            )
                        _log(
                            log_file,
                            "cloud-init failed — rebuilding VM from scratch...",
                        )
                        logger.warning(
                            "cloud-init did not complete cleanly on %s "
                            "— triggering rebuild",
                            self._dom_name,
                        )
                        self._rebuild_vm(log_file=log_file)
                        self.ensure_running(
                            log_file=log_file, _rebuild_attempted=True,
                        )
                        return
            finally:
                stop_tail.set()
                if tail_thread is not None:
                    tail_thread.join(timeout=2)

        _log(log_file, "VM ready.")
        logger.info("VM %s SSH ready on port %d", self._dom_name, self._ssh_port)

        # Configure filesystem mounts via SSH (idempotent).
        # This handles config changes (added/removed additional_directories)
        # without requiring a VM rebuild.
        all_dirs, mount_overrides, readonly_dirs = self._shared_dirs_and_overrides()
        fs_type = "virtiofs" if self._use_virtiofs else "9p"
        ensure_mounts(
            ssh_port=self._ssh_port,
            ssh_key=self._sdir / "ssh_key",
            shared_dirs=all_dirs,
            fs_type=fs_type,
            mount_overrides=mount_overrides,
            readonly_dirs=readonly_dirs,
        )

        # Mount persistent volumes (format ext4 if needed, create systemd
        # mount units).  These are block devices, not virtiofs.
        if self._config.persistent_paths:
            ensure_persistent_mounts(
                ssh_port=self._ssh_port,
                ssh_key=self._sdir / "ssh_key",
                persistent_paths=self._config.persistent_paths,
            )

    def provision_workspace(self, *, log_file: Path | None = None) -> None:
        """Install computer-use helpers, runtime CLI binary, and credentials."""
        assert self._ssh_port is not None
        if self._computer_use:
            try:
                self._install_security_key_helper()
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "Security-key helper install failed for libvirt context %s; "
                    "continuing without security-key forwarding: %s",
                    self._context_name,
                    exc,
                )

        if self._phone_use:
            self._ensure_waydroid_initialized(log_file=log_file)

        if self._runtime is None:
            return

        # Cloud-init creates a single ``SANDBOX_USER`` (openshrimp) user in the
        # guest with NOPASSWD sudo (see ``_build_cloud_init_user_data``); both
        # the Claude and OpenCode installers SSH in as that user.
        bundle = self._runtime.image_bundle
        if bundle is not None and bundle.libvirt_install is not None:
            bundle.libvirt_install(
                self._sdir / "ssh_key", self._ssh_port, SANDBOX_USER,
            )

        if self._runtime.provision_credentials is not None:
            self._runtime.provision_credentials(self._claude_home_dir)

    def _ensure_waydroid_initialized(
        self, *, log_file: Path | None = None,
    ) -> None:
        """Download Android images (once) and start the Waydroid session.

        The ~2.4 GB ``waydroid init`` download is the heavy, one-time cost.
        It lands on the ``/var/lib/waydroid`` persistent volume, so it
        survives VM rebuilds and only runs when the system image is absent.
        Output is streamed to the build log so the terminal Mini App can
        tail progress.
        """
        from open_shrimp.sandbox.libvirt_helpers import _log, _ssh_common_opts

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        target = f"{SANDBOX_USER}@localhost"

        already = subprocess.run(
            [
                "ssh", *ssh_opts, target,
                "test", "-f", "/var/lib/waydroid/images/system.img",
            ],
            capture_output=True,
            timeout=30,
        )
        if already.returncode == 0:
            logger.info(
                "Waydroid already initialized for context %s", self._context_name,
            )
            self._apply_android_gpu_config()
            self._ensure_waydroid_session_running(ssh_opts, target)
            return

        # phone_use contexts always carry an AndroidConfig (see config parsing).
        image_type = self._config.android.image_type if self._config.android else "VANILLA"

        _log(
            log_file,
            "Downloading Android system images (waydroid init) — this can "
            "take several minutes on first boot...",
        )
        proc = subprocess.Popen(
            [
                "ssh", *ssh_opts, target,
                "sudo", "waydroid", "init", "-s", image_type,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            logger.info("[waydroid init %s] %s", self._context_name, line)
            _log(log_file, line)
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(
                f"waydroid init failed (rc={rc}) for context "
                f"{self._context_name}"
            )
        _log(log_file, "Android images ready.")
        self._apply_android_gpu_config()
        self._start_waydroid_session(ssh_opts, target)

    def _apply_android_gpu_config(self) -> None:
        """Seed the software-render props when ``android.gpu`` is ``software``.

        The virgl default needs no props (Android drives host-GPU GLES via
        virglrenderer).  The ``software`` opt-out forces swiftshader/gbm-default
        for GPU-less hosts; the props live in ``waydroid.cfg`` (which only
        exists post-``init``) and take effect after ``waydroid upgrade -o``.
        """
        gpu = self._config.android.gpu if self._config.android else "virgl"
        if gpu != "software":
            return
        # configparser edit over SSH keeps the [properties] section intact
        # regardless of what `waydroid init` already wrote there.
        write = self._ssh_run(
            "sudo python3 -", input=_WAYDROID_SOFTWARE_GPU_SCRIPT, timeout=60,
        )
        if write.returncode != 0:
            logger.warning(
                "Failed to write software-GPU props for context %s: %s",
                self._context_name, (write.stderr or write.stdout).strip(),
            )
            return
        self._ssh_run("sudo waydroid upgrade -o", timeout=120)

    def _ensure_waydroid_session_running(
        self, ssh_opts: list[str], target: str,
    ) -> None:
        """Start the Waydroid session only if it is not already up.

        Runs on every session start, so it must be cheap and non-disruptive:
        a live Android session is left untouched — a blind restart would tear
        down the running UI mid-use.
        """
        active = subprocess.run(
            [
                "ssh", *ssh_opts, target,
                "systemctl", "is-active", "--quiet", "waydroid-session.service",
            ],
            capture_output=True,
            timeout=30,
        )
        if active.returncode == 0:
            return
        self._start_waydroid_session(ssh_opts, target)

    def _start_waydroid_session(
        self, ssh_opts: list[str], target: str,
    ) -> None:
        """(Re)start the Waydroid container + session services over SSH.

        The units carry ``Restart=on-failure``, so a plain restart is enough
        to bring the Android UI up now that the images exist; ordering
        between container and session is handled by the unit dependencies.
        """
        for units in (
            ["waydroid-container.service"],
            ["waydroid-session.service", "waydroid-show-full-ui.service"],
        ):
            result = subprocess.run(
                ["ssh", *ssh_opts, target, "sudo", "systemctl", "restart", *units],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning(
                    "Failed to restart %s for context %s: %s",
                    units, self._context_name,
                    (result.stderr or result.stdout).strip(),
                )

    def _install_security_key_helper(self) -> None:
        from open_shrimp.security_key.guest_setup import setup_security_key_guest_cmd
        from open_shrimp.sandbox.libvirt_helpers import (
            _ssh_common_opts,
            install_cli_via_ssh,
        )

        assert self._ssh_port is not None
        ssh_opts = _ssh_common_opts(self._sdir / "ssh_key", self._ssh_port)
        result = subprocess.run(
            [
                "ssh", *ssh_opts, f"{SANDBOX_USER}@localhost",
                "uname", "-m",
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Failed to detect VM architecture for {SECURITY_KEY_HELPER_BINARY}: "
                f"{error}"
            )

        setup_result = subprocess.run(
            [
                "ssh", *ssh_opts, f"{SANDBOX_USER}@localhost",
                "bash", "-c", setup_security_key_guest_cmd(),
            ],
            capture_output=True,
            text=True,
            timeout=300.0,
        )
        if setup_result.returncode != 0:
            error = (setup_result.stderr or setup_result.stdout).strip()
            raise RuntimeError(
                f"Failed to provision UHID support for {SECURITY_KEY_HELPER_BINARY}: "
                f"{error}"
            )

        helper_path = ensure_security_key_vm_helper(result.stdout.strip())
        install_cli_via_ssh(
            SECURITY_KEY_HELPER_BINARY,
            helper_path,
            ssh_key=self._sdir / "ssh_key",
            ssh_port=self._ssh_port,
            ssh_user=SANDBOX_USER,
        )

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
        """Run the serve argv over SSH in the VM and reach its port.

        The runtime supplies the serve argv + env + inject hook; this sandbox
        owns only the remote SSH spawn and hands the tunnel to :meth:`reach` (an
        ``ssh -L`` forward via :meth:`add_port_forward`).  The shared launch body
        lives in :func:`run_served_endpoint`.

        Guest-image precondition: this launch does **not** provision the VM
        image.  The ``opencode`` binary must already be on the guest ``PATH``
        for the ``{SANDBOX_USER}@localhost`` user (base image / ``provision``
        script — operator's responsibility, documented in CLAUDE.md → Backends).
        The per-context ``opencode-home`` (→
        ``{SANDBOX_HOME}/.local/share/opencode``) and ``openshrimp-data`` (→
        ``{SANDBOX_HOME}/.local/share/openshrimp``) host dirs are
        virtiofs/9p-mounted into the guest (see
        :meth:`_shared_dirs_and_overrides`), and ``runtime.inject`` syncs the
        provider ``auth.json`` + managed plugin config into them, so they reach
        the served process (which runs with ``HOME={SANDBOX_HOME}``).  When the
        binary is absent, the serve process exits early and readiness wait raises.
        """
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        if self._served_proc is not None and self._served_proc.poll() is None:
            if self._served_endpoint is not None:
                return AgentHandle(endpoint=self._served_endpoint)

        if self._ssh_port is None:
            raise RuntimeError("Cannot start served endpoint: libvirt VM is not running")

        ssh_port = self._ssh_port

        def spawn(
            serve_argv: list[str], env: dict[str, str],
        ) -> subprocess.Popen[str]:
            env_prefix = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in env.items()
            )
            remote_cmd = (
                f"cd {shlex.quote(self._project_dir)} && "
                f"{env_prefix} {shlex.join(serve_argv)}"
            )
            ssh_opts = _ssh_common_opts(self._sdir / "ssh_key", ssh_port)
            return subprocess.Popen(
                ["ssh", *ssh_opts, f"{SANDBOX_USER}@localhost", remote_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        proc, endpoint = run_served_endpoint(
            runtime,
            launch,
            spawn=spawn,
            reach=self.reach,
            owner=self,
            log_label=f"Libvirt context '{self._context_name}'",
        )
        self._served_proc = proc
        self._served_endpoint = endpoint
        return AgentHandle(endpoint=endpoint)

    def build_cli_wrapper(self) -> tuple[str, list[str]]:
        assert self._ssh_port is not None
        path = _build_cli_wrapper(
            self._context_name,
            self._sdir,
            self._ssh_port,
            project_dir=self._project_dir,
            instance_prefix=self._instance_prefix,
            claude_home_dir=self._claude_home_dir,
        )
        return path, [path]

    def reach(self, guest_port: int) -> str:
        forward = self.add_port_forward(
            guest_port=guest_port,
            requested_host_port=None,
            scope_key=None,
            description=f"reach({guest_port})",
        )
        return f"127.0.0.1:{forward.host_port}"

    def start_security_key_helper(
        self,
        *,
        relay_url: str,
        session_id: str,
        token: str,
    ) -> None:
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        if self._ssh_port is None:
            raise RuntimeError("Cannot start security-key helper: VM is not running")
        log_path = f"/tmp/openshrimp-security-key-helper-{session_id}.log"
        helper_cmd = shlex.join([
            "openshrimp-security-key-vm-helper",
            "--relay-url", relay_url,
            "--session-id", session_id,
            "--token", token,
        ])
        remote_cmd = (
            "command -v openshrimp-security-key-vm-helper >/dev/null && "
            "sudo -n true && "
            f"(nohup sudo -n {helper_cmd} > {shlex.quote(log_path)} 2>&1 "
            "< /dev/null &)"
        )
        ssh_opts = _ssh_common_opts(self._sdir / "ssh_key", self._ssh_port)
        result = subprocess.run(
            ["ssh", *ssh_opts, f"{SANDBOX_USER}@localhost", remote_cmd],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            if not error:
                error = (
                    "openshrimp-security-key-vm-helper is not installed in the VM "
                    "or passwordless sudo is unavailable"
                )
            raise RuntimeError(f"security-key helper failed to start: {error}")

    def stop(self) -> None:
        """Gracefully shutdown the VM (ACPI), with destroy fallback."""
        import libvirt

        # Tear down any served process (the ssh -L tunnel is reaped below with
        # the rest of the port forwards).
        terminate_served_proc(self._served_proc)
        self._served_proc = None
        self._served_endpoint = None

        # Reap forward subprocesses before the VM goes away — ssh would
        # die on its own but the Popen handles would linger as zombies.
        self._port_forwards.cleanup()

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
            # virtiofsd self-terminates when the VM disconnects; reap
            # the child processes so they don't linger as zombies.
            self._reap_dead_virtiofsd()
            return

        # Wait for shutdown to complete.
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if not domain.isActive():
                    logger.info("Domain %s shut down gracefully", self._dom_name)
                    self._reap_dead_virtiofsd()
                    return
            except libvirt.libvirtError:
                self._reap_dead_virtiofsd()
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
        self._reap_dead_virtiofsd()

    def get_screenshots_dir(self) -> Path | None:
        return self._screenshots_dir

    def get_vnc_port(self) -> int | None:
        """Discover the auto-assigned VNC port from the live domain XML."""
        if not self._computer_use:
            return None
        import libvirt
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                return None
            return extract_vnc_port_from_xml(domain.XMLDesc(0))
        except libvirt.libvirtError:
            return None

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Libvirt computer-use runs wayvnc with no authentication.
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        return frozenset()

    def get_text_input_state_path(self) -> Path | None:
        return None

    def get_text_input_active(self) -> bool:
        return False

    # -- Computer-use operations ---------------------------------------------

    def take_screenshot(self, output_path: Path) -> None:
        """Take a screenshot of the VM display via QMP ``screendump``.

        Uses QMP to capture directly from the QEMU display device,
        which works uniformly with and without VirGL and correctly
        captures XWayland windows (unlike ``grim`` / ``wlr-screencopy``).
        """
        qmp_screendump(self._conn, self._dom_name, output_path)

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        """Click at screen coordinates via QMP."""
        qmp_send_mouse_event(
            self._conn, self._dom_name, x, y, button=button,
        )

    def send_type(self, text: str) -> None:
        """Type text via QMP key events."""
        qmp_type_text(self._conn, self._dom_name, text)

    def send_key(self, key_str: str) -> None:
        """Press a key or combo (e.g. ``"ctrl+a"``) via QMP."""
        qmp_send_key_combo(self._conn, self._dom_name, key_str)

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        """Scroll at screen coordinates via QMP."""
        qmp_send_scroll_event(
            self._conn, self._dom_name, x, y, direction, amount,
        )

    def focus_window(self, name: str) -> None:
        """Focus a window by name — not supported in VM contexts."""
        raise NotImplementedError(
            "Window focus via toplevel is not supported in VM contexts. "
            "Use computer_click to click on the desired window, or use "
            "alt+Tab to switch windows."
        )

    def get_clipboard(self) -> str:
        """Get clipboard contents via wl-paste over SSH."""
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        result = subprocess.run(
            [
                "ssh", *ssh_opts, f"{SANDBOX_USER}@localhost",
                "env", f"XDG_RUNTIME_DIR=/run/user/{SANDBOX_UID}",
                "WAYLAND_DISPLAY=wayland-0",
                "wl-paste", "--no-newline", "--primary",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def set_clipboard(self, text: str) -> None:
        """Set clipboard contents via wl-copy over SSH.

        wl-copy forks a background process to serve paste requests, which
        keeps the SSH connection alive indefinitely. Work around this by
        saving stdin to a tmpfile, then backgrounding wl-copy with nohup
        so the SSH session can exit cleanly.
        """
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        # Shell script: save stdin to tmpfile, background wl-copy, exit.
        remote_cmd = (
            'tmpf=$(mktemp);'
            ' cat > "$tmpf";'
            f' env XDG_RUNTIME_DIR=/run/user/{SANDBOX_UID} WAYLAND_DISPLAY=wayland-0'
            ' nohup wl-copy < "$tmpf" >/dev/null 2>&1 &'
            ' sleep 0.1;'
            ' rm "$tmpf"'
        )
        result = subprocess.run(
            ["ssh", *ssh_opts, f"{SANDBOX_USER}@localhost", remote_cmd],
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"wl-copy failed: {result.stderr.strip()}")

    # -- Phone-use operations (Waydroid Android) -----------------------------

    def _waydroid_ssh_ctx(self) -> tuple[list[str], str]:
        """Return ``(ssh_opts, target)`` for driving Waydroid over SSH."""
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        assert self._ssh_port is not None
        ssh_opts = _ssh_common_opts(self._sdir / "ssh_key", self._ssh_port)
        return ssh_opts, f"{SANDBOX_USER}@localhost"

    def _ssh_run(
        self, remote: str, *, timeout: int, input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a single *remote* command in the guest over SSH (text mode)."""
        ssh_opts, target = self._waydroid_ssh_ctx()
        return subprocess.run(
            ["ssh", *ssh_opts, target, remote],
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _android_boot_completed(self, ssh_opts: list[str], target: str) -> bool:
        """Return ``True`` if Android reports ``sys.boot_completed == 1``."""
        probe = subprocess.run(
            [
                "ssh", *ssh_opts, target,
                "sudo waydroid shell -- getprop sys.boot_completed",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return probe.returncode == 0 and probe.stdout.strip() == "1"

    def _waydroid_desynced(self, ssh_opts: list[str], target: str) -> bool:
        """Return ``True`` for the ``Session: RUNNING`` / ``Container: STOPPED``
        desync that follows an unclean Waydroid stop."""
        status = subprocess.run(
            ["ssh", *ssh_opts, target, "waydroid status"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        fields = {
            k.strip(): v.strip()
            for line in status.stdout.splitlines()
            if ":" in line
            for k, v in [line.split(":", 1)]
        }
        return (
            fields.get("Session") == "RUNNING"
            and fields.get("Container") == "STOPPED"
        )

    def _wait_for_android_boot(self, ssh_opts: list[str], target: str) -> None:
        deadline = time.monotonic() + _ANDROID_BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._android_boot_completed(ssh_opts, target):
                self._phone_booted = True
                return
            time.sleep(_ANDROID_BOOT_POLL_S)
        raise RuntimeError(
            f"Waydroid did not finish booting for context {self._context_name} "
            f"within {_ANDROID_BOOT_TIMEOUT_S}s"
        )

    def ensure_phone_running(self) -> None:
        """Bring the Waydroid session up and wait for Android to finish booting.

        Idempotent and self-healing: once booted, a single cheap probe short-
        circuits the whole sequence; otherwise it starts a down session and
        recovers a desynced Session/Container state via a full restart.
        """
        ssh_opts, target = self._waydroid_ssh_ctx()

        # Steady state (the model taps many times in a row): one probe instead
        # of the status + is-active + boot-poll round-trips below.
        if self._phone_booted and self._android_boot_completed(ssh_opts, target):
            return
        self._phone_booted = False

        if self._waydroid_desynced(ssh_opts, target):
            logger.warning(
                "Waydroid Session/Container desync for context %s; resetting",
                self._context_name,
            )
            # Stop the orphaned session before restarting the container +
            # session units, otherwise the new session collides.
            subprocess.run(
                ["ssh", *ssh_opts, target, "waydroid session stop"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self._start_waydroid_session(ssh_opts, target)
        else:
            self._ensure_waydroid_session_running(ssh_opts, target)

        self._wait_for_android_boot(ssh_opts, target)
        self._apply_android_display_config()

    def _apply_android_display_config(self) -> None:
        """Apply ``android.resolution``/``dpi`` via ``wm size``/``wm density``.

        Runs only after a fresh boot (the steady-state path short-circuits
        before this), so the single ``wm`` round-trip is a once-per-boot cost.
        Resolution is pre-validated as ``WIDTHxHEIGHT`` at config load and dpi
        is an int, so both are safe to interpolate into the shell command.
        """
        android = self._config.android
        if android is None:
            return
        cmds: list[str] = []
        if android.resolution:
            cmds.append(f"wm size {android.resolution}")
        if android.dpi:
            cmds.append(f"wm density {android.dpi}")
        if not cmds:
            return
        inner = "; ".join(cmds)
        result = self._ssh_run(
            f"sudo waydroid shell -- sh -c {shlex.quote(inner)}", timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to apply Android display config (%s) for context "
                "%s: %s",
                inner, self._context_name,
                (result.stderr or result.stdout).strip(),
            )

    def phone_shell(self, cmd: str) -> str:
        """Run *cmd* in the Android environment via ``waydroid shell``.

        Wraps the command in Android's ``sh -c`` so pipes, redirects, and
        multi-token commands (``input``, ``uiautomator``, ``pm``/``am``,
        ``wm``, …) all work.  ``lxc-attach`` runs argv directly, so the
        remote-side quoting is a single ``shlex.quote`` for the guest shell.
        """
        ssh_opts, target = self._waydroid_ssh_ctx()
        # lxc-attach gives a bare env missing the Android shell's standard vars.
        # Legacy `uiautomator dump` builds new File(getenv("EXTERNAL_STORAGE"))
        # in DumpCommand's static init; an unset var throws an NPE that fails
        # class init and crashes the VM (exit 137). Export the vars so it works.
        wrapped = f"{_ANDROID_SHELL_ENV}{cmd}"
        remote = f"sudo waydroid shell -- sh -c {shlex.quote(wrapped)}"
        result = subprocess.run(
            ["ssh", *ssh_opts, target, remote],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            self._phone_booted = False
            stderr = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"waydroid shell failed: {stderr}")
        # Android's shell folds warnings into stderr; append them after stdout
        # so the model sees them without losing the primary output.
        out = result.stdout
        if result.stderr:
            out = f"{out}\n{result.stderr}" if out else result.stderr
        return out

    def phone_screenshot(self, output_path: Path) -> None:
        """Capture the Android framebuffer via ``screencap`` and save as PNG.

        ``screencap`` writes a file inside Android's ``/data/local/tmp`` (the
        same inode as the guest path below); ``cat`` streams the bytes back in
        the same SSH round-trip.  ``screencap``'s own stdout is discarded so
        only the PNG reaches ours.  Binary is safe here — only the interactive
        ``waydroid shell`` pty corrupts it.
        """
        ssh_opts, target = self._waydroid_ssh_ctx()
        android_png = "/data/local/tmp/os_screenshot.png"
        guest_png = f"{SANDBOX_HOME}/.local/share/waydroid/data/local/tmp/os_screenshot.png"

        result = subprocess.run(
            [
                "ssh", *ssh_opts, target,
                f"sudo waydroid shell -- screencap -p {android_png} >/dev/null "
                f"&& sudo cat {guest_png}",
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            self._phone_booted = False
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"phone screenshot failed: {stderr}")
        output_path.write_bytes(result.stdout)

    def phone_install_apk(self, apk_path: str) -> str:
        """Install a guest-side APK into Android via ``waydroid app install``.

        *apk_path* is a path in the guest filesystem (where the sandboxed
        ``Bash`` tool, downloads, and mounted project files all live) — not an
        Android-internal path.  ``waydroid app install`` is a guest-side
        Waydroid command, so it cannot be reached through ``phone_shell``
        (which runs *inside* Android); this is the convenience wrapper for it.

        Unlike ``waydroid shell`` (root ``lxc-attach``, hence ``sudo`` for the
        other phone tools), ``waydroid app`` talks to the *SessionManager* on
        the Waydroid user's DBus **session** bus. So it must run *without*
        ``sudo`` (as root the session bus is wrong/absent) and needs
        ``DBUS_SESSION_BUS_ADDRESS`` pointed at that user's bus, which a
        non-interactive SSH shell does not set. Getting either wrong makes
        every ``waydroid app`` command abort with "WayDroid session is stopped"
        regardless of whether the session is actually up.
        """
        env = (
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
            "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
        )
        install = self._ssh_run(
            f"{env}waydroid app install {shlex.quote(apk_path)}", timeout=180,
        )
        out = (install.stdout or install.stderr).strip()
        # ``waydroid app install`` exits 0 even when it fails to reach the
        # session, printing only "WayDroid session is stopped" — so the
        # returncode guard alone lets a silent failure look like success. Treat
        # that sentinel as an error so it surfaces instead of faking a success.
        if install.returncode != 0 or "session is stopped" in out.lower():
            raise RuntimeError(f"waydroid app install failed: {out or 'no output'}")
        return out or "Installed."

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
            f"{SANDBOX_USER}@localhost",
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
                f"{SANDBOX_USER}@localhost:{vm_path}",
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

    # -- Port forwarding ------------------------------------------------------

    def supports_port_forwarding(self) -> bool:
        return True

    def add_port_forward(
        self,
        guest_port: int,
        requested_host_port: int | None,
        scope_key: str | None,
        description: str | None,
    ) -> PortForward:
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        if self._ssh_port is None:
            raise RuntimeError(
                "Cannot add port forward: VM is not running"
            )
        host_port = allocate_host_port(requested_host_port, guest_port)
        cmd = [
            "ssh",
            *_ssh_common_opts(self._sdir / "ssh_key", self._ssh_port),
            *SSH_TUNNEL_OPTS,
            "-L", f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
            f"{SANDBOX_USER}@localhost",
        ]
        return open_ssh_tunnel(
            cmd,
            guest_port=guest_port,
            host_port=host_port,
            scope_key=scope_key,
            description=description,
            registry=self._port_forwards,
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return self._port_forwards.remove(forward_id)

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        return self._port_forwards.list(scope_key)

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        self._port_forwards.cleanup(scope_key)

    # -- Internal helpers -----------------------------------------------------

    def _shared_dirs_and_overrides(
        self,
    ) -> tuple[list[str], dict[str, str], set[str]]:
        """Return ``(all_dirs, mount_overrides, readonly_dirs)`` for domain XML / mounts.

        ``all_dirs`` is the list of host directories that need virtiofs/9p
        filesystem devices.  ``mount_overrides`` maps host paths that
        should be mounted at a *different* guest path (tmp and .claude).
        ``readonly_dirs`` is the subset of ``all_dirs`` that should be
        mounted read-only inside the guest.
        """
        all_dirs = [self._project_dir] + self._additional_directories
        if self._screenshots_dir is not None:
            all_dirs.append(str(self._screenshots_dir))
        all_dirs.append(str(self._tmp_dir))
        all_dirs.append(str(self._claude_home_dir))
        # The task-output share must mount at the guest path the agent CLI
        # actually writes to (Claude → /tmp/claude-<uid>), not a vendor-neutral
        # /tmp/<user>-<uid>; otherwise the CLI writes to an unshared guest path
        # and the host terminal mini app finds nothing ("View output" 400s).
        bundle = self._runtime.image_bundle if self._runtime else None
        task_tmp_guest = (
            bundle.guest_task_tmp(SANDBOX_UID)
            if bundle is not None
            else f"/tmp/claude-{SANDBOX_UID}"
        )
        mount_overrides = {
            str(self._tmp_dir): task_tmp_guest,
            str(self._claude_home_dir): f"{SANDBOX_HOME}/.claude",
        }
        # Served-endpoint launch only: sync each declared host_dir into the
        # guest at its declared mount point.  The mount SOURCE is whatever
        # path the served runtime's ``inject`` writes to host-side (provider
        # ``auth.json``, managed plugin config), so the served process (which
        # runs under its own ``HOME``) sees the synced files.  The wrapped-CLI
        # launch contributes ZERO new mounts here.
        for mount in self._served_home_mounts:
            host_str = str(mount.host_dir)
            all_dirs.append(host_str)
            mount_overrides[host_str] = mount.guest_mount_point
        readonly_dirs: set[str] = set()
        host_skills = Path.home() / ".claude" / "skills"
        if host_skills.is_dir():
            host_skills_str = str(host_skills)
            all_dirs.append(host_skills_str)
            mount_overrides[host_skills_str] = f"{SANDBOX_HOME}/.claude/skills"
            readonly_dirs.add(host_skills_str)
        return all_dirs, mount_overrides, readonly_dirs

    def _virtiofs_socket_for(self, host_dir: str) -> Path:
        """Return the virtiofsd socket path for a host directory."""
        tag = _fs_tag_for_dir(host_dir)
        return self._sdir / f"{tag}.sock"

    def _start_all_virtiofsd(self) -> None:
        """Start virtiofsd instances for all shared directories."""
        all_dirs, _, readonly_dirs = self._shared_dirs_and_overrides()
        for host_dir in all_dirs:
            sock = self._virtiofs_socket_for(host_dir)
            proc = start_virtiofsd(sock, host_dir, readonly=host_dir in readonly_dirs)
            self._virtiofsd_procs.append(proc)
        # Wait for all sockets to appear.
        import time as _time
        all_socks = [self._virtiofs_socket_for(d) for d in all_dirs]
        for _ in range(20):
            if all(s.exists() for s in all_socks):
                break
            _time.sleep(0.1)

    def _reap_dead_virtiofsd(self) -> None:
        """Reap exited virtiofsd child processes to avoid zombies.

        Does not kill live processes — virtiofsd self-terminates when the
        VM disconnects.  This only collects exit status so the kernel can
        release the process table entries.
        """
        alive: list[subprocess.Popen[bytes]] = []
        for proc in self._virtiofsd_procs:
            if proc.poll() is not None:
                logger.info(
                    "Reaped virtiofsd (pid=%d, rc=%d)", proc.pid, proc.returncode,
                )
            else:
                alive.append(proc)
        self._virtiofsd_procs = alive

    def _is_domain_active(self) -> bool:
        """Check if the domain is currently active."""
        import libvirt
        try:
            domain = self._conn.lookupByName(self._dom_name)
            return domain.isActive()
        except libvirt.libvirtError:
            return False

    def _rebuild_vm(self, *, log_file: Path | None = None) -> None:
        """Destroy the VM, delete the overlay, and recreate from scratch.

        Used when SSH is unreachable after boot — typically due to corrupt
        SSH host keys from a hard kill (SIGKILL / virsh destroy).
        """
        import libvirt

        # 1. Destroy + undefine the domain (virtiofsd self-terminates).
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if domain.isActive():
                domain.destroy()
            domain.undefine()
            logger.info("Undefined domain %s for rebuild", self._dom_name)
        except libvirt.libvirtError:
            pass
        # Reap any virtiofsd Popen handles whose processes exited along
        # with the destroyed domain — otherwise they linger as zombies.
        self._reap_dead_virtiofsd()

        # 2. Delete the overlay (forces fresh cloud-init on next boot).
        #    Persistent volume files (pv-*.qcow2) are intentionally preserved
        #    so that data survives rebuilds.
        overlay = self._sdir / "overlay.qcow2"
        overlay.unlink(missing_ok=True)
        # Also delete cloud-init ISO so it gets regenerated.
        (self._sdir / "cloud-init.iso").unlink(missing_ok=True)
        logger.info("Deleted overlay and cloud-init for rebuild")

        # 3. Re-run ensure_environment to regenerate overlay + cloud-init.
        # Do NOT start the domain here — let the caller's ensure_running()
        # handle it so it correctly detects a cold start and waits for
        # cloud-init to complete.
        self.ensure_environment(log_file=log_file)
