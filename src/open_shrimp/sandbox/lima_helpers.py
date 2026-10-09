"""Lima VM helper functions.

Handles Lima binary management (auto-download), YAML template generation,
limactl CLI wrappers, config fingerprinting, and CLI wrapper script
generation.  All limactl invocations use ``LIMA_HOME`` to isolate
OpenShrimp's VMs from the user's personal Lima instances.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
from collections.abc import Collection
from pathlib import Path
from typing import Callable

import yaml
from open_shrimp.config import SandboxConfig
from open_shrimp.paths import data_dir as _data_dir, get_instance_name as _get_instance_name
from open_shrimp.sandbox.agent_runtime import GuestMount
from open_shrimp.sandbox.prefetch import ProgressFn, content_length

logger = logging.getLogger(__name__)


def _read_credentials_json() -> str | None:
    """Read Claude Code credentials from the macOS Keychain.

    Returns the raw JSON string, or ``None`` if unavailable.
    """
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-a",
                getpass.getuser(),
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info("Read credentials from macOS Keychain")
            return result.stdout.strip()
    except Exception:
        logger.debug("Failed to read credentials from macOS Keychain", exc_info=True)
    return None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIMA_VERSION = "2.1.1"


def bin_dir() -> Path:
    """Directory the managed ``limactl`` and its siblings are extracted to.

    Public because a caller sizing up the download has to know which
    filesystem it will land on."""
    return _data_dir() / "bin"


def _lima_state_dir() -> Path:
    """Return the ``LIMA_HOME`` directory, scoped by instance name when set.

    Lima creates Unix sockets under LIMA_HOME/<instance>/ssh.sock.* which
    must stay below UNIX_PATH_MAX (104 on macOS).  The platformdirs data
    path (~/Library/Application Support/...) is too long, so we use a short
    path under $HOME instead.
    """
    name = _get_instance_name()
    if name:
        return Path.home() / ".openshrimp" / f"lima-{name}"
    return Path.home() / ".openshrimp" / "lima"

def _download_base() -> str:
    """Return the GitHub release base URL to download the lima tarball from.

    OpenShrimp ships a custom-built ``limactl`` patched to attach Apple's
    private ``_VZVNCServer`` SPI to the running ``VZVirtualMachine``,
    enabling a host-side VNC server that doesn't require the ``limactl``
    GUI window to be open. The patched binary is attached to the
    OpenShrimp GitHub release this code shipped in, so a given install
    always pulls the ``limactl`` built against the ``LIMA_VERSION`` it
    pins. ``release.yaml`` lints the ``LIMA_VERSION`` ↔ ``patches/PIN``
    agreement to keep the runtime constant and the build pin coupled.

    Falls back to upstream Lima if the install version can't be
    determined — works in dev, but the patched ``_VZVNCServer`` path
    will be inert there.
    """
    from open_shrimp.updater import _REPO, get_current_version

    version = get_current_version()
    if version != "0.0.0":
        return f"https://github.com/{_REPO}/releases/download/v{version}"
    return f"https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}"

_DOWNLOAD_MAP: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): f"lima-{LIMA_VERSION}-Darwin-arm64.tar.gz",
    ("Darwin", "x86_64"): f"lima-{LIMA_VERSION}-Darwin-x86_64.tar.gz",
}

# Ubuntu 24.04 LTS cloud images.
_CLOUD_IMAGES: dict[str, str] = {
    "aarch64": (
        "https://cloud-images.ubuntu.com/releases/24.04/release/"
        "ubuntu-24.04-server-cloudimg-arm64.img"
    ),
    "x86_64": (
        "https://cloud-images.ubuntu.com/releases/24.04/release/"
        "ubuntu-24.04-server-cloudimg-amd64.img"
    ),
}

# The uid Lima's guest user gets, and with it the ``/tmp/<prefix>-<uid>``
# directory an agent CLI writes its background-task output to.  Distinct from
# ``skill_paths.SANDBOX_UID``, which is the ``openshrimp`` user baked into the
# libvirt and HCS guest images; Lima builds its own user and only happens to
# land on the same number.
LIMA_GUEST_UID = 1000

# ---------------------------------------------------------------------------
# Lima binary management (following tunnel.py pattern)
# ---------------------------------------------------------------------------


def limactl_downloadable() -> bool:
    """Whether ``ensure_limactl_sync`` has a build to fetch for this platform.

    False is the only state a missing ``limactl`` cannot be recovered from,
    which is what lets a prerequisite check pass on the strength of the
    download without ever fetching anything itself.
    """
    return (platform.system(), platform.machine()) in _DOWNLOAD_MAP


def find_limactl() -> str | None:
    """Find limactl: check managed bin dir first, then ``$PATH``.

    Public because a prerequisite check has to ask the same question this
    package answers for itself — ``shutil.which`` alone reports a limactl
    this project downloaded as missing, because nothing puts the managed bin
    directory on ``$PATH``.
    """
    local_bin = bin_dir() / "limactl"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    path = shutil.which("limactl")
    if path:
        return path

    return None


def _download_lima_sync(*, progress: ProgressFn | None = None) -> str:
    """Download and extract the Lima release tarball (sync).

    Lima tarballs contain a ``bin/`` subdirectory with ``limactl``,
    ``lima``, etc.  All binaries are extracted to ``bin_dir()``.

    *progress* is called per chunk with the bytes transferred so far and the
    tarball's ``Content-Length``, or ``None`` where the server sent none.  It
    covers the transfer only: the extraction that follows is local and fast.

    Returns the path to the ``limactl`` binary.
    """
    system = platform.system()
    machine = platform.machine()
    tarball_name = _DOWNLOAD_MAP.get((system, machine))
    if tarball_name is None:
        raise RuntimeError(
            f"Unsupported platform for Lima auto-download: "
            f"{system} {machine}. Please install Lima manually: "
            f"brew install lima"
        )

    target_dir = bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    url = f"{_download_base()}/{tarball_name}"
    logger.info("Downloading Lima %s from %s ...", LIMA_VERSION, url)

    import httpx
    import tarfile

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = content_length(resp.headers.get("content-length"))
                done = 0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)

        # Lima expects share/lima/ (guest agents, templates) relative to
        # the install prefix.
        prefix_dir = target_dir.parent
        with tarfile.open(tmp_path, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                if not member.isfile():
                    continue
                if name.startswith("bin/"):
                    dest = target_dir / os.path.basename(name)
                elif name.startswith(("share/", "libexec/")):
                    dest = prefix_dir / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                else:
                    continue
                f = tar.extractfile(member)
                if f is not None:
                    with open(dest, "wb") as out:
                        out.write(f.read())
                    dest.chmod(
                        dest.stat().st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )
                    logger.debug("Extracted %s to %s", member.name, dest)
    finally:
        os.unlink(tmp_path)

    target = target_dir / "limactl"
    if not target.is_file():
        raise RuntimeError("limactl not found in downloaded Lima archive")

    logger.info("Lima %s downloaded to %s", LIMA_VERSION, target_dir)
    return str(target)


def ensure_limactl_sync(*, progress: ProgressFn | None = None) -> str:
    """Ensure limactl is available, downloading if necessary (sync).

    *progress* is only ever called when a download actually happens; a
    limactl already on disk returns without reporting a byte.

    Returns the path to the limactl binary.
    """
    path = find_limactl()
    if path:
        logger.info("Found limactl at %s", path)
        return path

    logger.info("limactl not found, attempting auto-download...")
    return _download_lima_sync(progress=progress)


# ---------------------------------------------------------------------------
# State directory helpers
# ---------------------------------------------------------------------------


def state_dir_for(context_name: str) -> Path:
    """Return per-context state dir (separate from LIMA_HOME).

    This must NOT live under ``_lima_state_dir()`` because Lima treats
    any subdirectory there with a ``lima.yaml`` as an instance.
    """
    return _data_dir() / "lima-state" / context_name


def lima_guest_home(guest_os: str = "linux") -> str:
    """The home directory of the user ``limactl shell`` lands in.

    Lima creates the guest user as ``<host user>.guest``, under ``/home`` in a
    Linux guest and ``/Users`` in a macOS one.  ``getpass.getuser()``, not
    ``os.getlogin()`` — the latter returns "root" under launchd (the macOS
    ``.app``), naming a home no mount lands at.
    """
    root = "/Users" if guest_os == "macos" else "/home"
    return f"{root}/{getpass.getuser()}.guest"


def vnc_host_port(context_name: str) -> int:
    """Return a deterministic VNC host port for a context.

    Lima does not support ``hostPort: 0`` for auto-assignment, so we
    derive a unique port from the context name to avoid collisions when
    multiple computer-use VMs run concurrently.  Uses the range
    49152–65535 (dynamic/private ports per IANA).
    """
    h = int.from_bytes(hashlib.sha256(context_name.encode()).digest())
    return 49152 + (h % (65536 - 49152))


def instance_name(context_name: str, instance_prefix: str = "openshrimp") -> str:
    """Return sanitised Lima instance name.

    Lima instance names must match ``^[a-zA-Z][a-zA-Z0-9_.-]*$``.

    The prefix is intentionally omitted from the name because LIMA_HOME
    already isolates our instances, and the extra length can push Unix
    socket paths past the 104-char UNIX_PATH_MAX limit.
    """
    raw = context_name
    # Replace invalid characters with hyphens.
    sanitised = re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)
    # Ensure it starts with a letter.
    if sanitised and not sanitised[0].isalpha():
        sanitised = "i-" + sanitised
    return sanitised


def _lima_env() -> dict[str, str]:
    """Return environment dict with ``LIMA_HOME`` set for isolation."""
    env = os.environ.copy()
    env["LIMA_HOME"] = str(_lima_state_dir())
    return env


# ---------------------------------------------------------------------------
# Lima YAML template generation
# ---------------------------------------------------------------------------


def lima_template(
    sdir: Path,
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None = None,
    computer_use: bool = False,
    *,
    context_name: str = "",
    agent_home_mounts: "tuple[tuple[str, str], ...]" = (),
    served_home_mounts: "tuple[GuestMount, ...]" = (),
    task_tmp_guest_path: str,
) -> dict:
    """The Lima instance template for a Linux guest, as a dict.

    One body behind the YAML that gets written, the fingerprint that detects
    drift, and the mount set a remount rewrites — three readers of one
    rendering, so a field added to the template cannot go missing from the
    fingerprint and rebuild the VM on every call.
    """
    mounts = _build_mounts(
        sdir, project_dir, additional_directories, computer_use,
        task_tmp_guest_path=task_tmp_guest_path,
        context_name=context_name,
        agent_home_mounts=agent_home_mounts,
        served_home_mounts=served_home_mounts,
    )
    provision = _build_provision_scripts(config, computer_use)

    template: dict = {
        "vmType": "vz",
        "vmOpts": {
            "vz": {"rosetta": {"enabled": True, "binfmt": True}},
        },
        "cpus": config.cpus,
        "memory": f"{config.memory}MiB",
        "disk": f"{config.disk_size}GiB",
        "images": [
            {"location": url, "arch": arch}
            for arch, url in _CLOUD_IMAGES.items()
        ],
        "mountType": "virtiofs",
        "mounts": mounts,
        "provision": provision,
        "containerd": {"system": False, "user": False},
        "ssh": {"forwardAgent": True},
    }

    port_forward: list[dict] = []
    if computer_use:
        # VNC server (wayvnc on guest port 5900).
        port_forward.append({
            "guestPort": 5900,
            "hostPort": vnc_host_port(context_name or sdir.name),
            "hostIP": "127.0.0.1",
        })
        # Chromium CDP debugging port for Playwright MCP.
        port_forward.append({
            "guestPort": 9222,
            "hostIP": "127.0.0.1",
        })
    if port_forward:
        template["portForwards"] = port_forward

    return template


def write_lima_yaml(sdir: Path, template: dict) -> Path:
    """Write *template* to ``sdir/lima.yaml`` and return the path."""
    sdir.mkdir(parents=True, exist_ok=True)
    yaml_path = sdir / "lima.yaml"
    yaml_path.write_text(
        yaml.dump(template, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("Generated Lima YAML template at %s", yaml_path)
    return yaml_path


def _build_mounts(
    sdir: Path,
    project_dir: str,
    additional_directories: list[str] | None,
    computer_use: bool = False,
    *,
    context_name: str = "",
    agent_home_mounts: "tuple[tuple[str, str], ...]" = (),
    served_home_mounts: "tuple[GuestMount, ...]" = (),
    task_tmp_guest_path: str,
) -> list[dict]:
    """Build Lima mount entries.

    *agent_home_mounts* is ``(host dir, guest path)`` per wrapped-CLI runtime's
    agent home — the dir the CLI reads its credentials and writes its session
    corpus in.

    Each :class:`GuestMount` in *served_home_mounts* is appended as a virtiofs
    mount so the runtime's ``inject``-written host dirs (provider
    ``auth.json``, plugin config) reach the served process in the guest.

    Lima keys its mount list by ``location`` and merges repeats, so one host
    dir gets one guest path: *task_tmp_guest_path* is the single path the task
    output share lands at, and a guest hosting a second agent that writes
    somewhere else reaches it through a symlink
    (:meth:`LimaSandbox._link_task_tmp_aliases`).
    """
    mounts = []

    # Project directory (writable).
    mounts.append({"location": project_dir, "writable": True})

    # Additional directories.
    for d in additional_directories or []:
        mounts.append({"location": d, "writable": True})

    # Each wrapped-CLI runtime's agent home, shared into the VM at the path it
    # resolves from the guest user's own home.
    vm_home = lima_guest_home()
    for host_dir, guest_path in agent_home_mounts:
        Path(host_dir).mkdir(parents=True, exist_ok=True)
        mounts.append({
            "location": host_dir,
            "mountPoint": guest_path,
            "writable": True,
        })

    host_skills = Path.home() / ".claude" / "skills"
    if host_skills.is_dir():
        mounts.append({
            "location": str(host_skills),
            "mountPoint": f"{vm_home}/.claude/skills",
            "writable": False,
        })

    # Host-side tmp directory (for task output files).  Must mount at the path
    # the agent CLI writes its background-task output to (Claude →
    # /tmp/claude-<uid>), or the host terminal mini app can't read it.
    tmp_dir = str(sdir / "tmp")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": tmp_dir,
        "mountPoint": task_tmp_guest_path,
        "writable": True,
    })

    if computer_use:
        # Screenshots directory — grim writes here, host reads for Telegram.
        screenshots_dir = str(sdir / "screenshots")
        Path(screenshots_dir).mkdir(parents=True, exist_ok=True)
        mounts.append({
            "location": screenshots_dir,
            "mountPoint": "/tmp/screenshots",
            "writable": True,
        })

    # Served-endpoint launch: each declared mount is a host dir the runtime's
    # ``inject`` writes into (provider ``auth.json``, managed plugin config),
    # synced into the guest so the served process (which runs under its own
    # ``HOME``) sees them.  The wrapped-CLI launch contributes nothing here.
    for mount in served_home_mounts:
        mounts.append({
            "location": str(mount.host_dir),
            "mountPoint": mount.guest_mount_point,
            "writable": mount.writable,
        })

    return mounts


def _build_provision_scripts(
    config: SandboxConfig,
    computer_use: bool = False,
) -> list[dict]:
    """Build Lima provision script entries."""
    scripts = []

    # Base system setup.
    base_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # Create claude user if not exists.
        id claude &>/dev/null || useradd -m -s /bin/bash -G sudo claude
        echo 'claude ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/claude

        # AcceptEnv for API key forwarding via SSH.
        printf 'AcceptEnv ANTHROPIC_API_KEY\\n' > /etc/ssh/sshd_config.d/openshrimp.conf
        systemctl restart ssh

        # Enable fstrim for disk space reclamation.
        systemctl enable --now fstrim.timer
    """)
    scripts.append({"mode": "system", "script": base_script})

    # User-provided provision script.
    if config.provision:
        scripts.append({"mode": "system", "script": config.provision})

    if computer_use:
        scripts.extend(_build_computer_use_provisions())

    return scripts


def _build_computer_use_provisions() -> list[dict]:
    """Build Lima provision entries for the computer-use desktop stack.

    Installs a headless Wayland compositor (labwc), input injection
    (wlrctl), screenshot capture (grim), VNC server (wayvnc), Google
    Chrome, and systemd user units to auto-start everything.
    """
    provisions: list[dict] = []

    # --- System provision: install packages ---
    install_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # Wayland compositor + tools.
        apt-get update
        apt-get install -y --no-install-recommends \\
            labwc \\
            grim \\
            wayvnc \\
            wl-clipboard \\
            foot \\
            fonts-liberation \\
            fonts-noto-color-emoji \\
            fonts-noto \\
            dbus-x11 \\
            procps

        # Build wlrctl from source (not packaged for arm64).
        apt-get install -y --no-install-recommends \\
            gcc libc6-dev git pkg-config meson ninja-build \\
            libwayland-dev libxkbcommon-dev wayland-protocols
        git clone https://git.sr.ht/~brocellous/wlrctl /tmp/wlrctl
        meson setup --prefix=/usr/local /tmp/wlrctl/build /tmp/wlrctl
        ninja -C /tmp/wlrctl/build install
        rm -rf /tmp/wlrctl

        # Node.js for Playwright MCP.
        curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
        apt-get install -y --no-install-recommends nodejs

        # Install browser: Google Chrome on amd64, Chromium from apt on arm64.
        if [ "$(dpkg --print-architecture)" = "amd64" ]; then
            wget -q -O /tmp/google-chrome.deb \
                'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb'
            apt-get install -y /tmp/google-chrome.deb
            rm /tmp/google-chrome.deb
        else
            apt-get install -y chromium-browser
        fi

        rm -rf /var/lib/apt/lists/*

        # Install Claude Code and Playwright MCP globally.
        npm install -g --cache /tmp/npm-cache \\
            @anthropic-ai/claude-code@latest \\
            @playwright/mcp
        rm -rf /tmp/npm-cache

        # Enable linger so user services start on boot without login.
        LIMA_USER=$(getent passwd 1000 | cut -d: -f1)
        loginctl enable-linger "$LIMA_USER"
    """)
    provisions.append({"mode": "system", "script": install_script})

    # --- User provision: download browsers, write configs ---
    user_setup_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # labwc config.
        mkdir -p ~/.config/labwc
        cat > ~/.config/labwc/rc.xml << 'RCXML'
        <?xml version="1.0" encoding="UTF-8"?>
        <labwc_config>
          <core><gap>0</gap></core>
          <theme>
            <name></name>
            <titlebar><height>20</height></titlebar>
            <font name="sans" size="10" />
          </theme>
          <keyboard />
          <mouse />
        </labwc_config>
        RCXML

        # Empty autostart — services handle application startup.
        echo '# Applications started via systemd user units.' > ~/.config/labwc/autostart

        # --- systemd user units ---
        mkdir -p ~/.config/systemd/user

        cat > ~/.config/systemd/user/openshrimp-labwc.service << 'UNIT'
        [Unit]
        Description=labwc Wayland compositor (headless)

        [Service]
        Type=simple
        Environment=WLR_BACKENDS=headless
        Environment=WLR_RENDERER=pixman
        Environment=WLR_HEADLESS_OUTPUTS=1
        Environment=WAYLAND_DISPLAY=wayland-0
        ExecStart=/usr/bin/labwc
        Restart=on-failure
        RestartSec=2

        [Install]
        WantedBy=default.target
        UNIT

        cat > ~/.config/systemd/user/openshrimp-wayvnc.service << 'UNIT'
        [Unit]
        Description=wayvnc VNC server
        After=openshrimp-labwc.service
        Requires=openshrimp-labwc.service

        [Service]
        Type=simple
        Environment=WAYLAND_DISPLAY=wayland-0
        ExecStartPre=/bin/bash -c 'for i in $(seq 1 75); do [ -S "$XDG_RUNTIME_DIR/wayland-0" ] && break; sleep 0.2; done'
        ExecStart=/usr/bin/wayvnc --output=HEADLESS-1 0.0.0.0 5900
        Restart=on-failure
        RestartSec=2

        [Install]
        WantedBy=default.target
        UNIT

        # Browser systemd unit: Google Chrome on amd64, Chromium on arm64.
        if command -v google-chrome >/dev/null 2>&1; then
            BROWSER_BIN=/usr/bin/google-chrome
            BROWSER_NAME="Google Chrome"
        else
            BROWSER_BIN=/usr/bin/chromium-browser
            BROWSER_NAME="Chromium"
        fi
        cat > ~/.config/systemd/user/openshrimp-chromium.service << UNIT
        [Unit]
        Description=${BROWSER_NAME} browser
        After=openshrimp-labwc.service
        Requires=openshrimp-labwc.service

        [Service]
        Type=simple
        Environment=WAYLAND_DISPLAY=wayland-0
        ExecStartPre=/bin/bash -c 'for i in \\$(seq 1 75); do [ -S "\\$XDG_RUNTIME_DIR/wayland-0" ] && break; sleep 0.2; done'
        ExecStart=${BROWSER_BIN} --no-first-run --no-default-browser-check --disable-background-networking --disable-default-apps --ozone-platform=wayland --user-data-dir=%h/.config/google-chrome-debug --remote-debugging-port=9222 --window-size=1280,720
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
        UNIT

        # Enable all units.
        systemctl --user daemon-reload
        systemctl --user enable openshrimp-labwc.service
        systemctl --user enable openshrimp-wayvnc.service
        systemctl --user enable openshrimp-chromium.service

        # Start services now (VM is booting for the first time).
        systemctl --user start openshrimp-labwc.service
        systemctl --user start openshrimp-wayvnc.service
        systemctl --user start openshrimp-chromium.service
    """)
    provisions.append({"mode": "user", "script": user_setup_script})

    return provisions


# ---------------------------------------------------------------------------
# Config fingerprinting (drift detection)
# ---------------------------------------------------------------------------


def config_fingerprint(
    template: dict, *, applied_in_place: Collection[str] = (),
) -> str:
    """Hash *template* without its ``mounts:`` block or *applied_in_place*.

    The mount set is left out because nothing persisted here could say which
    runtimes the *running* instance was booted with: a process that starts
    against a guest built for both agents and dispatches one of them would
    read a hash of its own half-sized plan as drift.  The instance's own
    ``lima.yaml`` already records the shares it carries, so mounts are
    reconciled against that file (:func:`instance_mounts`).

    *applied_in_place* names the sizing fields the caller settles against
    ``limactl list`` and ``limactl edit`` instead.  Every other field —
    provision scripts, images, port forwards — is hashed here, where a change
    means the VM is deleted and built again.
    """
    skipped = {"mounts", *applied_in_place}
    body = {key: value for key, value in template.items() if key not in skipped}
    content = yaml.dump(body, default_flow_style=False, sort_keys=False)
    return hashlib.sha256(content.encode()).hexdigest()


def save_config_fingerprint(sdir: Path, fingerprint: str) -> None:
    """Persist the config fingerprint for drift detection."""
    (sdir / "config.sha256").write_text(fingerprint + "\n", encoding="utf-8")


def load_config_fingerprint(sdir: Path) -> str | None:
    """Load the saved fingerprint, or ``None`` if absent or unreadable."""
    fp_file = sdir / "config.sha256"
    if not fp_file.exists():
        return None
    words = fp_file.read_text(encoding="utf-8").split()
    return words[0] if len(words) == 1 else None


def clear_config_fingerprint(sdir: Path) -> None:
    """Drop the saved fingerprint, so the next call rebuilds."""
    (sdir / "config.sha256").unlink(missing_ok=True)


def guest_mount_point(entry: dict) -> str | None:
    """The guest-side path a mount entry lands at, or ``None`` when it lands
    at its own host path.

    A share mounted at its own host path is one of the context's directories,
    which is what the approval layer treats as the sandbox boundary.  Every
    other share is an agent's machinery under a guest home — its data dir, its
    plugin config, its task output.

    The two are told apart by ``mountPoint`` rather than by where the host dir
    lives, because Lima fills a missing ``mountPoint`` in with the location and
    saves the filled config back to the instance: a context directory reaches
    this function spelled both ways.
    """
    point = entry.get("mountPoint")
    return point if point and point != entry["location"] else None


def mount_location(entry: dict) -> str:
    """The host dir a mount entry shares, through ``realpath``.

    Lima re-saves the instance config with its own spelling of each path, and
    a spelling difference read as drift would restart the VM every time the
    mount set is reconciled.
    """
    return os.path.realpath(entry["location"])


def mount_key(entry: dict) -> tuple[str, str, bool]:
    """A mount entry as ``(host dir, guest path, writable)``.

    The guest path is left verbatim — it names a directory in the guest, which
    the host cannot resolve.
    """
    location = mount_location(entry)
    return (
        location,
        guest_mount_point(entry) or location,
        bool(entry.get("writable")),
    )


# ---------------------------------------------------------------------------
# limactl CLI wrappers
# ---------------------------------------------------------------------------


def _log(log_file: Path | None, msg: str) -> None:
    """Append a line to the build log file (for terminal mini app)."""
    if log_file is not None:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.flush()


def _run_limactl(
    limactl: str,
    args: list[str],
    *,
    log_file: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a limactl command with ``LIMA_HOME`` set."""
    cmd = [limactl, *args]
    env = _lima_env()

    if log_file is not None and not capture_output:
        # Stream output to log file.
        with open(log_file, "a", encoding="utf-8") as f:
            result = subprocess.run(
                cmd,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                check=check,
                timeout=timeout,
            )
        return result

    return subprocess.run(
        cmd,
        env=env,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        check=check,
        timeout=timeout,
    )


def limactl_create(
    limactl: str,
    name: str,
    template_path: Path,
    *,
    log_file: Path | None = None,
) -> None:
    """Create a Lima instance from a YAML template."""
    _log(log_file, f"Creating Lima instance '{name}'...")
    _run_limactl(
        limactl,
        ["create", f"--name={name}", "--tty=false", str(template_path)],
        log_file=log_file,
        capture_output=False,
        timeout=600,
    )
    logger.info("Created Lima instance %s", name)


def limactl_start(
    limactl: str,
    name: str,
    *,
    log_file: Path | None = None,
) -> None:
    """Start a Lima instance."""
    _log(log_file, f"Starting Lima instance '{name}'...")
    _run_limactl(
        limactl,
        ["start", name],
        log_file=log_file,
        capture_output=False,
        timeout=300,
    )
    logger.info("Started Lima instance %s", name)


def limactl_stop(limactl: str, name: str, *, force: bool = False) -> None:
    """Stop a Lima instance.

    A graceful stop asks the guest to shut down over SSH; *force* kills the
    VM and host agent instead, the only stop that works on a guest whose SSH
    no longer answers.
    """
    args = ["stop", "--force", name] if force else ["stop", name]
    _run_limactl(limactl, args, check=False, timeout=120)
    logger.info("Stopped Lima instance %s", name)


def limactl_delete(limactl: str, name: str) -> None:
    """Delete a Lima instance."""
    _run_limactl(
        limactl, ["delete", "--force", name], check=False, timeout=60,
    )
    logger.info("Deleted Lima instance %s", name)


def instance_mounts(inst_name: str) -> list[dict] | None:
    """The shares ``LIMA_HOME/<instance>/lima.yaml`` gives the guest.

    This file is what Lima reads when it starts the VM, so it is the record of
    which runtimes the instance was booted for — the one thing a process that
    did not do the booting has no other way to learn.  ``None`` means there is
    no instance config to read, which leaves the caller a fresh create.
    """
    config = _lima_state_dir() / inst_name / "lima.yaml"
    try:
        instance = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError):
        logger.warning(
            "Could not read the mount set of Lima instance %s", inst_name,
            exc_info=True,
        )
        return None
    mounts = instance.get("mounts")
    if not isinstance(mounts, list):
        return None
    return [m for m in mounts if isinstance(m, dict) and m.get("location")]


def rewrite_instance_mounts(inst_name: str, mounts: list[dict]) -> None:
    """Replace the ``mounts:`` block of an existing instance's config.

    Lima reads ``LIMA_HOME/<instance>/lima.yaml`` when it starts the VM and
    fixes the mount set there — it has no hot-add — so a guest that gains a
    second agent runtime takes the new shares through a stop and a start.
    Only the mount list is replaced: everything ``limactl create`` resolved
    into that file (the picked image, the ssh port) stays, which is what makes
    this cheaper than deleting the instance and building it again.

    Raises if the instance config is missing, which leaves the caller its
    rebuild.
    """
    config = _lima_state_dir() / inst_name / "lima.yaml"
    instance = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    instance["mounts"] = mounts
    config.write_text(
        yaml.dump(instance, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("Rewrote the mount set of Lima instance %s", inst_name)


def limactl_list_json(limactl: str) -> list[dict]:
    """Return parsed JSON from ``limactl list --json``."""
    result = _run_limactl(limactl, ["list", "--json"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    # Lima outputs one JSON object per line (JSONL).
    instances = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                instances.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return instances


def limactl_instance(limactl: str, name: str) -> dict | None:
    """The ``limactl list --json`` entry for *name*, or ``None``.

    Lima reports ``cpus`` as a count and ``memory``/``disk`` in bytes, already
    resolved from whatever spelling the instance config uses.
    """
    for inst in limactl_list_json(limactl):
        if inst.get("name") == name:
            return inst
    return None


def limactl_instance_status(limactl: str, name: str) -> str | None:
    """Return instance status (``Running``, ``Stopped``, etc.) or ``None``."""
    inst = limactl_instance(limactl, name)
    return inst.get("status") if inst is not None else None


def limactl_edit(limactl: str, name: str, fields: dict[str, int | str]) -> None:
    """Set top-level *fields* in a stopped instance's config.

    ``limactl edit`` refuses a running instance and validates the result,
    rejecting a smaller ``disk`` among other things.  A grown ``disk`` is
    applied to the instance's diff disk by the next ``limactl start``, and the
    guest's cloud-init ``growpart`` extends the root filesystem on that boot.
    """
    expr = " | ".join(
        f".{key} = {json.dumps(value)}" for key, value in fields.items()
    )
    _run_limactl(
        limactl, ["edit", "--tty=false", "--set", expr, name], timeout=60,
    )
    logger.info("Edited Lima instance %s: %s", name, expr)


def limactl_shell_check(limactl: str, name: str) -> bool:
    """Quick liveness check: ``limactl shell <name> -- true``.

    A probe that outlives its 10s timeout counts as unresponsive: a stale SSH
    ControlMaster socket (left behind when the host sleeps) makes
    ``limactl shell`` hang rather than fail.
    """
    try:
        result = _run_limactl(
            limactl, ["shell", name, "--", "true"], check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("limactl shell %s timed out after 10s", name)
        return False
    return result.returncode == 0




# ---------------------------------------------------------------------------
# Per-backend CLI binary provisioning for Linux guests
# ---------------------------------------------------------------------------


def install_cli_in_linux_vm(
    limactl: str,
    inst_name: str,
    binary_name: str,
    *,
    install_cmd_for: Callable[[str], str],
    expected_version: str | None = None,
    timeout: int = 300,
) -> None:
    """Install a binary into ``/usr/local/bin/<binary_name>`` inside a Linux Lima VM.

    Combines the "already installed" probe with ``uname -m`` into one
    ``limactl shell`` round-trip, then runs the bash command
    *install_cmd_for* builds for the guest's architecture
    (``install_cmd_for(arch_str)`` → ``str``; *arch_str* is ``"x64"`` or
    ``"arm64"``).  The caller composes the whole command because everything
    that varies with the asset — the URL, its version, its checksum — varies
    together, and threading them through here separately only gave three
    parameters two callers each had to discard half of.

    It is called only when an install is actually needed, so a caller whose
    version costs a subprocess to resolve pays nothing on the common path.

    *expected_version* is the version the guest must already report for the
    install to be skipped, for a caller whose version is pinned: without it a
    guest provisioned before a bump keeps its old binary until somebody deletes
    it by hand.  A caller that leaves it unset gets the presence-only probe,
    which is all a caller whose version merely follows the host's can ask.
    """
    # Three fields always, each echoed rather than run bare, so the reply is
    # three lines whatever the guest has: a probe whose line count varies with
    # what is installed cannot be indexed.  ``true`` stands in for the version
    # when nobody asked for one.
    binary = shlex.quote(binary_name)
    version_probe = (
        f"{binary} --version 2>/dev/null | head -n1"
        if expected_version is not None
        else "true"
    )
    probe = _run_limactl(
        limactl,
        [
            "shell", inst_name, "--", "bash", "-c",
            "; ".join(
                f'echo "$({field})"'
                for field in (f"command -v {binary}", version_probe, "uname -m")
            ),
        ],
        check=False,
        timeout=10,
    )
    lines = probe.stdout.rstrip("\n").split("\n")
    if len(lines) != 3:
        raise RuntimeError(
            f"Failed to probe Lima VM {inst_name} for {binary_name}"
        )
    installed_at, reported, guest_arch = (line.strip() for line in lines)

    if installed_at:
        guest_version = version_token(reported)
        if expected_version is None or guest_version == expected_version:
            logger.info("%s already installed in VM %s", binary_name, inst_name)
            return
        logger.info(
            "VM %s has %s %s, replacing it with %s",
            inst_name, binary_name, guest_version or "an unknown version",
            expected_version,
        )

    if guest_arch == "aarch64":
        arch_str = "arm64"
    elif guest_arch == "x86_64":
        arch_str = "x64"
    else:
        raise RuntimeError(f"Unsupported guest architecture: {guest_arch}")

    logger.info(
        "Installing %s (linux-%s) into VM %s...", binary_name, arch_str, inst_name,
    )
    _run_limactl(
        limactl,
        ["shell", inst_name, "--", "bash", "-c", install_cmd_for(arch_str)],
        check=True,
        timeout=timeout,
    )
    logger.info("%s installed in VM %s", binary_name, inst_name)


def version_token(line: str) -> str | None:
    """The version out of a ``--version`` line, or ``None`` when there is none.

    CLIs pad the number with their own name and parenthetical build details;
    the number is always the first token, sometimes with a ``v`` on it.
    """
    parts = line.strip().split()
    return parts[0].lstrip("v") if parts else None


# ---------------------------------------------------------------------------
# CLI wrapper script generation
# ---------------------------------------------------------------------------


def build_cli_wrapper(
    context_name: str,
    sdir: Path,
    limactl_path: str,
    project_dir: str,
    inst_name: str,
    *,
    argv0: str,
    guest_os: str = "linux",
) -> str:
    """Generate a bash wrapper that uses ``limactl shell`` to run an agent CLI.

    *argv0* is the CLI's own name on the guest ``PATH``, off the runtime's
    image bundle.  Host-side credentials reach the guest through the runtime's
    ``provision_credentials`` hook, which ``provision_workspace`` runs into
    that runtime's shared agent home before every dispatch.

    Returns the absolute path to the generated wrapper script.
    """
    if guest_os == "macos":
        from open_shrimp.sandbox.lima_macos_helpers import build_cli_wrapper_macos
        return build_cli_wrapper_macos(
            context_name, sdir, limactl_path, project_dir,
            inst_name, argv0=argv0,
        )

    # Git identity — read from host and export in the remote shell.
    git_env_parts: list[str] = []
    for git_key, env_vars in [
        ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
        ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
    ]:
        try:
            value = subprocess.check_output(
                ["git", "config", "--global", git_key],
                text=True,
            ).strip()
            if value:
                for env_var in env_vars:
                    git_env_parts.append(
                        f"export {env_var}={shlex.quote(value)}"
                    )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    # Forward ANTHROPIC_API_KEY only if set in the host environment.
    api_key_export = ""
    if os.environ.get("ANTHROPIC_API_KEY"):
        api_key_export = " && export ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"

    git_env_export = ""
    if git_env_parts:
        git_env_export = " && " + " && ".join(git_env_parts)

    script = textwrap.dedent(f"""\
        #!/bin/bash
        set -euo pipefail

        LIMACTL={shlex.quote(limactl_path)}
        INSTANCE_NAME={shlex.quote(inst_name)}
        LIMA_HOME={shlex.quote(str(_lima_state_dir()))}
        export LIMA_HOME

        # Self-heal: check if instance is running, start if needed.
        # All pre-flight commands redirect stdin from /dev/null to avoid
        # consuming the SDK's JSON stream on our stdin.
        STATUS=$("$LIMACTL" list --json 2>/dev/null </dev/null | \
            python3 -c "
        import json, sys
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                inst = json.loads(line)
            except json.JSONDecodeError:
                continue
            if inst.get('name') == '$INSTANCE_NAME':
                print(inst.get('status', ''))
                break
        " 2>/dev/null || echo "")

        if [ "$STATUS" != "Running" ]; then
            "$LIMACTL" start "$INSTANCE_NAME" </dev/null 2>/dev/null || true
            for i in $(seq 1 60); do
                "$LIMACTL" shell "$INSTANCE_NAME" -- true </dev/null 2>/dev/null && break
                sleep 1
            done
        fi

    """) + textwrap.dedent(f"""\

        # Build remote command with proper shell-escaping.
        # Source /etc/profile for full PATH (needed for npx / Playwright MCP).
        REMOTE_CMD=". /etc/profile{api_key_export}{git_env_export} && cd {shlex.quote(project_dir)} && {shlex.quote(argv0)}"
        for arg in "$@"; do
            REMOTE_CMD+=" $(printf '%q' "$arg")"
        done

        exec "$LIMACTL" shell "$INSTANCE_NAME" -- bash -c "$REMOTE_CMD"
    """)

    wrapper_path = Path(tempfile.mktemp(
        prefix=f"openshrimp-lima-{context_name}-",
        suffix=".sh",
    ))
    wrapper_path.write_text(script, encoding="utf-8")
    wrapper_path.chmod(stat.S_IRWXU)
    logger.info("Generated Lima CLI wrapper at %s", wrapper_path)
    return str(wrapper_path)
