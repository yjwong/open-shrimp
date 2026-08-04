"""Windows Host Compute Service (HCS) sandbox backend.

Boots a Linux guest as an HCS compute system on native Windows, shares the
workspace over 9p, runs the agent CLI inside a chroot on a SCSI-attached ext4
rootfs VHDX, and reaches it over an ``AF_HYPERV`` control channel.

Implements the :class:`~open_shrimp.sandbox.base.Sandbox` protocol.  The
platform-neutral helpers (config JSON, path/label mapping, the launcher
source) live in :mod:`open_shrimp.sandbox.hcs_helpers`; the ``win32more``
plumbing lives in :mod:`open_shrimp.sandbox.hcs_win`; the guest-side exec
server is :mod:`open_shrimp.sandbox.hcs_exec_agent`.  The Claude CLI is
installed into the rootfs image from npm in-guest (see
``open_shrimp.backend.claude_sdk.hcs_install``) — the sandbox layer never
names an agent.

Lifecycle invariants:

* ``persistent_paths`` are flat dynamic VHDX disks on ``Devices.Scsi``,
  formatted + mounted in-guest by ext4 label; they survive every rebuild and
  die only in ``destroy_context``.
* There is no checkpoint/save path; the VM simply stays warm between
  sessions, and a rebuild is terminate + recreate.
* Teardown flushes the guest (``sync`` + unmount) before
  ``HcsTerminateComputeSystem`` so the ext4 journals close cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.agent_runtime import (
    AgentHandle,
    AgentRuntime,
    WrappedCLI,
)
from open_shrimp.sandbox.base import PortForward, VncQuirk
from open_shrimp.sandbox import hcs_helpers as H

if TYPE_CHECKING:
    from open_shrimp.sandbox.hcs_win import ControlChannel

logger = logging.getLogger(__name__)

# The WSL-shipped bzImage carries hv_vsock, hv_storvsc, hv_netvsc and 9p, all
# the guest needs.  Overridable for hosts that stage it elsewhere.
_DEFAULT_KERNEL = r"C:\Program Files\WSL\tools\kernel"
_DEFAULT_INITRD = r"C:\ProgramData\openshrimp\hcs\initrd.img"


def _kernel_path() -> Path:
    return Path(os.environ.get("OPENSHRIMP_HCS_KERNEL", _DEFAULT_KERNEL))


def _initrd_path() -> Path:
    """The control-agent initramfs (busybox + the static vsock agent that
    mounts shares and starts the exec server).  Built once in WSL and staged
    by the operator; located via ``OPENSHRIMP_HCS_INITRD`` when not at the
    default."""
    return Path(os.environ.get("OPENSHRIMP_HCS_INITRD", _DEFAULT_INITRD))


class HcsSandbox:
    """One HCS compute system for a single context."""

    def __init__(
        self,
        context_name: str,
        config: SandboxConfig,
        project_dir: str,
        *,
        state_dir: Path,
        additional_directories: list[str] | None = None,
        instance_prefix: str = "openshrimp",
        runtime: AgentRuntime | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "The HCS sandbox backend runs only on Windows "
                f"(host platform is {sys.platform!r})."
            )
        if sys.version_info < (3, 12):
            raise RuntimeError(
                "The HCS sandbox backend requires Python 3.12+ for hvsocket "
                "support (socket.AF_HYPERV); the interpreter is "
                f"{sys.version_info.major}.{sys.version_info.minor}."
            )
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._additional_directories = additional_directories or []
        self._instance_prefix = instance_prefix
        self._runtime = runtime

        # The guest sees drive-relative POSIX paths (C:\a\b -> /a/b), and the
        # approval layer maps a guest path back with os.path.realpath, which on
        # Windows resolves against the process's current drive.  That mapping is
        # only injective — and the boundary check only sound — when every shared
        # directory is on that one drive.  Refuse anything off it rather than
        # silently mis-scope the boundary.
        cwd_drive = PureWindowsPath(os.getcwd()).drive.upper()
        for label, d in [("directory", project_dir),
                         *[("additional_directories", a)
                           for a in self._additional_directories]]:
            drive = PureWindowsPath(d).drive.upper()
            if drive != cwd_drive:
                raise RuntimeError(
                    f"HCS sandbox context {label} {d!r} is on drive {drive!r}, "
                    f"but the process runs on {cwd_drive!r}. All sandbox "
                    "directories must share the process drive so the approval "
                    "boundary maps guest paths correctly."
                )

        self._sdir = state_dir
        self._claude_home_dir = self._sdir / "claude-home"
        self._cfg_dir = self._sdir / "cfg"
        self._tmp_dir = self._sdir / "tmp"
        self._rootfs_vhdx = self._sdir / "rootfs.vhdx"

        # Deterministic identity per context.
        self._system_id = f"{instance_prefix}-{context_name}"
        self._owner = f"{instance_prefix}-hcs"
        self._net_guid = self._stable_guid("net")
        self._ep_guid = self._stable_guid("ep")

        bundle = runtime.image_bundle if runtime else None
        self._task_tmp_prefix = bundle.task_tmp_prefix if bundle else "claude"
        self._hcs_install = getattr(bundle, "hcs_install", None) if bundle else None

        # Live handles, populated by ensure_running.
        self._runtime_id: str | None = None
        self._ep_props: dict | None = None
        self._forwarded_ports: set[int] = set()

    # -- identity helpers -----------------------------------------------------

    def _stable_guid(self, kind: str) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"openshrimp-hcs/{self._instance_prefix}/{self._context_name}/{kind}",
        ))

    def _runtime_id_file(self) -> Path:
        return self._sdir / "runtime_id"

    def _launch_json_file(self) -> Path:
        return self._sdir / "launch.json"

    def _launcher_exe(self) -> Path:
        return self._sdir / "cli-launcher.exe"

    # -- Sandbox protocol: identity ------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def container_name(self) -> str | None:
        return None

    @property
    def host_address(self) -> str:
        """Loopback — the guest reaches host services through the guest→host
        relay, which listens on the guest's own ``127.0.0.1`` and bridges to
        the host's ``127.0.0.1`` over hvsocket.

        The HNS NAT gateway forwards guest egress to the internet but not
        inbound to host loopback, so the gateway address is unusable for
        reaching the MCP proxy; the relay makes loopback the right answer.
        """
        return "127.0.0.1"

    # -- share plan -----------------------------------------------------------

    def _guest_workspace(self) -> str:
        return H.windows_to_guest_path(self._project_dir)

    def _p9_shares(self) -> list[tuple[str, str, int, int]]:
        """``(name, host_path, port, flags)`` per Plan9 share, in the order the
        SCSI-agnostic mount pass consumes them."""
        shares: list[tuple[str, str, int, int]] = [
            ("ws", self._project_dir, H.P9_PORT_WORKSPACE, 0),
            ("home", str(self._claude_home_dir), H.P9_PORT_HOME, 0),
            ("cfg", str(self._cfg_dir), H.P9_PORT_CFG, 0),
            ("tasktmp", str(self._tmp_dir), H.P9_PORT_TASK_TMP, 0),
        ]
        for i, extra in enumerate(self._additional_directories):
            shares.append(
                (f"add{i}", extra, H.P9_PORT_EXTRA_BASE + i, 0)
            )
        return shares

    def _persistent_paths(self) -> list[str]:
        return list(self._config.persistent_paths)

    def _scsi_disks(self) -> list[str]:
        """VHDX host paths in SCSI-LUN order: rootfs first, then one dynamic
        VHDX per persistent path."""
        disks = [str(self._rootfs_vhdx)]
        for guest_path in self._persistent_paths():
            disks.append(str(self._sdir / H.persistent_vol_filename(guest_path)))
        return disks

    # -- Sandbox protocol: environment ---------------------------------------

    def environment_ready(self) -> bool:
        return (
            self._rootfs_vhdx.exists()
            and self._initrd_ok()
            and (self._sdir / "config.sha256").exists()
        )

    def _initrd_ok(self) -> bool:
        return _initrd_path().exists() and _kernel_path().exists()

    def _fingerprint(self) -> str:
        return H.config_fingerprint(
            kernel_path=_kernel_path(),
            initrd_path=_initrd_path(),
            base_image=self._config.base_image,
            project_dir=self._project_dir,
            additional_directories=self._additional_directories,
            persistent_paths=self._persistent_paths(),
            memory_mb=self._config.memory,
            cpus=self._config.cpus,
            provision=self._config.provision,
        )

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        from open_shrimp.sandbox import hcs_win as W

        self._sdir.mkdir(parents=True, exist_ok=True)
        for d in (self._claude_home_dir, self._cfg_dir, self._tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

        if not _kernel_path().exists():
            raise RuntimeError(
                f"HCS kernel not found at {_kernel_path()} — install WSL "
                "(the kernel ships with it) or set OPENSHRIMP_HCS_KERNEL."
            )
        if not _initrd_path().exists():
            raise RuntimeError(
                f"HCS control initramfs not found at {_initrd_path()} — "
                "build it in WSL and stage it, or set OPENSHRIMP_HCS_INITRD."
            )

        desired_fp = self._fingerprint()
        saved_fp = self._load_fingerprint()
        if saved_fp is not None and saved_fp != desired_fp:
            self._log(log_file, "HCS config changed — rebuilding sandbox...")
            self._rebuild(log_file=log_file)
            return

        # Seed the per-context rootfs from the base image (once, or when the
        # base image drifts).
        self._ensure_rootfs(log_file=log_file)

        # Create any missing persistent-volume VHDX (never re-create existing;
        # data survives rebuilds).
        for guest_path in self._persistent_paths():
            vol = self._sdir / H.persistent_vol_filename(guest_path)
            if not vol.exists():
                self._log(
                    log_file, f"Creating persistent volume for {guest_path}...",
                )
                W.create_dynamic_vhdx(str(vol), self._config.disk_size)

        # Stage the guest-side exec agent + provision script into the cfg
        # share so the guest can pick them up over 9p (no rootfs rebuild).
        self._stage_cfg_share()

        self._save_fingerprint(desired_fp)
        self._log(log_file, "HCS sandbox environment ready.")

    def _ensure_rootfs(self, *, log_file: Path | None) -> None:
        base = self._config.base_image
        if not base:
            raise RuntimeError(
                "HCS sandbox requires 'base_image' (the rootfs VHDX with the "
                "guest userland + Node + agent CLI) in the context config."
            )
        base_path = Path(base)
        if not base_path.exists():
            raise RuntimeError(f"HCS base_image not found: {base_path}")

        want = H.rootfs_fingerprint(base)
        marker = self._sdir / "rootfs.base.sha256"
        have = marker.read_text().strip() if marker.exists() else None
        if self._rootfs_vhdx.exists() and have == want:
            return

        self._log(log_file, "Seeding per-context rootfs from base image...")
        tmp = self._rootfs_vhdx.with_suffix(".vhdx.tmp")
        shutil.copyfile(base_path, tmp)
        os.replace(tmp, self._rootfs_vhdx)
        marker.write_text(want)

    def _stage_cfg_share(self) -> None:
        exec_src = Path(__file__).with_name("hcs_exec_agent.py")
        shutil.copyfile(exec_src, self._cfg_dir / "exec_agent.py")
        relay_src = Path(__file__).with_name("hcs_host_relay.py")
        shutil.copyfile(relay_src, self._cfg_dir / "host_relay.py")
        provision = self._config.provision
        (self._cfg_dir / "provision.sh").write_text(
            provision or "", encoding="utf-8", newline="\n",
        )

    # -- Sandbox protocol: running -------------------------------------------

    def running(self) -> bool:
        from open_shrimp.sandbox import hcs_win as W

        rid = self._read_runtime_id()
        if rid is None:
            return False
        try:
            op = W.HcsOperation()
        except W.HcsError:
            return False
        try:
            entries = W.enumerate_compute_systems(self._owner, op)
        except W.HcsError:
            return False
        finally:
            op.close()
        for e in entries:
            if e.get("Id") == self._system_id and e.get("State") == "Running":
                self._runtime_id = e.get("RuntimeId")
                return W.vsock_port_reachable(self._runtime_id, H.EXEC_PORT)
        return False

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        from open_shrimp.sandbox import hcs_win as W

        if self.running():
            return

        # Reattach if the compute system is up but our exec agent isn't yet.
        rid = self._live_runtime_id()
        if rid is not None:
            self._runtime_id = rid
            self._log(log_file, "Reattaching to running HCS sandbox...")
            self._reattach_guest(log_file=log_file)
            return

        self._boot(log_file=log_file)

    def _boot(self, *, log_file: Path | None) -> None:
        from open_shrimp.sandbox import hcs_win as W

        self._log(log_file, "Booting HCS sandbox...")
        self._forwarded_ports.clear()  # a fresh VM has no guest relay running
        self._teardown_stale()

        subnet = H.pick_subnet(W.hcn_network_prefixes())
        W.hcn_create_network(
            self._net_guid, H.compose_network_settings(
                f"{self._system_id}-nat", subnet,
            ),
        )
        W.hcn_create_endpoint(
            self._net_guid, self._ep_guid,
            H.compose_endpoint_settings(f"{self._system_id}-ep", self._net_guid),
        )
        props = W.hcn_endpoint_properties(self._ep_guid)
        if not props or not props.get("IPAddress") or not props.get("MacAddress"):
            raise RuntimeError("HCN endpoint has no IP/MAC after create")
        # Cache the allocation — a later re-query can transiently omit the IP.
        self._ep_props = props

        pipe_name = rf"\\.\pipe\openshrimp-hcs-{uuid.uuid4().hex[:8]}"
        console = W.ConsoleReader(pipe_name)
        op = W.HcsOperation()
        system = None
        try:
            config = H.compose_vm_config(
                owner=self._owner,
                kernel_path=str(_kernel_path()),
                initrd_path=str(_initrd_path()),
                memory_mb=self._config.memory,
                cpus=self._config.cpus,
                console_pipe=pipe_name,
                endpoint_guid=self._ep_guid,
                endpoint_mac=props["MacAddress"],
                p9_shares=self._p9_shares(),
                scsi_disks=self._scsi_disks(),
                connect_sddl=W.hvsocket_connect_sddl(),
            )
            system = W.create_compute_system(
                self._system_id, json.dumps(config), op,
            )
            # The VM worker's virtual SID needs access to each VHDX before it
            # can power on the disk attachment (profile-restricted dirs deny
            # it otherwise).
            for disk in self._scsi_disks():
                W.grant_vm_access(self._system_id, disk)
            W.start_compute_system(system, op)
            if not console.wait_for(H.AGENT_MARKER, timeout=120.0):
                raise RuntimeError(
                    "HCS guest control agent never came up "
                    f"(console: {bytes(console.buf)[-400:]!r})"
                )
            entries = W.enumerate_compute_systems(self._owner, op)
            self._runtime_id = next(
                (e.get("RuntimeId") for e in entries
                 if e.get("Id") == self._system_id), None,
            )
            if not self._runtime_id:
                raise RuntimeError("no RuntimeId after boot")
            self._write_runtime_id(self._runtime_id)
        finally:
            if system is not None:
                system.close()
            op.close()
            console.stop()

        self._provision_guest(log_file=log_file)

    def _provision_guest(self, *, log_file: Path | None) -> None:
        """Mount shares + rootfs, bind the workspace/home/cfg/tmp into the
        chroot, mount persistent volumes by label, and start the exec agent."""
        from open_shrimp.sandbox import hcs_win as W

        assert self._runtime_id is not None
        chan = W.ControlChannel(self._runtime_id, H.CONTROL_PORT)

        def ctl(cmd: str, *, expect: str | None = None, **kw) -> str:
            ok, out = chan.run(cmd, **kw)
            if not ok or (expect is not None and expect not in out):
                raise RuntimeError(f"guest command failed: {cmd!r}\n{out}")
            return out

        # 1. Mount the 9p shares (initramfs agent's @mount).
        opts = "version=9p2000.L,msize=262144"
        for name, mnt, port in (
            ("ws", H.MNT_WORKSPACE, H.P9_PORT_WORKSPACE),
            ("home", H.MNT_HOME, H.P9_PORT_HOME),
            ("cfg", H.MNT_CFG, H.P9_PORT_CFG),
            ("tasktmp", H.MNT_TASK_TMP, H.P9_PORT_TASK_TMP),
        ):
            ctl(f"@mount {port} {name} {mnt} {opts}", expect="MOUNT-OK")
        for i, extra in enumerate(self._additional_directories):
            ctl(f"mkdir -p /mnt/add{i}")
            ctl(f"@mount {H.P9_PORT_EXTRA_BASE + i} add{i} /mnt/add{i} {opts}",
                expect="MOUNT-OK")

        # 2. Mount the rootfs VHDX by ext4 label, and proc/sys/dev inside it.
        ctl(
            f"dev=$(labelfind {H.ROOTFS_LABEL}); echo DEV=$dev; "
            f"mount -t ext4 \"$dev\" {H.MNT_ROOT} && echo ROOT-MOUNT-OK; "
            f"mount -t proc proc {H.MNT_ROOT}/proc; "
            f"mount -t sysfs sysfs {H.MNT_ROOT}/sys; "
            f"mount -t devtmpfs dev {H.MNT_ROOT}/dev",
            expect="ROOT-MOUNT-OK",
        )

        # 3. Bind the shares into the chroot at their guest paths.
        ws = self._guest_workspace()
        binds = [
            (H.MNT_WORKSPACE, f"{H.MNT_ROOT}{ws}"),
            (H.MNT_HOME, f"{H.MNT_ROOT}/root/.claude"),
            (H.MNT_CFG, f"{H.MNT_ROOT}{H.CHROOT_CFG_DIR}"),
            (H.MNT_TASK_TMP, f"{H.MNT_ROOT}/tmp/{self._task_tmp_prefix}-0"),
        ]
        for i, extra in enumerate(self._additional_directories):
            binds.append(
                (f"/mnt/add{i}", f"{H.MNT_ROOT}{H.windows_to_guest_path(extra)}")
            )
        # Guest paths derive from user config (project dir, additional dirs),
        # which routinely contain spaces on Windows; every interpolated path is
        # shell-quoted so a space or quote cannot split the command or break out.
        for src, dst in binds:
            ctl(f"mkdir -p {shlex.quote(dst)} && "
                f"mount -o bind {shlex.quote(src)} {shlex.quote(dst)}")

        # 4. Persistent volumes: format-if-blank, mount by label — via the
        #    rootfs's own e2fsprogs (the control initramfs busybox has no
        #    mkfs.ext4/blkid), so this runs inside the chroot.  A blank disk
        #    is formatted at its deterministic LUN device; a formatted one is
        #    resolved by label, so attach-order drift cannot corrupt data.
        for idx, guest_path in enumerate(self._persistent_paths()):
            dev = f"/dev/{H.persistent_dev_name(idx)}"
            label = H.persistent_vol_label(guest_path)
            # The guest path is passed as a positional arg ($1) to the inner
            # chroot sh, never spliced into the single-quoted script — and
            # shell-quoted for the agent's outer sh.  label/dev are safe
            # (hex/fixed).  A blank disk is formatted at its LUN device; a
            # formatted one resolves by label, so attach-order drift is inert.
            inner = (
                f"d=$(blkid -L {label} 2>/dev/null || true); "
                f"if [ -z \"$d\" ]; then mkfs.ext4 -q -L {label} {dev}; d={dev}; fi; "
                'mkdir -p "$1" && mount -t ext4 "$d" "$1" && echo PV-OK'
            )
            ctl(
                f"chroot {H.MNT_ROOT} /usr/bin/env PATH={H.CHROOT_PATH} "
                f"sh -c {shlex.quote(inner)} _ {shlex.quote(guest_path)}",
                expect="PV-OK", read_timeout=120.0,
            )

        # 5. Run the provision script inside the chroot when configured.
        if self._config.provision:
            ctl(
                f"sh -c 'chroot {H.MNT_ROOT} /usr/bin/env "
                f"HOME=/root PATH={H.CHROOT_PATH} "
                f"sh {H.CHROOT_CFG_DIR}/provision.sh; echo PROVISION-DONE'",
                expect="PROVISION-DONE", read_timeout=600.0,
            )

        # 6. Start the exec agent inside the chroot (against the guest's own
        #    network namespace, so it can bind vsock).
        self._start_exec_agent(chan)

        # 7. Push the static network config from the endpoint properties.
        self._configure_network(chan)

    def _start_exec_agent(self, chan: "ControlChannel") -> None:
        """(Re)start the in-chroot vsock exec server and wait for it to bind.

        Split out so the reattach path can restart just the exec agent without
        re-running the one-shot mount sequence.
        """
        from open_shrimp.sandbox import hcs_win as W

        chan.run(
            f"chroot {H.MNT_ROOT} /usr/bin/env HOME=/root "
            f"PATH={H.CHROOT_PATH} python3 {H.CHROOT_CFG_DIR}/exec_agent.py "
            f"{H.EXEC_PORT} </dev/null >/tmp/exec-agent.log 2>&1 &",
            tolerate_read_timeout=True, read_timeout=4.0,
        )
        for _ in range(40):
            if W.vsock_port_reachable(self._runtime_id, H.EXEC_PORT):
                return
            time.sleep(0.25)
        raise RuntimeError("exec agent never bound its vsock port")

    def _reattach_guest(self, *, log_file: Path | None) -> None:
        """Recover a Running compute system whose exec agent died.

        The compute system stays on its original boot (``StopOnReset`` turns a
        guest reset into a stop), so the shares and rootfs are still mounted —
        only the exec agent needs restarting.  If that fails the guest is
        unhealthy, so fall back to a clean reboot.
        """
        from open_shrimp.sandbox import hcs_win as W

        chan = W.ControlChannel(self._runtime_id, H.CONTROL_PORT)
        try:
            self._start_exec_agent(chan)
        except RuntimeError:
            self._log(log_file, "Reattach failed; rebooting HCS sandbox...")
            self._boot(log_file=log_file)

    def _configure_network(self, chan: "ControlChannel") -> None:
        props = self._ep_props or self._endpoint_props()
        if not props or not props.get("IPAddress"):
            logger.warning("no endpoint IP; guest egress will be unavailable")
            return
        ip = props["IPAddress"]
        prefix = props.get("PrefixLength") or 24
        gw = props.get("GatewayAddress")
        dns = [d.strip() for d in (props.get("DNSServerList") or "").split(",")
               if d.strip()] or ["1.1.1.1"]
        chan.run("ip link set lo up")
        chan.run("ip link set eth0 up")
        chan.run(f"ip addr add {ip}/{prefix} dev eth0")
        if gw:
            chan.run(f"ip route add default via {gw}")
        resolv = "\\n".join(f"nameserver {d}" for d in dns)
        # resolv.conf must be visible inside the chroot too.  busybox here has
        # no `tee`; write once and copy (cp is in the initramfs applet set).
        chan.run(
            f"sh -c 'printf \"{resolv}\\n\" > /etc/resolv.conf; "
            f"cp /etc/resolv.conf {H.MNT_ROOT}/etc/resolv.conf'"
        )

    # -- Sandbox protocol: provisioning + launch -----------------------------

    def provision_workspace(self, *, log_file: Path | None = None) -> None:
        """Install the agent CLI into the rootfs (first build) and sync
        credentials into the host-side agent home."""
        if self._runtime is None:
            return
        if self._hcs_install is not None:
            self._hcs_install(self)
        if self._runtime.provision_credentials is not None:
            self._runtime.provision_credentials(self._claude_home_dir)

    def ensure_host_port_forward(self, host_port: int) -> None:
        """Make host ``127.0.0.1:host_port`` reachable from the guest.

        Starts the process-wide host relay (once) and a guest-side relay
        listener on the same loopback port, so the agent CLI can reach the
        OpenShrimp MCP proxy.  Idempotent per port; a no-op once forwarded.
        """
        if host_port in self._forwarded_ports or self._runtime_id is None:
            return
        from open_shrimp.sandbox import hcs_win as W

        W.allow_relay_port(host_port)
        W.ensure_host_relay(self._runtime_id)
        chan = W.ControlChannel(self._runtime_id, H.CONTROL_PORT)
        chan.run(
            f"sh -c 'chroot {H.MNT_ROOT} /usr/bin/env PATH={H.CHROOT_PATH} "
            f"python3 {H.CHROOT_CFG_DIR}/host_relay.py {host_port} "
            f"</dev/null >/tmp/host-relay-{host_port}.log 2>&1 &'",
            tolerate_read_timeout=True, read_timeout=4.0,
        )
        self._forwarded_ports.add(host_port)

    def guest_exec(
        self, argv: list[str], *, read_timeout: float = 120.0,
    ) -> tuple[int, str]:
        """Run *argv* inside the rootfs chroot over the exec channel.

        Used by the in-guest CLI installer (``hcs_install``) so it shares the
        exact exec path the launcher uses.  Returns ``(exit_code, output)``.
        """
        from open_shrimp.sandbox import hcs_exec_client as C

        assert self._runtime_id is not None
        return C.run(
            self._runtime_id, H.EXEC_PORT, argv,
            cwd="/", env={"HOME": "/root", "PATH": H.CHROOT_PATH},
            read_timeout=read_timeout,
        )

    def start_agent(self, runtime: AgentRuntime) -> AgentHandle:
        if isinstance(runtime.launch, WrappedCLI):
            cli_path, cleanup = self.build_cli_wrapper()
            return AgentHandle(cli_path=cli_path, cleanup_paths=cleanup)
        raise NotImplementedError(
            f"HCS backend supports only the wrapped-CLI launch; got "
            f"{runtime.launch!r}"
        )

    def build_cli_wrapper(self) -> tuple[str, list[str]]:
        """Write the per-context launch config and compile the launcher exe.

        The launcher is a compiled ``.exe`` (never a ``.cmd``): it ships argv
        as a structured JSON list over hvsocket to the in-guest exec agent,
        which ``exec``\\ s it with no shell.  Built once and reused while the
        source is unchanged.
        """
        env: dict[str, str] = {"HOME": "/root", "PATH": H.CHROOT_PATH}
        for git_key, env_vars in (
            ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
            ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
        ):
            try:
                value = subprocess.check_output(
                    ["git", "config", "--global", git_key], text=True,
                ).strip()
            except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                value = ""
            if value:
                for var in env_vars:
                    env[var] = value

        launch_cfg = {
            "runtime_id_file": str(self._runtime_id_file()),
            "port": H.EXEC_PORT,
            "cwd": self._guest_workspace(),
            "env": env,
            "env_passthrough": ["ANTHROPIC_API_KEY"],
            "argv_prefix": ["claude"],
            "connect_timeout_s": 30.0,
        }
        self._launch_json_file().write_text(
            json.dumps(launch_cfg), encoding="utf-8",
        )
        exe = self._build_launcher_exe()
        # The launcher is per-context durable state; the launch json is too.
        # Neither is a session temp, so cleanup_paths stays empty.
        return str(exe), []

    def _build_launcher_exe(self) -> Path:
        source = H.render_launcher_source(str(self._launch_json_file()))
        cs_path = self._sdir / "cli-launcher.cs"
        exe = self._launcher_exe()
        if (
            exe.exists()
            and cs_path.exists()
            and cs_path.read_text(encoding="utf-8") == source
        ):
            return exe
        cs_path.write_text(source, encoding="utf-8")
        csc = _find_csc()
        result = subprocess.run(
            [
                csc, "/nologo", "/optimize+", "/target:exe",
                "/reference:System.Web.Extensions.dll",
                f"/out:{exe}", str(cs_path),
            ],
            capture_output=True, text=True,
        )
        if not exe.exists():
            raise RuntimeError(
                "csc failed to build the HCS launcher: "
                f"{(result.stdout + result.stderr).strip()}"
            )
        return exe

    def reach(self, guest_port: int) -> str:
        raise NotImplementedError(
            "The HCS backend has no served-endpoint / reach() support."
        )

    # -- Sandbox protocol: teardown ------------------------------------------

    def stop(self) -> None:
        from open_shrimp.sandbox import hcs_win as W

        rid = self._runtime_id or self._read_runtime_id()
        if rid is not None:
            # Flush before ForcedExit so persistent-volume ext4 journals close
            # cleanly.  busybox `umount` has no -R, so the persistent mounts
            # are unmounted by name; a global sync covers the rest.  The
            # rootfs is reborn every boot, so only these journals matter.
            chan = W.ControlChannel(rid, H.CONTROL_PORT)
            umounts = " ".join(
                f"umount {shlex.quote(H.MNT_ROOT + gp)} 2>/dev/null;"
                for gp in self._persistent_paths()
            )
            chan.run(
                f"sync; {umounts} sync; echo FLUSH-OK",
                read_timeout=30.0,
            )

        try:
            op = W.HcsOperation()
        except W.HcsError:
            op = None
        if op is not None:
            try:
                for e in W.enumerate_compute_systems(self._owner, op):
                    if e.get("Id") != self._system_id:
                        continue
                    try:
                        system = W.open_compute_system(self._system_id)
                    except W.HcsError:
                        break
                    try:
                        W.terminate_compute_system(system, op)
                    except W.HcsError as exc:
                        logger.warning("HCS terminate failed: %s", exc)
                    finally:
                        system.close()
                    break
            finally:
                op.close()

        if rid is not None:
            W.close_host_relay(rid)
        self._runtime_id = None
        self._forwarded_ports.clear()
        self._runtime_id_file().unlink(missing_ok=True)

    def destroy(self) -> None:
        """Full teardown: stop, then delete the HCN endpoint + network.

        Persistent-volume VHDX deletion is the manager's ``destroy_context``
        job (it removes the whole state dir); this only releases the network
        objects so ``hnsdiag`` returns to its pre-run baseline.
        """
        from open_shrimp.sandbox import hcs_win as W

        try:
            self.stop()
        finally:
            W.hcn_delete_endpoint(self._ep_guid)
            W.hcn_delete_network(self._net_guid)

    def _teardown_stale(self) -> None:
        """Terminate any leftover compute system and delete stale HCN objects
        for this context before a fresh boot (pre-enumeration hygiene)."""
        from open_shrimp.sandbox import hcs_win as W

        try:
            op = W.HcsOperation()
        except W.HcsError:
            op = None
        if op is not None:
            try:
                for e in W.enumerate_compute_systems(self._owner, op):
                    if e.get("Id") != self._system_id:
                        continue
                    try:
                        system = W.open_compute_system(self._system_id)
                        W.terminate_compute_system(system, op)
                        system.close()
                    except W.HcsError:
                        pass
            finally:
                op.close()
        W.hcn_delete_endpoint(self._ep_guid)
        W.hcn_delete_network(self._net_guid)
        self._runtime_id_file().unlink(missing_ok=True)

    def _rebuild(self, *, log_file: Path | None) -> None:
        """Terminate + delete network objects, preserve persistent volumes and
        the rootfs, then re-run ensure_environment."""
        (self._sdir / "config.sha256").unlink(missing_ok=True)
        self._teardown_stale()
        self.ensure_environment(log_file=log_file)

    # -- runtime-id state -----------------------------------------------------

    def _write_runtime_id(self, rid: str) -> None:
        self._runtime_id_file().write_text(rid, encoding="ascii")

    def _read_runtime_id(self) -> str | None:
        try:
            return self._runtime_id_file().read_text(encoding="ascii").strip()
        except OSError:
            return None

    def _live_runtime_id(self) -> str | None:
        from open_shrimp.sandbox import hcs_win as W

        try:
            op = W.HcsOperation()
        except W.HcsError:
            return None
        try:
            for e in W.enumerate_compute_systems(self._owner, op):
                if e.get("Id") == self._system_id and e.get("State") == "Running":
                    return e.get("RuntimeId")
        except W.HcsError:
            return None
        finally:
            op.close()
        return None

    def _endpoint_props(self) -> dict | None:
        try:
            from open_shrimp.sandbox import hcs_win as W
        except Exception:
            return None
        try:
            return W.hcn_endpoint_properties(self._ep_guid)
        except Exception:
            return None

    # -- fingerprint io -------------------------------------------------------

    def _load_fingerprint(self) -> str | None:
        p = self._sdir / "config.sha256"
        try:
            return p.read_text().strip()
        except OSError:
            return None

    def _save_fingerprint(self, fp: str) -> None:
        (self._sdir / "config.sha256").write_text(fp)

    # -- misc -----------------------------------------------------------------

    @staticmethod
    def _log(log_file: Path | None, msg: str) -> None:
        logger.info(msg)
        if log_file is not None:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                pass

    # -- unsupported capabilities --------------------------------------------

    def get_screenshots_dir(self) -> Path | None:
        return None

    def get_vnc_port(self) -> int | None:
        return None

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        return frozenset()

    def get_text_input_state_path(self) -> Path | None:
        return None

    def get_text_input_active(self) -> bool:
        return False

    def take_screenshot(self, output_path: Path) -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def send_type(self, text: str) -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def send_key(self, key_str: str) -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def send_scroll(self, x: int, y: int, direction: str, amount: int = 3) -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def focus_window(self, name: str) -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def get_clipboard(self) -> str:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def set_clipboard(self, text: str) -> None:
        raise NotImplementedError("Computer use is not supported on the HCS backend.")

    def start_security_key_helper(
        self, *, relay_url: str, session_id: str, token: str,
    ) -> None:
        raise NotImplementedError(
            "Security-key forwarding is not supported on the HCS backend."
        )

    def ensure_phone_running(self) -> None:
        raise NotImplementedError("Phone use is not supported on the HCS backend.")

    def phone_shell(self, cmd: str) -> str:
        raise NotImplementedError("Phone use is not supported on the HCS backend.")

    def phone_screenshot(self, output_path: Path) -> None:
        raise NotImplementedError("Phone use is not supported on the HCS backend.")

    def phone_install_apk(self, apk_path: str) -> str:
        raise NotImplementedError("Phone use is not supported on the HCS backend.")

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files into the sandbox via the workspace share.

        Files are staged under a ``.openshrimp-uploads`` dir in the workspace
        (already 9p-shared into the guest), so the guest path is derived from
        the guest workspace mount.
        """
        if not host_paths:
            return []
        uploads = Path(self._project_dir) / ".openshrimp-uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        ws = self._guest_workspace()
        result: list[Path] = []
        for host_path in host_paths:
            try:
                dest = uploads / host_path.name
                shutil.copyfile(host_path, dest)
                # The guest is Linux: return a POSIX path so its string form
                # keeps forward slashes when spliced into the agent's prompt
                # (a WindowsPath would stringify with backslashes).
                result.append(
                    PurePosixPath(ws) / ".openshrimp-uploads" / host_path.name
                )
            except OSError:
                logger.warning("failed to stage upload %s", host_path, exc_info=True)
                result.append(host_path)
        return result

    # -- port forwarding (dynamic guest→host forwards not yet supported) -----

    def supports_port_forwarding(self) -> bool:
        return False

    def add_port_forward(
        self, guest_port: int, requested_host_port: int | None,
        scope_key: str | None, description: str | None,
    ) -> PortForward:
        raise NotImplementedError(
            "Runtime port forwarding is not yet supported on the HCS backend."
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return False

    def list_port_forwards(self, scope_key: str | None = None) -> list[PortForward]:
        return []

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        return None


def _find_csc() -> str:
    """Locate the in-box .NET Framework C# compiler (no toolchain install)."""
    candidates = [
        r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ]
    override = os.environ.get("OPENSHRIMP_HCS_CSC")
    if override:
        candidates.insert(0, override)
    for c in candidates:
        if Path(c).exists():
            return c
    raise RuntimeError(
        "csc.exe (in-box .NET Framework compiler) not found — expected under "
        r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319."
    )
