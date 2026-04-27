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
import concurrent.futures
import getpass
import logging
import shlex
import subprocess
from pathlib import Path

# Bootstrap CoreGraphics/AppKit once for non-app processes (idempotent).
# Required so SCScreenshotManager doesn't trip CGS_REQUIRE_INIT inside CLI
# Python invocations. Skipped on non-macOS hosts where AppKit is absent.
try:
    import AppKit as _AppKit  # type: ignore[import-not-found]
    _AppKit.NSApplicationLoad()
except ImportError:
    pass

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.base import VncQuirk
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
    vnc_host_port,
)

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


def _ax_find_content_rect(
    pid: int, sc_frame: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """Return the VZ scroll-area's frame in window-local points.

    Walks the limactl process's accessibility tree, finds the AXWindow
    matching *sc_frame* (its screen-coord frame from ScreenCaptureKit),
    and returns ``(x, y, w, h)`` of its AXScrollArea child relative to
    the window's origin. Returns ``None`` if AX is unavailable or the
    expected structure isn't present.

    Requires Accessibility TCC permission for the calling process.
    """
    try:
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXValueGetValue,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
    except ImportError:
        return None

    def _ax_get(elem, attr):
        err, val = AXUIElementCopyAttributeValue(elem, attr, None)
        return val if err == 0 else None

    def _ax_frame(elem) -> tuple[float, float, float, float] | None:
        pos = _ax_get(elem, "AXPosition")
        siz = _ax_get(elem, "AXSize")
        if pos is None or siz is None:
            return None
        _, p = AXValueGetValue(pos, kAXValueCGPointType, None)
        _, s = AXValueGetValue(siz, kAXValueCGSizeType, None)
        return (p.x, p.y, s.width, s.height)

    app = AXUIElementCreateApplication(pid)
    windows = _ax_get(app, "AXWindows")
    if not windows:
        return None
    sx, sy, sw, sh = sc_frame
    for w in windows:
        wf = _ax_frame(w)
        if wf is None:
            continue
        wx, wy, ww, wh = wf
        if abs(wx - sx) > 2 or abs(wy - sy) > 2 \
                or abs(ww - sw) > 2 or abs(wh - sh) > 2:
            continue
        children = _ax_get(w, "AXChildren") or []
        for c in children:
            if _ax_get(c, "AXRole") != "AXScrollArea":
                continue
            cf = _ax_frame(c)
            if cf is None:
                return None
            cx, cy, cw, ch = cf
            return (cx - wx, cy - wy, cw, ch)
        return None
    return None


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
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._limactl = limactl_path
        self._additional_directories = additional_directories or []
        self._instance_prefix = instance_prefix
        self._computer_use = computer_use
        self._guest_os = guest_os

        self._sdir = state_dir_for(context_name)
        self._inst_name = _instance_name(context_name, instance_prefix)
        self._claude_home_dir = self._sdir / "claude-home"
        self._tmp_dir = self._sdir / "tmp"
        self._env = _lima_env()  # cached — LIMA_HOME doesn't change

        # SSH tunnel processes for macOS guest port forwarding.
        self._ssh_tunnels: list[subprocess.Popen] = []

        # Cached VNC credentials (read once from the guest).
        self._vnc_credentials_cached: tuple[str, str] | None = None

        # Cached chrome-crop rect, keyed by the Lima window's screen frame.
        # Walking the limactl AX tree costs a few IPC roundtrips per call;
        # the rect is stable until the user resizes the window.
        self._crop_cache: dict[
            tuple[float, float, float, float],
            tuple[float, float, float, float] | None,
        ] = {}

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def host_address(self) -> str:
        return "192.168.5.2"

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
            sdir,
            self._config,
            self._project_dir,
            self._additional_directories or None,
            self._computer_use,
            context_name=self._context_name,
            guest_os=self._guest_os,
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
            context_name=self._context_name,
            guest_os=self._guest_os,
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

    def provision_workspace(self) -> None:
        """Ensure Claude CLI is installed in the VM and credentials are copied."""
        ensure_claude_cli_in_vm(
            self._limactl, self._inst_name, guest_os=self._guest_os,
        )

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
            guest_os=self._guest_os,
        )
        return path, [path]

    def stop(self) -> None:
        """Stop the Lima instance and any SSH tunnels."""
        # Terminate SSH tunnels first.
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
        if self._computer_use:
            return vnc_host_port(self._context_name)
        return None

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Linux guests run wayvnc with no auth.  macOS guests run
        # Apple Screen Sharing which always requires credentials.
        if not self._computer_use or self._guest_os != "macos":
            return None
        if self._vnc_credentials_cached is not None:
            return self._vnc_credentials_cached
        rc, stdout, stderr = self._exec_in_vm_sync("cat ~/password")
        if rc != 0 or not stdout.strip():
            logger.warning(
                "Failed to read VNC password from %s: %s",
                self._inst_name, stderr.strip() or "empty",
            )
            return None
        creds = (getpass.getuser(), stdout.strip())
        self._vnc_credentials_cached = creds
        return creds

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        return frozenset()

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
            self._capture_lima_window_macos(output_path)
            return
        ts = int(output_path.stem.split("-")[-1]) if "-" in output_path.stem else 0
        guest_path = f"/tmp/screenshots/screenshot-{ts}.png"
        rc, _, stderr = self._exec_in_vm_sync(f"grim {guest_path}")
        if rc != 0:
            raise RuntimeError(f"grim failed: {stderr.strip()}")

    def _capture_lima_window_macos(self, output_path: Path) -> None:
        """Capture the Lima VM window from the host via ScreenCaptureKit.

        Avoids in-guest ``screencapture`` which fails when the guest is
        locked or the user session is unavailable. Lima's hostagent owns
        ~6 windows for a graphical VM: the VM display (e.g. 1165x780)
        plus several off-screen menubar/toolbar surfaces (e.g. 1440x30);
        we pick the largest on-screen one. The capture is cropped to
        exclude the host window's titlebar/toolbar chrome (~52pt) using
        the AX tree, so coordinates in the resulting PNG map directly
        to the guest framebuffer. Falls back to a full-window capture
        with chrome if Accessibility permission isn't granted.

        Requires Screen Recording permission for OpenShrimp.
        """
        from Foundation import NSURL  # type: ignore[import-not-found]
        from Quartz import (  # type: ignore[import-not-found]
            CGImageDestinationAddImage,
            CGImageDestinationCreateWithURL,
            CGImageDestinationFinalize,
            kCVPixelFormatType_32BGRA,
        )
        from ScreenCaptureKit import (  # type: ignore[import-not-found]
            SCContentFilter,
            SCScreenshotManager,
            SCShareableContent,
            SCStreamConfiguration,
        )

        pid = self._read_ha_pid()
        future: concurrent.futures.Future = concurrent.futures.Future()

        def on_image(image, error):
            if error is not None or image is None:
                future.set_exception(RuntimeError(f"capture failed: {error}"))
            else:
                future.set_result(image)

        def on_content(content, error):
            if error is not None:
                future.set_exception(
                    RuntimeError(f"shareable content failed: {error}"))
                return
            best = None
            best_area = 0
            for w in content.windows():
                app = w.owningApplication()
                if app is None or app.processID() != pid:
                    continue
                frame = w.frame()
                area = int(frame.size.width * frame.size.height)
                if area < 100_000:  # skip menubar/toolbar surfaces
                    continue
                if area > best_area:
                    best_area, best = area, w
            if best is None:
                future.set_exception(RuntimeError(
                    f"no on-screen Lima window for hostagent pid={pid}; "
                    "is the VM running with video.display=vz?"))
                return

            sc_frame = (
                best.frame().origin.x, best.frame().origin.y,
                best.frame().size.width, best.frame().size.height,
            )
            crop = self._crop_for_window(pid, sc_frame)
            if crop is not None:
                cx, cy, cw, ch = crop
            else:
                cx, cy, cw, ch = 0.0, 0.0, sc_frame[2], sc_frame[3]

            filt = SCContentFilter.alloc().initWithDesktopIndependentWindow_(best)
            cfg = SCStreamConfiguration.alloc().init()
            cfg.setSourceRect_(((cx, cy), (cw, ch)))
            cfg.setWidth_(int(cw * 2))
            cfg.setHeight_(int(ch * 2))
            cfg.setShowsCursor_(False)
            cfg.setPixelFormat_(kCVPixelFormatType_32BGRA)
            SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                filt, cfg, on_image)

        SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            False, True, on_content)

        try:
            image = future.result(timeout=10.0)
        except concurrent.futures.TimeoutError as e:
            raise RuntimeError("ScreenCaptureKit timed out after 10s") from e

        url = NSURL.fileURLWithPath_(str(output_path))
        dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        if dest is None:
            raise RuntimeError(
                f"CGImageDestinationCreateWithURL failed for {output_path}")
        CGImageDestinationAddImage(dest, image, None)
        if not CGImageDestinationFinalize(dest):
            raise RuntimeError(
                f"CGImageDestinationFinalize failed for {output_path}")

    def _crop_for_window(
        self, pid: int, sc_frame: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float] | None:
        if sc_frame in self._crop_cache:
            return self._crop_cache[sc_frame]
        crop = _ax_find_content_rect(pid, sc_frame)
        if crop is None:
            logger.warning(
                "AX content rect unavailable for pid=%d; capturing full "
                "window with chrome — grant Accessibility permission to "
                "remove the ~52pt offset.", pid,
            )
        self._crop_cache[sc_frame] = crop
        return crop

    def _read_ha_pid(self) -> int:
        pid_file = Path(self._env["LIMA_HOME"]) / self._inst_name / "ha.pid"
        try:
            return int(pid_file.read_text().strip())
        except (OSError, ValueError) as e:
            raise RuntimeError(f"cannot read {pid_file}: {e}") from e

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

    # -- SSH tunnel management (macOS guests) ---------------------------------

    def _ensure_ssh_tunnels(self) -> None:
        """Set up SSH port-forwarding tunnels for macOS guest ports.

        macOS Lima guests don't support automatic port forwarding, so
        we use ``ssh -L`` tunnels for VNC and CDP ports.
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

        host_vnc_port = vnc_host_port(self._context_name)
        tunnels_needed = [(host_vnc_port, 5900), (9222, 9222)]

        for host_port, guest_port in tunnels_needed:
            # Check if this tunnel is already running.
            already_tunneled = any(
                p.poll() is None
                for p in alive
                # Can't easily check which port a Popen maps to, so just
                # check total count — if we lost any, restart all missing.
            )
            if already_tunneled and len(alive) >= len(tunnels_needed):
                continue

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
