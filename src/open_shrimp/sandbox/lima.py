"""Lima-based sandbox for isolated Claude CLI execution on macOS.

Uses Lima (Apple Virtualization.framework via the VZ driver) for full VM
isolation.  VirtioFS provides fast filesystem sharing between the host
and the guest (Linux or macOS).

VMs are **persistent**: one long-lived VM per context, kept warm between
Claude sessions.  Cold boot is ~30 s, so VMs should stay running.  The
CLI wrapper uses ``limactl shell`` to exec commands inside the VM.

Implements the :class:`~open_shrimp.sandbox.base.Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import yaml
from open_shrimp.config import SandboxConfig
from open_shrimp.security_key.vm_helper_binary import (
    BINARY_NAME as SECURITY_KEY_HELPER_BINARY,
    download_url_for_linux_arch,
    install_cmd_for_linux_guest,
)
from open_shrimp.sandbox.agent_runtime import (
    AgentHandle,
    AgentRuntime,
    GuestMount,
    ServedEndpoint,
    ServedSlot,
    WrappedCLI,
    agent_argv0,
    agent_home_guest_dir,
    agent_home_shares,
    run_served_endpoint,
    served_home_mounts,
    task_tmp_guest_paths,
)
from open_shrimp.sandbox.base import (
    VNC_QUIRK_RFB_BGRA_PIXEL_FORMAT,
    VNC_QUIRK_RFB_DROPS_SET_ENCODINGS,
    PortForward,
    VncQuirk,
)
from open_shrimp.sandbox.prefetch import ProgressFn
from open_shrimp.sandbox.port_forward import (
    SSH_TUNNEL_OPTS,
    PortForwardRegistry,
    allocate_host_port,
    open_ssh_tunnel,
)
from open_shrimp.sandbox.skill_paths import SANDBOX_HOME
from open_shrimp.sandbox.lima_helpers import (
    LIMA_GUEST_UID,
    _lima_env,
    _log,
    build_cli_wrapper as _build_cli_wrapper,
    clear_config_fingerprint,
    config_fingerprint,
    guest_mount_point,
    instance_mounts,
    instance_name as _instance_name,
    lima_guest_home,
    lima_template,
    limactl_create,
    limactl_delete,
    limactl_edit,
    limactl_instance,
    limactl_instance_status,
    limactl_shell_check,
    limactl_start,
    limactl_stop,
    load_config_fingerprint,
    mount_key,
    mount_location,
    rewrite_instance_mounts,
    save_config_fingerprint,
    state_dir_for,
    vnc_host_port,
    write_lima_yaml,
)
from open_shrimp.vnc.rfb_snapshot import RfbSnapshotError, capture_to_png

logger = logging.getLogger(__name__)

# Named key → character mapping for wlrctl keyboard input (Linux guests).
_NAMED_KEY_CHARS: dict[str, str] = {
    "return": "\n", "enter": "\n",
    "tab": "\t", "escape": "\x1b",
    "backspace": "\x08", "space": " ",
}

# macOS key code mapping for osascript (macOS guests).
_MACOS_KEY_CODES: dict[str, int] = {
    "return": 36, "enter": 76,
    "tab": 48, "escape": 53,
    "backspace": 51, "delete": 117,
    "space": 49,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_MACOS_MODIFIER_MAP: dict[str, str] = {
    "ctrl": "control down",
    "control": "control down",
    "alt": "option down",
    "option": "option down",
    "shift": "shift down",
    "super": "command down",
    "cmd": "command down",
    "command": "command down",
    "meta": "command down",
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
        guest_os: str = "linux",
        runtimes: Sequence[AgentRuntime] = (),
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._limactl = limactl_path
        self._additional_directories = additional_directories or []
        self._instance_prefix = instance_prefix
        self._computer_use = computer_use
        self._guest_os = guest_os

        # Every agent runtime this guest is laid out for, keyed by name: the
        # instance's mount set is settled when it is written, so a mount
        # appearing later costs a stop and a restart of the VM.
        self._runtimes: dict[str, AgentRuntime] = {r.name: r for r in runtimes}
        # The subset a caller has taken into use.  Only these get their CLI
        # installed into the guest and their credentials written.
        self._in_use: dict[str, AgentRuntime] = {}

        self._sdir = state_dir_for(context_name)
        self._inst_name = _instance_name(context_name, instance_prefix)
        self._tmp_dir = self._sdir / "tmp"
        self._env = _lima_env()  # cached — LIMA_HOME doesn't change

        # SSH tunnel processes for macOS guest port forwarding.
        self._ssh_tunnels: list[subprocess.Popen] = []

        self._port_forwards = PortForwardRegistry()

        # Served-endpoint state, one slot per runtime name: the slot is the
        # endpoint's ``owner``, so two served runtimes in one guest cannot
        # overwrite each other's liveness handle.
        self._served: dict[str, ServedSlot] = {}

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def host_address(self) -> str:
        return "192.168.5.2"

    def add_runtime(self, runtime: AgentRuntime) -> None:
        self._runtimes[runtime.name] = runtime
        self._in_use[runtime.name] = runtime

    def reconfigure(self, config: SandboxConfig) -> None:
        self._config = config

    @property
    def runtimes_in_use(self) -> set[str]:
        return set(self._in_use)

    def _task_tmp_guest_paths(self) -> list[str]:
        """The guest paths the hosted agents write background-task output to.

        Lima merges mount entries by ``location``, so the one host task-output
        dir gets exactly one guest mount point — the first of these, which is
        sorted and therefore the same whichever ChatScope dispatched first.
        :meth:`_link_task_tmp_aliases` symlinks the rest onto it.
        """
        return task_tmp_guest_paths(self._runtimes.values(), LIMA_GUEST_UID)

    def _served_home_mounts(self) -> tuple[GuestMount, ...]:
        """The union of every registered runtime's served-launch host dirs,
        written into the generated Lima YAML."""
        return tuple(
            mount for _rt, mount in served_home_mounts(self._runtimes.values())
        )

    def _agent_home_mounts(self) -> tuple[tuple[str, str], ...]:
        """``(host dir, guest path)`` per wrapped-CLI runtime's agent home.

        The guest path is the runtime's own home re-rooted at the Lima guest
        user's home, which is where a ``limactl shell`` lands.  A served
        runtime is left to :meth:`_served_home_mounts`: its serve process is
        given a ``HOME`` of its own and its home has to arrive under *that*,
        and Lima merges mount entries by ``location``, so one host dir gets
        one guest path either way.
        """
        guest_home = lima_guest_home(self._guest_os)
        return tuple(
            (str(home), agent_home_guest_dir(runtime, guest_home))
            for runtime, home in agent_home_shares(self._runtimes.values())
            if isinstance(runtime.launch, WrappedCLI)
        )

    def environment_ready(self) -> bool:
        """Check if the Lima instance exists (any status)."""
        return limactl_instance_status(self._limactl, self._inst_name) is not None

    def ensure_environment(
        self,
        *,
        log_file: Path | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        """Create the Lima instance from a generated YAML template.

        Idempotent — only creates if the instance doesn't exist.
        Detects config drift and rebuilds if necessary.

        No *progress*: ``limactl`` owns the image download on this path and
        reports it into *log_file* itself.

        A drift in the mount set — what registering a second agent runtime
        produces — is absorbed by rewriting the instance's mount list and
        letting :meth:`ensure_running` restart it, so the guest disk and the
        CLIs installed on it survive.  So is a change to ``cpus``, ``memory``
        or a grown ``disk`` (:meth:`_reconcile_sizing`).  Anything else still
        rebuilds.
        """
        sdir = self._sdir
        sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Detect config drift outside the mount set and the sizing fields.
        template = self._template()
        desired = config_fingerprint(
            template, applied_in_place=self._sizing_fields(),
        )
        saved = load_config_fingerprint(sdir)
        if saved is not None and saved != desired:
            # Drop the fingerprint first: a crash partway through the rebuild
            # must not leave one claiming the instance matches this config.
            clear_config_fingerprint(sdir)
            _log(
                log_file,
                "Lima config changed — rebuilding VM from scratch...",
            )
            logger.info(
                "Config fingerprint drifted for %s — triggering rebuild",
                self._inst_name,
            )
            self._rebuild_vm(log_file=log_file)
            return

        # Check if instance already exists.
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status is not None:
            logger.info(
                "Lima instance %s already exists (status: %s)",
                self._inst_name, status,
            )
            self._reconcile_mounts(template, log_file=log_file)
            self._reconcile_sizing(template, log_file=log_file)
            save_config_fingerprint(sdir, desired)
            _log(log_file, "Lima VM environment ready.")
            return

        _log(log_file, f"Setting up Lima VM for '{self._context_name}'...")

        # Ensure shared directories exist on host.
        for home, _guest_path in self._agent_home_mounts():
            Path(home).mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        # Create the instance (this downloads the image + boots for cloud-init).
        limactl_create(
            self._limactl,
            self._inst_name,
            write_lima_yaml(sdir, template),
            log_file=log_file,
        )

        save_config_fingerprint(sdir, desired)
        _log(log_file, "Lima VM environment ready.")

    def _template(self) -> dict:
        """The instance template for the runtimes registered so far.

        The YAML that gets written, the fingerprint that detects drift and the
        mount list a remount rewrites all read this one rendering — building
        the mount set costs a handful of ``mkdir`` calls and every dispatch
        goes through here.
        """
        args = (
            self._sdir,
            self._config,
            self._project_dir,
            self._additional_directories or None,
            self._computer_use,
        )
        task_tmp_guest_path = self._task_tmp_guest_paths()[0]
        if self._guest_os == "macos":
            from open_shrimp.sandbox.lima_macos_helpers import lima_template_macos
            return lima_template_macos(
                *args,
                agent_home_mounts=self._agent_home_mounts(),
                task_tmp_guest_path=task_tmp_guest_path,
            )
        return lima_template(
            *args,
            context_name=self._context_name,
            agent_home_mounts=self._agent_home_mounts(),
            served_home_mounts=self._served_home_mounts(),
            task_tmp_guest_path=task_tmp_guest_path,
        )

    def _mount_plan(self, template: dict, carried: list[dict]) -> list[dict]:
        """*template*'s shares, reconciled against the ones the instance has.

        Two rules, split by whether a share is the sandbox boundary or an
        agent's machinery:

        A share the guest mounts at its own host path is a context directory,
        and the approval layer treats the context's directories as the
        boundary — so the plan carries exactly the ones the config still
        lists.  Drop an ``additional_directories`` entry and the guest loses
        the mount at the next start.

        Every other share is mounted at a guest-side path an agent owns: its
        home, its plugin config, its task-output dir.  Those the plan keeps
        even when this process has not registered the runtime that asked for
        them, because a guest booted for both agents is one whose next
        dispatch may be either, and rebooting it to drop one agent's homes
        costs a restart to lose what the dispatch after that asks back.

        The task-output dir is the one share whose guest path is taken from
        the instance rather than the template: Lima merges mount entries by
        ``location``, so that one host dir gets one guest path, and
        :meth:`_link_task_tmp_aliases` points every registered agent's
        ``/tmp/<prefix>-<uid>`` at whichever path the boot happened to pick.
        """
        by_location = {mount_location(mount): mount for mount in carried}
        task_tmp = os.path.realpath(self._tmp_dir)
        plan: list[dict] = []
        for entry in template["mounts"]:
            mounted = by_location.get(mount_location(entry))
            if (
                mount_location(entry) == task_tmp
                and mounted is not None
                and guest_mount_point(mounted) is not None
            ):
                entry = {**entry, "mountPoint": mounted["mountPoint"]}
            plan.append(entry)
        planned = {mount_location(entry) for entry in plan}
        plan.extend(
            mount for mount in carried
            if mount_location(mount) not in planned
            and guest_mount_point(mount) is not None
        )
        return plan

    def _reconcile_mounts(
        self, template: dict, *, log_file: Path | None = None,
    ) -> None:
        """Give the existing instance the shares :meth:`_mount_plan` wants.

        Reads the instance's own mount list rather than a hash of the last
        plan written, so a process that starts against a guest another one
        booted sees the shares that guest actually carries and leaves it
        alone.
        """
        carried = instance_mounts(self._inst_name)
        if carried is None:
            return
        plan = self._mount_plan(template, carried)
        if {mount_key(m) for m in plan} == {mount_key(m) for m in carried}:
            return
        if not self._remount({**template, "mounts": plan}, log_file=log_file):
            _log(
                log_file,
                "Lima mount set could not be rewritten — rebuilding VM...",
            )
            self._rebuild_vm(log_file=log_file)

    def _remount(self, template: dict, *, log_file: Path | None = None) -> bool:
        """Give the existing instance *template*'s mount set, or return False.

        Registering a second agent runtime adds that runtime's home shares and
        moves nothing else, and Lima fixes its mount set when the VM starts.
        Rewriting the instance's own ``lima.yaml`` and stopping it leaves
        :meth:`ensure_running` to bring it back with the new shares: tens of
        seconds against the several minutes of deleting the instance and
        reinstalling both agents' CLIs into a fresh guest.

        The VM is stopped before its config is rewritten, so the instance
        config never describes shares a running guest does not have: a crash
        between the two leaves a stopped instance whose old mount list the
        next start reconciles again.

        ``False`` means the caller should rebuild — a macOS guest, whose
        mounts the guest agent materialises as symlinks at boot, a missing
        instance, or a write that failed.
        """
        if self._guest_os != "linux":
            return False
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status is None:
            return False
        if status == "Running":
            _log(log_file, "Agent shares changed — restarting the Lima VM...")
            limactl_stop(self._limactl, self._inst_name)
        try:
            rewrite_instance_mounts(self._inst_name, template["mounts"])
        except (OSError, yaml.YAMLError):
            logger.warning(
                "Could not rewrite the mount set of %s — falling back to a "
                "rebuild", self._inst_name, exc_info=True,
            )
            return False
        # Keep the generated template in step with the instance, so a later
        # rebuild starts from what is actually mounted.
        write_lima_yaml(self._sdir, template)
        return True

    def _sizing_fields(self) -> tuple[str, ...]:
        """Template fields :meth:`_reconcile_sizing` applies to an existing
        instance, which the rebuild fingerprint therefore leaves out.

        A macOS guest keeps ``disk`` in the fingerprint: Lima grows its image,
        but nothing in the guest grows the APFS container onto the new space.
        """
        if self._guest_os == "linux":
            return ("cpus", "memory", "disk")
        return ("cpus", "memory")

    def _reconcile_sizing(
        self, template: dict, *, log_file: Path | None = None,
    ) -> None:
        """Give the existing instance the template's sizing fields.

        Compared against ``limactl list``, which reports what the instance
        config holds whoever wrote it.  A changed field costs a stop and a
        ``limactl edit``; :meth:`ensure_running` starts the VM again.  A
        smaller ``disk`` is skipped with a warning, because Lima cannot shrink
        a disk and would refuse to start an instance configured for it.
        """
        inst = limactl_instance(self._limactl, self._inst_name)
        if inst is None:
            return
        wanted = {
            "cpus": self._config.cpus,
            "memory": self._config.memory * 1024 * 1024,
            "disk": self._config.disk_size * 1024 * 1024 * 1024,
        }
        changed: dict[str, int | str] = {}
        for field in self._sizing_fields():
            have = inst.get(field)
            if not isinstance(have, int) or have == wanted[field]:
                continue
            if field == "disk" and wanted[field] < have:
                msg = (
                    f"sandbox.disk_size is {self._config.disk_size} GiB but "
                    f"the Lima VM's disk is already {have / 1024**3:g} GiB, "
                    "and a disk cannot shrink — keeping the larger disk."
                )
                _log(log_file, msg)
                logger.warning("%s: %s", self._inst_name, msg)
                continue
            changed[field] = template[field]
        if not changed:
            return

        if inst.get("status") == "Running":
            _log(
                log_file,
                f"VM sizing changed ({', '.join(changed)}) — "
                "restarting the Lima VM...",
            )
            limactl_stop(self._limactl, self._inst_name)
        try:
            limactl_edit(self._limactl, self._inst_name, changed)
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "limactl edit of %s failed (%s) — falling back to a rebuild",
                self._inst_name, (exc.stderr or "").strip(),
            )
            _log(log_file, "Lima VM sizing could not be changed — rebuilding VM...")
            self._rebuild_vm(log_file=log_file)

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
            if self._guest_os == "macos":
                # macOS guests often start in DEGRADED state because
                # SSH agent forwarding requires sudo which isn't
                # available until our askpass provision runs.
                # limactl start exits non-zero for DEGRADED, but the
                # VM is still usable — don't treat it as fatal.
                try:
                    limactl_start(
                        self._limactl, self._inst_name, log_file=log_file,
                    )
                except subprocess.CalledProcessError:
                    # Check if the VM came up despite the error.
                    recheck = limactl_instance_status(
                        self._limactl, self._inst_name,
                    )
                    if recheck != "Running":
                        raise
                    logger.warning(
                        "limactl start returned non-zero for %s but VM is "
                        "running (likely DEGRADED state — expected for "
                        "macOS guests before askpass is provisioned)",
                        self._inst_name,
                    )
            else:
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

        if self._guest_os == "macos":
            from open_shrimp.sandbox.lima_macos_helpers import (
                ensure_mounts_macos,
                reboot_if_first_provision,
            )
            mount_points = [self._project_dir] + self._additional_directories

            # Auto-login only takes effect on boot —
            # reboot once after first provisioning.  Do this before
            # mount fixups so we don't have to redo them after reboot.
            reboot_if_first_provision(
                self._limactl, self._inst_name, log_file=log_file,
            )

            # Fix up VirtioFS mount symlinks — the guest agent may have
            # failed on first boot because parent directories didn't exist.
            ensure_mounts_macos(
                self._limactl, self._inst_name, mount_points,
            )

            # Set up SSH tunnels for port forwarding.
            if self._computer_use:
                self._ensure_ssh_tunnels()

    def provision_workspace(self, *, log_file: Path | None = None) -> None:
        """Install computer-use helpers, runtime CLI binary, and credentials."""
        if self._computer_use:
            try:
                self._install_security_key_helper()
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "Security-key helper install failed for Lima context %s; "
                    "continuing without security-key forwarding: %s",
                    self._context_name,
                    exc,
                )

        for runtime in self._in_use.values():
            bundle = runtime.image_bundle
            if bundle is not None and bundle.lima_install is not None:
                bundle.lima_install(self._limactl, self._inst_name, self._guest_os)

            # Each runtime's credentials land in its own home — the very dir
            # the guest mounts as that agent's home.
            if runtime.provision_credentials is not None:
                runtime.provision_credentials(runtime.home_mount.host_dir)

        self._link_task_tmp_aliases()

    def _link_task_tmp_aliases(self) -> None:
        """Point every other agent's task-output path at the mounted one.

        Each CLI picks its own ``/tmp/<prefix>-<uid>`` — Claude writes
        ``/tmp/claude-1000``, OpenCode ``/tmp/openshrimp-1000`` — and the host
        reads both from one directory, the context's ``tmp``.  Lima merges
        mount entries by ``location``, so that directory gets one mount point
        and the others reach it through a symlink; without one the second agent
        writes to guest-local disk and "View output" finds nothing.

        Which path carries the mount is the booting runtime set's choice, not
        this process's, so it is read back from the instance config: a guest
        booted for both agents mounts at ``/tmp/claude-<uid>`` and still
        serves a process that has registered only OpenCode.

        Runs on every dispatch because ``/tmp`` is emptied by a guest reboot.
        """
        if self._guest_os != "linux":
            return
        mounted = self._mounted_task_tmp()
        aliases = [p for p in self._task_tmp_guest_paths() if p != mounted]
        if not aliases:
            return
        # rmdir clears the empty directory an agent that started before the
        # link left behind; anything still standing after it — a live link, a
        # directory with output already in it — is left alone.
        script = "; ".join(
            f"rmdir {shlex.quote(alias)} 2>/dev/null; [ -e {shlex.quote(alias)} ] "
            f"|| ln -sfn {shlex.quote(mounted)} {shlex.quote(alias)}"
            for alias in aliases
        )
        rc, _stdout, stderr = self._exec_in_vm_sync(script)
        if rc != 0:
            logger.warning(
                "Could not link task-output paths %s to %s in %s: %s",
                ", ".join(aliases), mounted, self._inst_name, stderr.strip(),
            )

    def _mounted_task_tmp(self) -> str:
        """The guest path the instance gives the host task-output dir.

        Falls back to the path this process's runtime set would pick, which is
        what a guest about to be created from that set will carry.
        """
        task_tmp = os.path.realpath(self._tmp_dir)
        for mount in instance_mounts(self._inst_name) or []:
            if mount_location(mount) == task_tmp:
                return guest_mount_point(mount) or mount["location"]
        return self._task_tmp_guest_paths()[0]

    def _install_security_key_helper(self) -> None:
        if self._guest_os != "linux":
            logger.info(
                "Security-key helper install only supports linux guests; skipping "
                "guest_os=%s", self._guest_os,
            )
            return

        from open_shrimp.security_key.guest_setup import setup_security_key_guest_cmd
        from open_shrimp.sandbox.lima_helpers import install_cli_in_linux_vm

        setup_result = subprocess.run(
            [
                self._limactl,
                "shell",
                self._inst_name,
                "--",
                "bash",
                "-c",
                setup_security_key_guest_cmd(),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if setup_result.returncode != 0:
            error = (setup_result.stderr or setup_result.stdout).strip()
            raise RuntimeError(
                f"Failed to provision UHID support for {SECURITY_KEY_HELPER_BINARY}: "
                f"{error}"
            )

        install_cli_in_linux_vm(
            self._limactl,
            self._inst_name,
            SECURITY_KEY_HELPER_BINARY,
            install_cmd_for=lambda arch: install_cmd_for_linux_guest(
                download_url_for_linux_arch(arch)
            ),
            timeout=300,
        )

    def start_agent(self, runtime: AgentRuntime) -> AgentHandle:
        if isinstance(runtime.launch, WrappedCLI):
            cli_path, cleanup_paths = self.build_cli_wrapper(runtime)
            return AgentHandle(cli_path=cli_path, cleanup_paths=cleanup_paths)
        if isinstance(runtime.launch, ServedEndpoint):
            return self._start_served_endpoint(runtime, runtime.launch)
        raise NotImplementedError(
            f"Unsupported launch strategy: {runtime.launch!r}"
        )

    def _start_served_endpoint(
        self, runtime: AgentRuntime, launch: ServedEndpoint,
    ) -> AgentHandle:
        """Run the serve argv via ``limactl shell`` and reach its port.

        The runtime supplies the serve argv + env + inject hook; this sandbox
        owns only the ``limactl shell`` exec and hands the tunnel to
        :meth:`reach` (an ``ssh -L`` forward).  The shared launch body lives in
        :func:`run_served_endpoint`.

        Guest-image precondition: this launch does **not** provision the VM
        image.  The ``opencode`` binary must already be on the guest ``PATH``
        (Lima template / ``provision`` script — operator's responsibility,
        documented in CLAUDE.md → Backends).  The per-context ``opencode-home``
        (→ ``{SANDBOX_HOME}/.local/share/opencode``) and ``openshrimp-data`` (→
        ``{SANDBOX_HOME}/.local/share/openshrimp``) host dirs are declared as
        virtiofs mounts in the generated Lima YAML (see ``_build_mounts``), and
        ``runtime.inject`` syncs the provider ``auth.json`` + managed plugin
        config into them, so they reach the served process (which runs with
        ``HOME={SANDBOX_HOME}``).  When the binary is absent, the serve process
        exits early and readiness wait raises.
        """
        slot = self._served.setdefault(runtime.name, ServedSlot())
        live = slot.live_handle()
        if live is not None:
            return live

        if limactl_instance_status(self._limactl, self._inst_name) != "Running":
            raise RuntimeError("Cannot start served endpoint: Lima VM is not running")

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
            return subprocess.Popen(
                [
                    self._limactl, "shell", self._inst_name,
                    "--", "bash", "-lc", remote_cmd,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._env,
            )

        proc, endpoint = run_served_endpoint(
            runtime,
            launch,
            spawn=spawn,
            reach=self.reach,
            owner=slot,
            log_label=f"Lima context '{self._context_name}'",
        )
        slot.adopt(proc, endpoint)
        return AgentHandle(endpoint=endpoint)

    def build_cli_wrapper(self, runtime: AgentRuntime) -> tuple[str, list[str]]:
        path = _build_cli_wrapper(
            self._context_name,
            self._sdir,
            self._limactl,
            project_dir=self._project_dir,
            inst_name=self._inst_name,
            argv0=agent_argv0(runtime),
            guest_os=self._guest_os,
        )
        return path, [path]

    def shell_argv(self) -> list[str]:
        # limactl finds the instance through LIMA_HOME, which this process
        # carries only in the env dicts it hands its own subprocesses.
        return [
            "env", f"LIMA_HOME={_lima_env()['LIMA_HOME']}",
            self._limactl, "shell", "--workdir", self._project_dir,
            self._inst_name,
        ]

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
        if self._guest_os == "macos":
            raise NotImplementedError(
                "security-key helper requires Linux UHID support"
            )
        log_path = f"/tmp/openshrimp-security-key-helper-{session_id}.log"
        helper_cmd = shlex.join([
            "openshrimp-security-key-vm-helper",
            "--relay-url", relay_url,
            "--session-id", session_id,
            "--token", token,
        ])
        cmd = (
            "command -v openshrimp-security-key-vm-helper >/dev/null && "
            "sudo -n true && "
            f"(nohup sudo -n {helper_cmd} > {shlex.quote(log_path)} 2>&1 "
            "< /dev/null &)"
        )
        rc, stdout, stderr = self._exec_in_vm_sync(cmd, timeout_secs=10.0)
        if rc != 0:
            error = (stderr or stdout).strip()
            if not error:
                error = (
                    "openshrimp-security-key-vm-helper is not installed in the VM "
                    "or passwordless sudo is unavailable"
                )
            raise RuntimeError(f"security-key helper failed to start: {error}")

    # -- Phone use (libvirt only) --------------------------------------------

    def ensure_phone_running(self) -> None:
        raise NotImplementedError(
            "Phone use (Waydroid) is only supported on the libvirt backend."
        )

    def phone_shell(self, cmd: str) -> str:
        raise NotImplementedError(
            "Phone use (Waydroid) is only supported on the libvirt backend."
        )

    def phone_screenshot(self, output_path: Path) -> None:
        raise NotImplementedError(
            "Phone use (Waydroid) is only supported on the libvirt backend."
        )

    def phone_install_apk(self, apk_path: str) -> str:
        raise NotImplementedError(
            "Phone use (Waydroid) is only supported on the libvirt backend."
        )

    def stop(self) -> None:
        """Stop the Lima instance and any SSH tunnels."""
        # Tear down every served process (the ssh -L tunnels are reaped below
        # with the rest of the port forwards).
        for slot in self._served.values():
            slot.close()

        # Reap forward subprocesses before the VM goes away.
        self._port_forwards.cleanup()
        for proc in self._ssh_tunnels:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._ssh_tunnels.clear()

        status = limactl_instance_status(self._limactl, self._inst_name)
        if status == "Running":
            limactl_stop(self._limactl, self._inst_name)

    def get_screenshots_dir(self) -> Path | None:
        if self._computer_use:
            return self._sdir / "screenshots"
        return None

    def get_vnc_port(self) -> int | None:
        if not self._computer_use:
            return None
        if self._guest_os == "macos":
            # macOS guests render via VZMacGraphics; the patched limactl
            # publishes an _VZVNCServer port through Lima's hostagent,
            # which writes <LIMA_HOME>/<instance>/vncdisplay.
            return self._read_vz_vnc_port()
        # Linux guests use the YAML port-forward to in-VM wayvnc on 5900.
        return vnc_host_port(self._context_name)

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Linux wayvnc and the macOS-guest _VZVNCServer (configured with
        # NoSecurity) both run unauthenticated on localhost; the WS proxy
        # is the access boundary.
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        # The patched limactl drives Apple's _VZVNCServer SPI, which
        # crashes on SetEncodings (RFB type 2), resets on SetPixelFormat
        # (type 0), and advertises a ServerInit pixel format whose shifts
        # don't match the BGRA bytes it puts on the wire.  The proxy
        # strips the offending client messages and rewrites the server's
        # pixel-format advertisement to match the actual byte order.
        if self._computer_use and self._guest_os == "macos":
            return frozenset({
                VNC_QUIRK_RFB_DROPS_SET_ENCODINGS,
                VNC_QUIRK_RFB_BGRA_PIXEL_FORMAT,
            })
        return frozenset()

    def _read_vz_vnc_port(self) -> int | None:
        """Read the bound _VZVNCServer port from Lima's ``vncdisplay`` file.

        File format is ``<host>:<displaynum>`` with ``displaynum =
        port - 5900``.  Lima writes it once after VM start; until then
        the file is absent and the proxy reports "VNC port not available".
        """
        vnc_file = Path(self._env["LIMA_HOME"]) / self._inst_name / "vncdisplay"
        try:
            content = vnc_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        try:
            _host, num = content.rsplit(":", 1)
            return int(num) + 5900
        except ValueError:
            logger.warning(
                "Cannot parse %s: %r (expected host:displaynum)",
                vnc_file, content,
            )
            return None

    # -- Computer-use operations ------------------------------------------------

    def _exec_in_vm_sync(
        self, cmd: str, *, timeout_secs: float = 10.0,
        stdin_data: str | None = None,
    ) -> tuple[int, str, str]:
        """Run a shell command inside the VM via ``limactl shell``.

        *cmd* is a shell command string (passed to ``bash -c``).
        For Linux guests, the Wayland environment is exported automatically.
        """
        if self._guest_os == "macos":
            shell_cmd = cmd
        else:
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
        if self._guest_os == "macos":
            port = self._read_vz_vnc_port()
            if port is None:
                raise RuntimeError(
                    "VZ host VNC port not yet published — is the VM running "
                    "with video.display=vnc and the patched limactl?"
                )
            try:
                capture_to_png("127.0.0.1", port, output_path)
            except RfbSnapshotError as e:
                raise RuntimeError(f"VZ VNC snapshot failed: {e}") from e
            return
        ts = int(output_path.stem.split("-")[-1]) if "-" in output_path.stem else 0
        guest_path = f"/tmp/screenshots/screenshot-{ts}.png"
        rc, _, stderr = self._exec_in_vm_sync(f"grim {guest_path}")
        if rc != 0:
            raise RuntimeError(f"grim failed: {stderr.strip()}")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        if self._guest_os == "macos":
            self._send_click_macos(x, y, button)
        else:
            rc, _, stderr = self._exec_in_vm_sync(
                f"wlrctl pointer move {x} {y} && wlrctl pointer click {button}"
            )
            if rc != 0:
                raise RuntimeError(f"click failed: {stderr.strip()}")

    def send_type(self, text: str) -> None:
        if self._guest_os == "macos":
            self._send_type_macos(text)
        else:
            rc, _, stderr = self._exec_in_vm_sync(
                f"wlrctl keyboard type {shlex.quote(text)}"
            )
            if rc != 0:
                raise RuntimeError(f"type failed: {stderr.strip()}")

    def send_key(self, key_str: str) -> None:
        if self._guest_os == "macos":
            self._send_key_macos(key_str)
            return
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
        if self._guest_os == "macos":
            self._send_scroll_macos(x, y, direction, amount)
            return
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
        if self._guest_os == "macos":
            self._focus_window_macos(name)
            return
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl toplevel focus {shlex.quote(name)}"
        )
        if rc != 0:
            raise RuntimeError(f"focus failed: {stderr.strip()}")

    def get_clipboard(self) -> str:
        if self._guest_os == "macos":
            rc, stdout, _ = self._exec_in_vm_sync("pbpaste")
            return stdout if rc == 0 else ""
        rc, stdout, _ = self._exec_in_vm_sync("wl-paste --no-newline --primary")
        if rc != 0:
            return ""
        return stdout

    def set_clipboard(self, text: str) -> None:
        if self._guest_os == "macos":
            rc, _, stderr = self._exec_in_vm_sync("pbcopy", stdin_data=text)
            if rc != 0:
                raise RuntimeError(f"pbcopy failed: {stderr.strip()}")
            return
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

    # -- macOS computer-use helpers -------------------------------------------

    def _send_click_macos(self, x: int, y: int, button: str = "left") -> None:
        """Click at coordinates using Python+Quartz CGEvent."""
        btn_map = {
            "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
            "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
            "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
        }
        down_evt, up_evt, btn_const = btn_map.get(button, btn_map["left"])
        py_script = (
            f"from Quartz.CoreGraphics import *; import time; "
            f"p=CGPointMake({x},{y}); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, {btn_const})); "
            f"time.sleep(0.05); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, {down_evt}, p, {btn_const})); "
            f"time.sleep(0.05); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, {up_evt}, p, {btn_const}))"
        )
        rc, _, stderr = self._exec_in_vm_sync(
            f"python3 -c {shlex.quote(py_script)}", timeout_secs=15.0,
        )
        if rc != 0:
            raise RuntimeError(f"click failed: {stderr.strip()}")

    def _send_type_macos(self, text: str) -> None:
        """Type text using osascript keystroke."""
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{escaped}"'
        rc, _, stderr = self._exec_in_vm_sync(
            f"osascript -e {shlex.quote(script)}"
        )
        if rc != 0:
            raise RuntimeError(f"type failed: {stderr.strip()}")

    def _send_key_macos(self, key_str: str) -> None:
        """Press a key or key combo using osascript key code."""
        parts = key_str.split("+")
        key_name = parts[-1].lower()
        modifiers = parts[:-1] if len(parts) > 1 else []

        # Build modifier clause.
        modifier_clause = ""
        if modifiers:
            mod_strs = []
            for m in modifiers:
                mapped = _MACOS_MODIFIER_MAP.get(m.lower())
                if mapped:
                    mod_strs.append(mapped)
            if mod_strs:
                modifier_clause = " using {" + ", ".join(mod_strs) + "}"

        # Use key code for named keys, keystroke for characters.
        key_code = _MACOS_KEY_CODES.get(key_name)
        if key_code is not None:
            script = (
                f'tell application "System Events" to '
                f'key code {key_code}{modifier_clause}'
            )
        else:
            char = key_name.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                f'tell application "System Events" to '
                f'keystroke "{char}"{modifier_clause}'
            )

        rc, _, stderr = self._exec_in_vm_sync(
            f"osascript -e {shlex.quote(script)}"
        )
        if rc != 0:
            raise RuntimeError(f"key press failed: {stderr.strip()}")

    def _send_scroll_macos(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        """Scroll using Python+Quartz CGEvent."""
        scroll_map = {
            "up": (amount, 0),
            "down": (-amount, 0),
            "left": (0, -amount),
            "right": (0, amount),
        }
        dy, dx = scroll_map.get(direction, (-amount, 0))

        # Move mouse to position first, then scroll.
        py_script = (
            f"from Quartz.CoreGraphics import *; "
            f"p=CGPointMake({x},{y}); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, kCGMouseButtonLeft)); "
            f"e=CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, {dy}, {dx}); "
            f"CGEventPost(kCGHIDEventTap, e)"
        )
        rc, _, stderr = self._exec_in_vm_sync(
            f"python3 -c {shlex.quote(py_script)}", timeout_secs=15.0,
        )
        if rc != 0:
            raise RuntimeError(f"scroll failed: {stderr.strip()}")

    def _focus_window_macos(self, name: str) -> None:
        """Focus a window by application name using osascript."""
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "{escaped}" to activate'
        rc, _, stderr = self._exec_in_vm_sync(
            f"osascript -e {shlex.quote(script)}"
        )
        if rc != 0:
            # Fallback: search by window title via System Events.
            script2 = (
                f'tell application "System Events" to set frontmost of '
                f'(first process whose name contains "{escaped}") to true'
            )
            rc2, _, stderr2 = self._exec_in_vm_sync(
                f"osascript -e {shlex.quote(script2)}"
            )
            if rc2 != 0:
                raise RuntimeError(f"focus failed: {stderr2.strip()}")

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
        ssh_config = (
            Path(self._env["LIMA_HOME"]) / self._inst_name / "ssh.config"
        )
        if not ssh_config.is_file():
            raise RuntimeError(
                f"Cannot add port forward: Lima ssh.config not found at "
                f"{ssh_config} — is the VM running?"
            )

        host_port = allocate_host_port(requested_host_port, guest_port)
        cmd = [
            "ssh", "-F", str(ssh_config), f"lima-{self._inst_name}",
            *SSH_TUNNEL_OPTS,
            "-L", f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
        ]
        return open_ssh_tunnel(
            cmd,
            guest_port=guest_port,
            host_port=host_port,
            scope_key=scope_key,
            description=description,
            registry=self._port_forwards,
            env=self._env,
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return self._port_forwards.remove(forward_id)

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        return self._port_forwards.list(scope_key)

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        self._port_forwards.cleanup(scope_key)

    # -- SSH tunnel management (macOS guests) ---------------------------------

    def _ensure_ssh_tunnels(self) -> None:
        """Set up SSH port-forwarding tunnels for macOS guest ports.

        macOS Lima guests don't support automatic port forwarding, so
        we use an ``ssh -L`` tunnel for the Chromium CDP port (Playwright
        MCP).  The VNC port is exposed directly on the host by the
        patched ``limactl`` via ``_VZVNCServer`` and needs no tunnel.
        """
        # Check if existing tunnels are still alive.
        alive = [p for p in self._ssh_tunnels if p.poll() is None]
        if alive and len(alive) == len(self._ssh_tunnels):
            return
        self._ssh_tunnels = alive

        # Lima writes a ready-to-use ssh client config in the instance
        # directory under ``LIMA_HOME``, not in our OpenShrimp state dir.
        ssh_config = Path(self._env["LIMA_HOME"]) / self._inst_name / "ssh.config"
        if not ssh_config.is_file():
            logger.warning(
                "Cannot set up SSH tunnels for %s: %s not found",
                self._inst_name, ssh_config,
            )
            return
        ssh_target = f"lima-{self._inst_name}"

        tunnels_needed = [(9222, 9222)]

        for host_port, guest_port in tunnels_needed:
            tunnel_cmd = [
                "ssh", "-F", str(ssh_config), ssh_target,
                "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-L", f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
            ]
            try:
                proc = subprocess.Popen(
                    tunnel_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    env=self._env,
                )
                self._ssh_tunnels.append(proc)
                logger.info(
                    "SSH tunnel: localhost:%d -> guest:%d (pid %d)",
                    host_port, guest_port, proc.pid,
                )
            except Exception:
                logger.warning(
                    "Failed to start SSH tunnel for port %d", guest_port,
                    exc_info=True,
                )

    # -- Internal helpers -----------------------------------------------------

    def _rebuild_vm(self, *, log_file: Path | None = None) -> None:
        """Delete the Lima instance and recreate from scratch."""
        _log(log_file, "Deleting existing Lima instance for rebuild...")
        limactl_delete(self._limactl, self._inst_name)

        # Re-run ensure_environment to recreate.
        self.ensure_environment(log_file=log_file)
