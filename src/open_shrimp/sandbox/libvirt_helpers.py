"""Libvirt/QEMU helpers for VM-based sandbox isolation.

Provides domain XML generation, SSH key management, virtiofsd lifecycle,
cloud-init ISO creation, qcow2 overlay management, and the CLI wrapper
script for SSH-based Claude CLI execution inside KVM virtual machines.

Uses ``qemu:///session`` (rootless libvirt) — no root privileges required
after initial system package installation.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path
from xml.etree import ElementTree as ET

from platformdirs import user_data_path

from open_shrimp.config import SandboxConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_IMAGE_URL = (
    "https://cloud-images.ubuntu.com/noble/current/"
    "noble-server-cloudimg-amd64.img"
)
DEFAULT_BASE_IMAGE_NAME = "ubuntu-24.04-cloud.img"

_VM_STATE_DIR = user_data_path("openshrimp") / "vms"
_IMAGES_DIR = user_data_path("openshrimp") / "images"
_DOMAIN_PREFIX = "openshrimp"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def state_dir_for(context_name: str) -> Path:
    """Return the per-context state directory for VM artifacts."""
    return _VM_STATE_DIR / context_name


def domain_name(context_name: str, instance_prefix: str = _DOMAIN_PREFIX) -> str:
    """Return the libvirt domain name for a context."""
    return f"{instance_prefix}-{context_name}"


# ---------------------------------------------------------------------------
# virtiofsd discovery
# ---------------------------------------------------------------------------


_MIN_VIRTIOFSD_VERSION = (1, 11, 0)
"""Minimum virtiofsd version required.

v1.10.0 (shipped in Ubuntu Noble) has a vring notification deadlock:
after a transient ``process_queue_*()`` error, notifications are never
re-enabled, so the guest blocks forever.  Fixed in v1.11.0 (dbfe3c4).
"""


def _parse_virtiofsd_version(path: str) -> tuple[int, ...] | None:
    """Run ``virtiofsd --version`` and parse the version tuple."""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5,
        )
        # Output: "virtiofsd 1.13.3"
        for token in result.stdout.strip().split():
            parts = token.split(".")
            if len(parts) >= 2 and parts[0].isdigit():
                return tuple(int(p) for p in parts)
    except Exception:
        pass
    return None


def find_virtiofsd() -> str | None:
    """Locate the virtiofsd binary.

    Prefers the managed binary in ``~/.local/share/openshrimp/bin/``
    (auto-downloaded, known-good version), then falls back to ``$PATH``
    and known system locations — but only if they meet the minimum
    version requirement.

    Returns:
        Absolute path to virtiofsd, or ``None`` if not found.
    """
    # 1. Managed binary (auto-downloaded, always preferred).
    managed = user_data_path("openshrimp") / "bin" / "virtiofsd"
    if managed.is_file() and os.access(str(managed), os.X_OK):
        return str(managed)

    # 2. $PATH and known system locations — version-gated.
    candidates: list[str] = []
    path = shutil.which("virtiofsd")
    if path:
        candidates.append(path)
    for system_path in (
        "/usr/libexec/virtiofsd",
        "/usr/lib/qemu/virtiofsd",
    ):
        if os.path.isfile(system_path) and os.access(system_path, os.X_OK):
            candidates.append(system_path)

    for candidate in candidates:
        ver = _parse_virtiofsd_version(candidate)
        if ver is not None and ver >= _MIN_VIRTIOFSD_VERSION:
            return candidate

    # 3. If system versions are too old, return None so the caller can
    #    trigger an auto-download or show an error.
    return None


_VIRTIOFSD_DOWNLOAD_BASE = (
    "https://github.com/yjwong/open-shrimp/releases/latest/download"
)

_VIRTIOFSD_BINARY_MAP: dict[str, str] = {
    "x86_64": "virtiofsd-linux-x86_64",
    "aarch64": "virtiofsd-linux-aarch64",
}


def _download_virtiofsd() -> str:
    """Download the virtiofsd binary for this platform.

    Synchronous — called from ``start_reaper()`` which runs before any
    async code.

    Returns:
        Absolute path to the downloaded binary.

    Raises:
        RuntimeError: If the platform is unsupported or download fails.
    """
    import platform as _platform
    import urllib.request

    machine = _platform.machine()
    binary_name = _VIRTIOFSD_BINARY_MAP.get(machine)
    if binary_name is None:
        raise RuntimeError(
            f"No pre-built virtiofsd for {_platform.system()} {machine}. "
            f"Please install virtiofsd >= {'.'.join(str(v) for v in _MIN_VIRTIOFSD_VERSION)} manually."
        )

    bin_dir = user_data_path("openshrimp") / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "virtiofsd"
    url = f"{_VIRTIOFSD_DOWNLOAD_BASE}/{binary_name}"

    logger.info("Downloading virtiofsd from %s ...", url)

    tmp = target.with_suffix(".tmp")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        tmp.rename(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("virtiofsd %s downloaded to %s", binary_name, target)
    return str(target)


def ensure_virtiofsd() -> str:
    """Find or download virtiofsd, returning the binary path.

    Called during libvirt sandbox manager startup to guarantee a
    working virtiofsd is available before any VM is started.

    Returns:
        Absolute path to a virtiofsd binary meeting the minimum version.

    Raises:
        RuntimeError: If virtiofsd cannot be found or downloaded.
    """
    path = find_virtiofsd()
    if path is not None:
        return path

    logger.warning(
        "No virtiofsd >= %s found — downloading from GitHub releases...",
        ".".join(str(v) for v in _MIN_VIRTIOFSD_VERSION),
    )
    return _download_virtiofsd()


# ---------------------------------------------------------------------------
# SSH key management
# ---------------------------------------------------------------------------


def ensure_ssh_key(sdir: Path) -> tuple[Path, Path]:
    """Generate an SSH key pair if one doesn't exist.

    Returns:
        (private_key_path, public_key_path)
    """
    sdir.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Ensure the directory permissions are correct even if it already existed.
    sdir.chmod(0o700)

    private = sdir / "ssh_key"
    public = sdir / "ssh_key.pub"

    if private.exists() and public.exists():
        return private, public

    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(private), "-N", ""],
        check=True,
        capture_output=True,
    )
    # Ensure restrictive permissions.
    private.chmod(0o600)
    logger.info("Generated SSH key pair at %s", private)
    return private, public


# ---------------------------------------------------------------------------
# Free port allocation
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Cloud-init ISO
# ---------------------------------------------------------------------------


def _build_cloud_init_user_data(
    public_key: str,
    *,
    provision_script: str | None = None,
    computer_use: bool = False,
) -> str:
    """Build the cloud-init user-data YAML string.

    Extracted so :func:`cloud_init_fingerprint` can hash the same content
    that :func:`generate_cloud_init_iso` writes, ensuring any template
    change triggers a VM rebuild.
    """
    # Build write_files entries.
    write_files = textwrap.dedent("""\
        write_files:
          # AcceptEnv for API key forwarding via SSH.
          - path: /etc/ssh/sshd_config.d/openshrimp.conf
            content: |
              AcceptEnv ANTHROPIC_API_KEY
    """)

    if computer_use:
        # Items appended here are already dedented — use exact indentation
        # (2-space indent for YAML list items under write_files:).
        write_files += (
            "  # labwc Wayland compositor on virtio-gpu DRM (computer-use).\n"
            "  - path: /etc/systemd/system/wayland-compositor.service\n"
            "    content: |\n"
            "      [Unit]\n"
            "      Description=labwc Wayland compositor on DRM\n"
            "      Requires=seatd.service\n"
            "      After=seatd.service\n"
            "      [Service]\n"
            "      User=claude\n"
            "      SupplementaryGroups=video render input\n"
            "      Environment=WLR_BACKENDS=drm,libinput\n"
            "      Environment=XDG_RUNTIME_DIR=/run/user/1000\n"
            "      Environment=WAYLAND_DISPLAY=wayland-0\n"
            "      ExecStartPre=+/bin/bash -c 'mkdir -p /run/user/1000 && chown claude:claude /run/user/1000'\n"
            "      ExecStart=/usr/bin/labwc\n"
            "      Restart=on-failure\n"
            "      [Install]\n"
            "      WantedBy=multi-user.target\n"
            "  # Minimal labwc config for computer-use.\n"
            "  # Deferred so the claude user exists when the file is written.\n"
            "  - path: /home/claude/.config/labwc/rc.xml\n"
            "    owner: claude:claude\n"
            "    defer: true\n"
            "    content: |\n"
            '      <?xml version="1.0"?>\n'
            "      <labwc_config>\n"
            "        <theme><name></name><cornerRadius>0</cornerRadius></theme>\n"
            '        <keyboard><default /><keybind key="A-F4"><action name="Close" /></keybind></keyboard>\n'
            "        <mouse><default /></mouse>\n"
            "      </labwc_config>\n"
            "  # Chrome autostart (opens after compositor is up).\n"
            "  - path: /home/claude/.config/labwc/autostart\n"
            "    owner: claude:claude\n"
            "    defer: true\n"
            "    permissions: '0755'\n"
            "    content: |\n"
            "      #!/bin/sh\n"
            "      # Start Chrome with Wayland native rendering on virtio-gpu.\n"
            "      google-chrome --ozone-platform=wayland \\\n"
            "        --user-data-dir=/home/claude/.config/google-chrome-debug \\\n"
            "        --remote-debugging-port=9222 \\\n"
            "        --disable-background-networking \\\n"
            "        --disable-default-apps \\\n"
            "        --no-first-run \\\n"
            "        --window-size=1280,800 &\n"
            "      # Start a foot terminal.\n"
            "      foot &\n"
        )

    # Build runcmd entries.
    runcmd = textwrap.dedent("""\
        runcmd:
          - systemctl enable --now fstrim.timer
    """)

    if computer_use:
        runcmd += (
            "  - apt-get update -qq\n"
            "  - |\n"
            "    # Install Google Chrome (deb, not snap).\n"
            "    wget -q -O /tmp/google-chrome.deb 'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb'\n"
            "    apt-get install -y -qq /tmp/google-chrome.deb > /dev/null 2>&1\n"
            "    rm /tmp/google-chrome.deb\n"
            "  - apt-get install -y -qq labwc foot seatd > /dev/null 2>&1\n"
            # Node.js for npx (Playwright MCP is fetched on demand).
            "  - curl -fsSL https://deb.nodesource.com/setup_24.x | bash -\n"
            "  - apt-get install -y -qq nodejs > /dev/null 2>&1\n"
            "  - usermod -aG video,render,input claude\n"
            "  - systemctl enable --now seatd.service\n"
            "  - systemctl enable --now wayland-compositor.service\n"
        )

    user_data = textwrap.dedent(f"""\
        #cloud-config
        users:
          - name: claude
            shell: /bin/bash
            sudo: ALL=(ALL) NOPASSWD:ALL
            ssh_authorized_keys:
              - {public_key}
    """) + write_files + runcmd

    if provision_script:
        user_data += f"  - |\n"
        for line in provision_script.splitlines():
            user_data += f"    {line}\n"

    return user_data


def generate_cloud_init_iso(
    sdir: Path,
    public_key: str,
    *,
    provision_script: str | None = None,
    computer_use: bool = False,
) -> Path:
    """Generate a cloud-init ``cloud-init.iso`` with SSH key + user setup.

    Filesystem mounts are **not** handled here — they are managed
    dynamically via SSH in :func:`ensure_mounts`, so that config changes
    (adding/removing ``additional_directories``) take effect without
    rebuilding the VM overlay.

    When *computer_use* is True, adds a systemd service that starts the
    labwc Wayland compositor on the virtio-gpu DRM device, plus installs
    required GUI packages (labwc, foot terminal, Google Chrome).

    Args:
        sdir: State directory for this context.
        public_key: SSH public key contents.
        provision_script: Optional shell script to run on first boot.
        computer_use: Enable GUI compositor setup.

    Returns:
        Path to the generated ISO.
    """
    iso_path = sdir / "cloud-init.iso"

    user_data = _build_cloud_init_user_data(
        public_key,
        provision_script=provision_script,
        computer_use=computer_use,
    )

    meta_data = textwrap.dedent(f"""\
        instance-id: openshrimp-{sdir.name}
        local-hostname: openshrimp-{sdir.name}
    """)

    # Write temp files and generate ISO.
    user_data_path_f = sdir / "user-data"
    meta_data_path_f = sdir / "meta-data"
    user_data_path_f.write_text(user_data)
    meta_data_path_f.write_text(meta_data)

    subprocess.run(
        [
            "cloud-localds", str(iso_path),
            str(user_data_path_f), str(meta_data_path_f),
        ],
        check=True,
        capture_output=True,
    )
    logger.info("Generated cloud-init ISO at %s", iso_path)
    return iso_path


def ensure_mounts(
    ssh_port: int,
    ssh_key: Path,
    shared_dirs: list[str],
    fs_type: str = "virtiofs",
    mount_overrides: dict[str, str] | None = None,
) -> None:
    """Ensure shared directories are mounted inside the VM via SSH.

    Idempotent — creates mount points and systemd mount units only when
    missing, and starts them.  Also unmounts and removes units for
    directories that are no longer in the desired set.

    This runs after SSH is up, on every sandbox start, so config changes
    (adding/removing ``additional_directories``) take effect without
    rebuilding the VM.

    Args:
        ssh_port: Host port forwarded to guest SSH.
        ssh_key: Path to the SSH private key.
        shared_dirs: Host directories that should be mounted at their
            original paths inside the VM.
        fs_type: ``"virtiofs"`` or ``"9p"``.
        mount_overrides: Optional mapping of host directory path to
            guest mount path.  When a host directory appears in this
            dict, the systemd mount unit uses the override as the
            guest-side ``Where=`` path instead of the host path.
            The virtiofs/9p tag (``What=``) is still derived from
            the host path so it matches the domain XML.
    """
    ssh_opts = _ssh_common_opts(ssh_key, ssh_port)

    def _ssh_run(cmd: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["ssh", *ssh_opts, "claude@localhost", "--", cmd],
            capture_output=True,
        )

    # Build the desired set of mount units.
    # Use systemd-escape to get correct unit names (e.g. paths with dashes
    # need \x2d escaping — simple str.replace("/", "-") is wrong).
    _overrides = mount_overrides or {}
    desired: dict[str, tuple[str, str]] = {}  # unit_name -> (mount_path, unit_content)
    for host_dir in shared_dirs:
        tag = _fs_tag_for_dir(host_dir)
        # Use override guest path if provided, otherwise mount at the
        # same path as on the host.
        guest_path = _overrides.get(host_dir, host_dir)
        # systemd-escape --path produces the correct unit name stem.
        esc = subprocess.run(
            ["systemd-escape", "--path", guest_path],
            capture_output=True, text=True, check=True,
        )
        unit_name = esc.stdout.strip() + ".mount"

        options_line = ""
        if fs_type == "9p":
            options_line = "Options=trans=virtio,version=9p2000.L"

        unit_content = textwrap.dedent(f"""\
            [Unit]
            Description=Mount {host_dir} via {fs_type}
            DefaultDependencies=no
            After=local-fs.target
            [Mount]
            What={tag}
            Where={guest_path}
            Type={fs_type}
            {options_line}
            [Install]
            WantedBy=multi-user.target
        """).strip() + "\n"

        desired[unit_name] = (guest_path, unit_content)

    # Discover existing openshrimp-managed mount units in the VM.
    # We identify ours by the "Description=Mount ... via virtiofs/9p" pattern.
    result = _ssh_run(
        "grep -rl 'Description=Mount .* via' /etc/systemd/system/*.mount 2>/dev/null "
        "| xargs -r -n1 -d '\\n' basename"
    )
    existing_units = set(result.stdout.decode().split()) if result.returncode == 0 else set()

    # Remove stale units (no longer in config).
    stale_units = existing_units - set(desired.keys())
    for unit_name in stale_units:
        logger.info("Removing stale mount unit %s from VM", unit_name)
        _ssh_run(
            f"sudo systemctl stop {shlex.quote(unit_name)} 2>/dev/null; "
            f"sudo systemctl disable {shlex.quote(unit_name)} 2>/dev/null; "
            f"sudo rm -f /etc/systemd/system/{shlex.quote(unit_name)}"
        )

    # Create/update desired units and ensure they're mounted.
    for unit_name, (mount_path, unit_content) in desired.items():
        unit_file = f"/etc/systemd/system/{unit_name}"

        # Check if unit already exists with correct content.
        check = _ssh_run(f"cat {shlex.quote(unit_file)} 2>/dev/null")
        if check.returncode == 0 and check.stdout.decode() == unit_content:
            # Unit exists and is correct — just ensure it's mounted.
            _ssh_run(f"mountpoint -q {shlex.quote(mount_path)} || sudo systemctl start {shlex.quote(unit_name)}")
            continue

        # Write new/updated unit.  Use printf '%s' (not echo) to avoid
        # appending a trailing newline — otherwise the file won't match
        # unit_content on the next idempotency check, causing every call
        # to rewrite the unit and run systemctl enable --now (which can
        # hang when multiple callers race on the same mount unit).
        escaped_content = shlex.quote(unit_content)
        _ssh_run(
            f"sudo mkdir -p {shlex.quote(mount_path)} && "
            f"sudo chown claude:claude {shlex.quote(mount_path)} && "
            f"printf '%s' {escaped_content} | sudo tee {shlex.quote(unit_file)} > /dev/null && "
            f"sudo systemctl daemon-reload && "
            f"sudo systemctl enable --now {shlex.quote(unit_name)}"
        )
        logger.info("Configured mount unit %s -> %s in VM", unit_name, mount_path)

    if stale_units:
        _ssh_run("sudo systemctl daemon-reload")


def extract_fs_tags_from_xml(domain_xml: str) -> set[str]:
    """Extract the set of filesystem ``<target dir=...>`` tags from domain XML.

    Used to detect when the desired shared directories have changed and the
    domain needs to be re-defined.
    """
    root = ET.fromstring(domain_xml)
    tags: set[str] = set()
    for fs in root.iter("filesystem"):
        target = fs.find("target")
        if target is not None:
            dir_attr = target.get("dir")
            if dir_attr:
                tags.add(dir_attr)
    return tags


# ---------------------------------------------------------------------------
# Base image management
# ---------------------------------------------------------------------------


def ensure_base_image(base_image: str | None, *, log_file: Path | None = None) -> Path:
    """Ensure the base cloud image is available locally.

    Args:
        base_image: Path to a custom base image, or ``None`` to download
            the default Ubuntu 24.04 cloud image.
        log_file: Optional log file for download progress.

    Returns:
        Path to the base image on disk.
    """
    if base_image:
        path = Path(base_image)
        if not path.exists():
            raise FileNotFoundError(
                f"Base image not found: {base_image}"
            )
        return path

    # Default: download Ubuntu 24.04 cloud image if not cached.
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image_path = _IMAGES_DIR / DEFAULT_BASE_IMAGE_NAME

    if image_path.exists():
        return image_path

    logger.info("Downloading base cloud image to %s ...", image_path)
    _log(log_file, f"Downloading base cloud image: {DEFAULT_BASE_IMAGE_URL}")

    # Download with wget (available on most Linux systems).
    proc = subprocess.run(
        ["wget", "-q", "-O", str(image_path), DEFAULT_BASE_IMAGE_URL],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        image_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to download base image: {proc.stderr.strip()}"
        )

    _log(log_file, "Base cloud image downloaded.")
    logger.info("Downloaded base image to %s", image_path)
    return image_path


# ---------------------------------------------------------------------------
# qcow2 overlay management
# ---------------------------------------------------------------------------


def create_overlay(sdir: Path, base_image: Path, disk_size_gb: int) -> Path:
    """Create a qcow2 CoW overlay backed by the base image.

    Idempotent — returns the existing overlay if already present.

    Returns:
        Path to the overlay qcow2 file.
    """
    overlay = sdir / "overlay.qcow2"
    if overlay.exists():
        return overlay

    sdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "qemu-img", "create", "-f", "qcow2",
            "-b", str(base_image.resolve()), "-F", "qcow2",
            str(overlay), f"{disk_size_gb}G",
        ],
        check=True,
        capture_output=True,
    )
    logger.info("Created qcow2 overlay at %s (backed by %s)", overlay, base_image)
    return overlay


# ---------------------------------------------------------------------------
# Domain XML generation
# ---------------------------------------------------------------------------


def _fs_tag_for_dir(directory: str) -> str:
    """Return a virtiofs/9p tag for a host directory.

    The tag is used as the ``What=`` in the guest systemd mount unit and
    as the ``dir`` attribute in the domain XML ``<target>`` element.
    We use a deterministic short hash to avoid path-length issues with
    virtiofs tags (max ~36 chars in older QEMU).
    """
    import hashlib
    h = hashlib.sha256(directory.encode()).hexdigest()[:12]
    return f"fs-{h}"


def generate_domain_xml(
    dom_name: str,
    *,
    overlay_path: Path,
    cloud_init_iso: Path,
    serial_log: Path,
    ssh_port: int,
    memory_mb: int,
    vcpus: int,
    shared_dirs: list[tuple[str, Path | None]] | None = None,
    use_virtiofs: bool = False,
    computer_use: bool = False,
    virgl: bool = False,
) -> str:
    """Generate libvirt domain XML for a VM sandbox.

    Uses the ``qemu:commandline`` namespace for SLIRP port forwarding
    (libvirt's native ``<interface type='user'>`` doesn't support
    ``hostfwd`` without passt, which is broken on Ubuntu 24.04).

    Args:
        dom_name: Libvirt domain name.
        overlay_path: Path to qcow2 overlay disk.
        cloud_init_iso: Path to cloud-init ISO.
        serial_log: Path for serial console output.
        ssh_port: Host port to forward to guest SSH.
        memory_mb: Memory ceiling in MB.
        vcpus: Number of virtual CPUs.
        shared_dirs: List of ``(host_directory, virtiofs_socket | None)``
            tuples.  In virtiofs mode each entry has a socket path; in 9p
            mode the socket is ``None``.
        use_virtiofs: Whether virtiofs is available.
        computer_use: Enable GUI support — adds VNC display (auto-port),
            virtio-gpu video model, and virtio-keyboard/mouse input devices.
        virgl: Enable VirGL 3D GPU acceleration.  Adds an ``egl-headless``
            graphics device for the GL context and ``accel3d="yes"`` on the
            virtio-gpu model.  Requires a host GPU with a DRM render node.

    Returns:
        Domain XML string.
    """
    if shared_dirs is None:
        shared_dirs = []

    qemu_ns = "http://libvirt.org/schemas/domain/qemu/1.0"

    domain = ET.Element("domain", type="kvm")
    domain.set("xmlns:qemu", qemu_ns)

    ET.SubElement(domain, "name").text = dom_name
    ET.SubElement(domain, "memory", unit="MiB").text = str(memory_mb)
    ET.SubElement(domain, "vcpu").text = str(vcpus)

    # CPU: pass through host CPU model for full feature support.
    # Without this, QEMU defaults to a minimal CPU (qemu64) that lacks
    # modern extensions, causing V8/Bun to use inefficient memory paths
    # and OOM on small VMs.
    cpu = ET.SubElement(domain, "cpu", mode="host-passthrough")

    # OS boot config.
    os_elem = ET.SubElement(domain, "os")
    os_type = ET.SubElement(os_elem, "type", arch="x86_64", machine="q35")
    os_type.text = "hvm"
    ET.SubElement(os_elem, "boot", dev="hd")

    # Features: ACPI for graceful shutdown.
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")

    # Memory backing (required for virtiofs, also works with balloon).
    if use_virtiofs and shared_dirs:
        mem_backing = ET.SubElement(domain, "memoryBacking")
        ET.SubElement(mem_backing, "source", type="memfd")
        ET.SubElement(mem_backing, "access", mode="shared")

    # Devices.
    devices = ET.SubElement(domain, "devices")

    # Emulator.
    ET.SubElement(devices, "emulator").text = "/usr/bin/qemu-system-x86_64"

    # Main disk (qcow2 overlay with discard support).
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2", discard="unmap")
    ET.SubElement(disk, "source", file=str(overlay_path.resolve()))
    ET.SubElement(disk, "target", dev="vda", bus="virtio")

    # Cloud-init ISO.
    cdrom = ET.SubElement(devices, "disk", type="file", device="cdrom")
    ET.SubElement(cdrom, "driver", name="qemu", type="raw")
    ET.SubElement(cdrom, "source", file=str(cloud_init_iso.resolve()))
    ET.SubElement(cdrom, "target", dev="sda", bus="sata")
    ET.SubElement(cdrom, "readonly")

    # Primary serial console on PTY (enables `virsh console`).
    # The <log> element tees ttyS0 output to a file so we can stream
    # boot progress to the terminal mini app without changing guest config.
    serial0 = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial0, "target", port="0")
    ET.SubElement(serial0, "log", file=str(serial_log.resolve()), append="off")

    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")

    # Virtio-balloon with free-page-reporting.
    ET.SubElement(
        devices, "memballoon",
        model="virtio",
        freePageReporting="on",
        autodeflate="on",
    )

    # Filesystem passthrough — one entry per shared directory.
    for host_dir, virtiofs_sock in shared_dirs:
        tag = _fs_tag_for_dir(host_dir)
        if use_virtiofs and virtiofs_sock is not None:
            fs = ET.SubElement(devices, "filesystem", type="mount")
            ET.SubElement(fs, "driver", type="virtiofs")
            ET.SubElement(fs, "source", socket=str(virtiofs_sock.resolve()))
            ET.SubElement(fs, "target", dir=tag)
        else:
            fs = ET.SubElement(
                devices, "filesystem",
                type="mount", accessmode="mapped",
            )
            ET.SubElement(fs, "source", dir=host_dir)
            ET.SubElement(fs, "target", dir=tag)

    # Computer-use: VNC display + virtio-gpu + input devices.
    if computer_use:
        # VNC graphics — QEMU auto-assigns a port in the 5900+ range.
        ET.SubElement(
            devices, "graphics",
            type="vnc", port="-1", autoport="yes", listen="127.0.0.1",
        )
        if virgl:
            # egl-headless provides the GL context that VirGL needs;
            # VNC picks up the rendered framebuffer for remote display.
            egl = ET.SubElement(devices, "graphics", type="egl-headless")
            ET.SubElement(egl, "gl", rendernode="/dev/dri/renderD128")
        # virtio-gpu gives 1280x800 natively (built-in kernel driver).
        video = ET.SubElement(devices, "video")
        model_attrs: dict[str, str] = {"type": "virtio"}
        if virgl:
            model_attrs["heads"] = "1"
        model = ET.SubElement(video, "model", **model_attrs)
        if virgl:
            ET.SubElement(model, "acceleration", accel3d="yes")
        # Virtio keyboard + tablet (absolute pointer) for QMP input.
        ET.SubElement(devices, "input", type="keyboard", bus="virtio")
        ET.SubElement(devices, "input", type="tablet", bus="virtio")

    # QEMU commandline args for SLIRP networking with SSH port forward.
    qemu_cmdline = ET.SubElement(domain, f"{{{qemu_ns}}}commandline")
    ET.SubElement(qemu_cmdline, f"{{{qemu_ns}}}arg").set(
        "value", "-netdev"
    )
    ET.SubElement(qemu_cmdline, f"{{{qemu_ns}}}arg").set(
        "value", f"user,id=mynet0,hostfwd=tcp::{ssh_port}-:22"
    )
    ET.SubElement(qemu_cmdline, f"{{{qemu_ns}}}arg").set(
        "value", "-device"
    )
    ET.SubElement(qemu_cmdline, f"{{{qemu_ns}}}arg").set(
        "value", "virtio-net-pci,netdev=mynet0,addr=0x5"
    )

    ET.indent(domain, space="  ")
    return ET.tostring(domain, encoding="unicode", xml_declaration=False)


# ---------------------------------------------------------------------------
# virtiofsd lifecycle
# ---------------------------------------------------------------------------


def start_virtiofsd(
    socket_path: Path,
    shared_dir: str,
) -> subprocess.Popen[bytes]:
    """Start a virtiofsd process for filesystem passthrough.

    The process must be started before the VM and stopped after it.

    Args:
        socket_path: Path for the virtiofsd Unix socket.
        shared_dir: Host directory to share.

    Returns:
        The virtiofsd :class:`subprocess.Popen` handle.
    """
    virtiofsd_bin = find_virtiofsd()
    if not virtiofsd_bin:
        raise FileNotFoundError(
            "virtiofsd not found or version too old (need >= "
            f"{'.'.join(str(v) for v in _MIN_VIRTIOFSD_VERSION)}) — "
            "run ensure_virtiofsd() first or install manually"
        )

    # Remove stale socket.
    socket_path.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [
            virtiofsd_bin,
            f"--socket-path={socket_path}",
            f"--shared-dir={shared_dir}",
            "--sandbox=none",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    logger.info(
        "Started virtiofsd (pid=%d) socket=%s shared=%s",
        proc.pid, socket_path, shared_dir,
    )
    return proc


# ---------------------------------------------------------------------------
# SSH connectivity
# ---------------------------------------------------------------------------


def wait_for_ssh(
    ssh_port: int,
    ssh_key: Path,
    *,
    timeout: int = 60,
    user: str = "claude",
) -> bool:
    """Wait for SSH to become available on the VM.

    Args:
        ssh_port: Host port forwarded to guest SSH.
        ssh_key: Path to the SSH private key.
        timeout: Maximum seconds to wait.
        user: SSH username.

    Returns:
        ``True`` if SSH is reachable, ``False`` if timed out.
    """
    import time

    ssh_opts = _ssh_common_opts(ssh_key, ssh_port)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "ssh", *ssh_opts,
                "-o", "ConnectTimeout=1",
                f"{user}@localhost", "true",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(1)

    return False


def wait_for_cloud_init(
    ssh_port: int,
    ssh_key: Path,
    *,
    timeout: int = 300,
    user: str = "claude",
) -> bool:
    """Wait for cloud-init to finish inside the VM.

    Runs ``cloud-init status --wait`` via SSH, which blocks until
    cloud-init reaches a terminal state (done or error).

    Args:
        ssh_port: Host port forwarded to guest SSH.
        ssh_key: Path to the SSH private key.
        timeout: Maximum seconds to wait.
        user: SSH username.

    Returns:
        ``True`` if cloud-init completed, ``False`` if timed out.
    """
    ssh_opts = _ssh_common_opts(ssh_key, ssh_port)

    result = subprocess.run(
        [
            "ssh", *ssh_opts,
            "-o", f"ConnectTimeout={timeout}",
            f"{user}@localhost",
            "cloud-init", "status", "--wait",
        ],
        capture_output=True,
        timeout=timeout + 10,
    )
    if result.returncode == 0:
        logger.info("cloud-init finished (port %d)", ssh_port)
        return True

    stderr = result.stderr.decode(errors="replace").strip()
    logger.warning(
        "cloud-init wait returned %d on port %d: %s",
        result.returncode, ssh_port, stderr,
    )
    # Return code 2 means cloud-init finished with errors/recoverable —
    # still consider it "done" so the VM is usable.
    return result.returncode == 2


def ssh_check_alive(
    ssh_port: int,
    ssh_key: Path,
    *,
    user: str = "claude",
) -> bool:
    """Quick check if SSH is reachable."""
    ssh_opts = _ssh_common_opts(ssh_key, ssh_port)
    result = subprocess.run(
        [
            "ssh", *ssh_opts,
            "-o", "ConnectTimeout=2",
            f"{user}@localhost", "true",
        ],
        capture_output=True,
    )
    return result.returncode == 0


def _ssh_common_opts(ssh_key: Path, ssh_port: int) -> list[str]:
    """Return common SSH options."""
    return [
        "-i", str(ssh_key),
        "-p", str(ssh_port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]


# ---------------------------------------------------------------------------
# CLI wrapper script
# ---------------------------------------------------------------------------


def build_cli_wrapper(
    context_name: str,
    sdir: Path,
    ssh_port: int,
    project_dir: str,
    instance_prefix: str = _DOMAIN_PREFIX,
    claude_home_dir: Path | None = None,
) -> str:
    """Generate a bash wrapper script that SSHs into the VM to run Claude.

    The wrapper:
    - Checks if the VM's SSH is reachable
    - Self-heals by starting the domain if needed
    - Forwards ``ANTHROPIC_API_KEY`` via ``SendEnv``
    - Enables SSH agent forwarding for git operations
    - Copies fresh credentials before each invocation

    Args:
        context_name: Context name (used for domain name and temp file naming).
        sdir: State directory for this context.
        ssh_port: Host port forwarded to guest SSH.
        project_dir: Host project directory path.  The VM mounts this at
            the same path, so ``cd`` targets the real host path.
        instance_prefix: Libvirt domain name prefix.
        claude_home_dir: Host-side directory shared into the VM as
            ``/home/claude/.claude``.  When set, credentials are copied
            directly to this directory (no SCP needed).

    Returns:
        Absolute path to the generated wrapper script.
    """
    dom_name = domain_name(context_name, instance_prefix)
    ssh_key = sdir / "ssh_key"

    # Detect host-side credentials for copying into VM.
    claude_dir = Path.home() / ".claude"
    host_credentials = claude_dir / ".credentials.json"

    # Build the credential-copy block.  When claude_home_dir is set
    # (virtiofs/9p shared), copy directly on the host — no SCP needed.
    if claude_home_dir is not None:
        cred_block = textwrap.dedent(f"""\
            # Copy fresh credentials via host-side shared directory.
            HOST_CREDENTIALS={shlex.quote(str(host_credentials))}
            CLAUDE_HOME_DIR={shlex.quote(str(claude_home_dir))}
            if [ -f "$HOST_CREDENTIALS" ]; then
                cp "$HOST_CREDENTIALS" "$CLAUDE_HOME_DIR/.credentials.json" 2>/dev/null || true
            fi
        """)
    else:
        cred_block = textwrap.dedent(f"""\
            # Copy fresh credentials if they exist on the host.
            HOST_CREDENTIALS={shlex.quote(str(host_credentials))}
            if [ -f "$HOST_CREDENTIALS" ]; then
                scp -i "$SSH_KEY" -P "$VM_SSH_PORT" \\
                    -o StrictHostKeyChecking=no \\
                    -o UserKnownHostsFile=/dev/null \\
                    -o LogLevel=ERROR \\
                    "$HOST_CREDENTIALS" \\
                    "$VM_USER@localhost:/home/claude/.claude/.credentials.json" \\
                    </dev/null 2>/dev/null || true
            fi
        """)

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

    git_env_export = ""
    if git_env_parts:
        git_env_export = " && " + " && ".join(git_env_parts)

    script = textwrap.dedent(f"""\
        #!/bin/bash
        set -euo pipefail

        VM_SSH_PORT={ssh_port}
        SSH_KEY={shlex.quote(str(ssh_key))}
        VM_USER="claude"
        DOMAIN_NAME={shlex.quote(dom_name)}

        SSH_OPTS=(
            -i "$SSH_KEY"
            -p "$VM_SSH_PORT"
            -o StrictHostKeyChecking=no
            -o UserKnownHostsFile=/dev/null
            -o LogLevel=ERROR
        )

        # Check if VM is running; restart if needed.
        # All pre-flight commands must redirect stdin from /dev/null to
        # avoid consuming the SDK's JSON stream on our stdin.
        if ! ssh "${{SSH_OPTS[@]}}" -o ConnectTimeout=2 \\
             "$VM_USER@localhost" true </dev/null 2>/dev/null; then
            virsh -c qemu:///session start "$DOMAIN_NAME" </dev/null 2>/dev/null || true
            for i in $(seq 1 30); do
                ssh "${{SSH_OPTS[@]}}" -o ConnectTimeout=1 \\
                    "$VM_USER@localhost" true </dev/null 2>/dev/null && break
                sleep 1
            done
        fi

    """) + cred_block + textwrap.dedent(f"""\

        # Forward ANTHROPIC_API_KEY, enable agent forwarding for git, exec Claude CLI.
        # Build a properly quoted remote command string.  SSH concatenates
        # remote args into a single string and passes it to ``$SHELL -c``,
        # so we must shell-escape each argument for the remote shell.
        # Source /etc/profile so the remote shell picks up the full PATH
        # (needed for npx / Playwright MCP).  SSH non-login shells get a
        # minimal PATH that may not include /usr/bin.
        REMOTE_CMD=". /etc/profile{git_env_export} && cd {shlex.quote(project_dir)} && claude"
        for arg in "$@"; do
            REMOTE_CMD+=" $(printf '%q' "$arg")"
        done

        exec ssh "${{SSH_OPTS[@]}}" \\
            -o SendEnv=ANTHROPIC_API_KEY \\
            -o ForwardAgent=yes \\
            "$VM_USER@localhost" \\
            -- "$REMOTE_CMD"
    """)

    wrapper_path = Path(tempfile.mktemp(
        prefix=f"openshrimp-libvirt-{context_name}-",
        suffix=".sh",
    ))
    wrapper_path.write_text(script)
    wrapper_path.chmod(stat.S_IRWXU)
    logger.info("Generated CLI wrapper at %s", wrapper_path)
    return str(wrapper_path)


# ---------------------------------------------------------------------------
# Port persistence
# ---------------------------------------------------------------------------


def cloud_init_fingerprint(config: SandboxConfig, computer_use: bool) -> str:
    """Compute a SHA-256 fingerprint of the cloud-init user-data content.

    Cloud-init only runs on first boot, so if any of the template content
    changes the VM overlay must be rebuilt from scratch.  We hash the
    actual rendered user-data (with a placeholder SSH key) so that any
    change — including edits to systemd units, package lists, etc. —
    triggers a rebuild automatically.
    """
    import hashlib

    # Use a placeholder key so the fingerprint is stable across SSH key
    # regeneration (the key doesn't affect cloud-init behavior).
    user_data = _build_cloud_init_user_data(
        "FINGERPRINT_PLACEHOLDER_KEY",
        provision_script=config.provision,
        computer_use=computer_use,
    )
    return hashlib.sha256(user_data.encode()).hexdigest()


def save_cloud_init_fingerprint(sdir: Path, fingerprint: str) -> None:
    """Persist the cloud-init fingerprint for drift detection."""
    (sdir / "cloud-init.sha256").write_text(fingerprint)


def load_cloud_init_fingerprint(sdir: Path) -> str | None:
    """Load the saved cloud-init fingerprint, or ``None`` if absent."""
    fp_file = sdir / "cloud-init.sha256"
    if fp_file.exists():
        return fp_file.read_text().strip()
    return None


def save_ssh_port(sdir: Path, port: int) -> None:
    """Persist the SSH port for a context."""
    (sdir / "ssh_port").write_text(str(port))


def load_ssh_port(sdir: Path) -> int | None:
    """Load the persisted SSH port, or ``None``."""
    port_file = sdir / "ssh_port"
    if port_file.exists():
        try:
            return int(port_file.read_text().strip())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# VNC port discovery
# ---------------------------------------------------------------------------


def extract_vnc_port_from_xml(domain_xml: str) -> int | None:
    """Extract the auto-assigned VNC port from live domain XML.

    Parses ``<graphics type="vnc" port="NNNN" ...>`` from the domain's
    XML description.  Returns ``None`` if no VNC graphics device is
    configured or the port hasn't been assigned yet (port="-1").
    """
    root = ET.fromstring(domain_xml)
    for graphics in root.iter("graphics"):
        if graphics.get("type") == "vnc":
            port_str = graphics.get("port")
            if port_str and port_str != "-1":
                try:
                    return int(port_str)
                except ValueError:
                    pass
    return None


# ---------------------------------------------------------------------------
# QMP input injection
# ---------------------------------------------------------------------------

# All QMP input events target ``device="video0"`` (the virtio-vga).
# Without an explicit device, QEMU routes events to implicit PS/2
# input devices that the guest Wayland compositor (labwc) ignores.


def _check_qmp_response(resp: str, context: str) -> None:
    """Log a warning if a QMP response contains an error."""
    import json
    try:
        parsed = json.loads(resp)
    except (json.JSONDecodeError, TypeError):
        return
    if "error" in parsed:
        logger.warning(
            "QMP error during %s: %s",
            context, parsed["error"].get("desc", parsed["error"]),
        )


def qmp_send_mouse_event(
    conn: "libvirt.virConnect",  # type: ignore[name-defined]
    domain_name: str,
    x: int,
    y: int,
    *,
    button: str = "left",
    click: bool = True,
) -> None:
    """Send a mouse move + optional click via QMP ``input-send-event``.

    Uses absolute coordinates on the virtio-tablet device.  QMP absolute
    coordinates range from 0–32767; the caller provides screen coordinates
    (e.g. 0–1279 for x, 0–799 for y) which are scaled accordingly.

    Events are sent to the ``video0`` device (virtio-vga) so they reach
    the guest compositor.  Without an explicit device, QEMU routes events
    to implicit PS/2 devices that the Wayland compositor ignores.

    Args:
        conn: Active libvirt connection.
        domain_name: Libvirt domain name.
        x: Screen X coordinate.
        y: Screen Y coordinate.
        button: ``"left"``, ``"right"``, or ``"middle"``.
        click: If True, send button press+release after moving.
    """
    import json

    try:
        import libvirt_qemu  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "libvirt_qemu not available — install libvirt-python "
            "with QMP support"
        ) from exc

    import libvirt
    domain = conn.lookupByName(domain_name)

    # Scale screen coordinates to QMP absolute range (0–32767).
    abs_x = int(x * 32767 / 1279) if x > 0 else 0
    abs_y = int(y * 32767 / 799) if y > 0 else 0

    # Move pointer.
    move_cmd = json.dumps({
        "execute": "input-send-event",
        "arguments": {
            "device": "video0",
            "events": [
                {"type": "abs", "data": {"axis": "x", "value": abs_x}},
                {"type": "abs", "data": {"axis": "y", "value": abs_y}},
            ],
        },
    })
    resp = libvirt_qemu.qemuMonitorCommand(
        domain, move_cmd, libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
    )
    _check_qmp_response(resp, "mouse move")

    if click:
        # QMP InputButton enum values: "left", "right", "middle".
        # Press.
        press_cmd = json.dumps({
            "execute": "input-send-event",
            "arguments": {
                "device": "video0",
                "events": [
                    {"type": "btn", "data": {"button": button, "down": True}},
                ],
            },
        })
        resp = libvirt_qemu.qemuMonitorCommand(
            domain, press_cmd,
            libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
        )
        _check_qmp_response(resp, f"mouse {button} press")

        # Release.
        release_cmd = json.dumps({
            "execute": "input-send-event",
            "arguments": {
                "device": "video0",
                "events": [
                    {"type": "btn", "data": {"button": button, "down": False}},
                ],
            },
        })
        resp = libvirt_qemu.qemuMonitorCommand(
            domain, release_cmd,
            libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
        )
        _check_qmp_response(resp, f"mouse {button} release")


def qmp_send_key_event(
    conn: "libvirt.virConnect",  # type: ignore[name-defined]
    domain_name: str,
    qcode: str,
    *,
    down: bool | None = None,
) -> None:
    """Send a single key press/release via QMP ``input-send-event``.

    Args:
        conn: Active libvirt connection.
        domain_name: Libvirt domain name.
        qcode: QCode key name (e.g. ``"ret"``, ``"tab"``, ``"a"``).
        down: If ``None`` (default), sends press then release.
            If ``True``/``False``, sends only the specified event.
    """
    import json

    try:
        import libvirt_qemu  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "libvirt_qemu not available — install libvirt-python "
            "with QMP support"
        ) from exc

    import libvirt
    domain = conn.lookupByName(domain_name)

    def _send(is_down: bool) -> None:
        cmd = json.dumps({
            "execute": "input-send-event",
            "arguments": {
                "device": "video0",
                "events": [
                    {"type": "key", "data": {"key": {"type": "qcode", "data": qcode}, "down": is_down}},
                ],
            },
        })
        resp = libvirt_qemu.qemuMonitorCommand(
            domain, cmd,
            libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
        )
        _check_qmp_response(resp, f"key {qcode} {'down' if is_down else 'up'}")

    if down is None:
        _send(True)
        _send(False)
    else:
        _send(down)


def qmp_send_scroll_event(
    conn: "libvirt.virConnect",  # type: ignore[name-defined]
    domain_name: str,
    x: int,
    y: int,
    direction: str,
    amount: int = 3,
) -> None:
    """Send a scroll event via QMP: move pointer then send button events.

    Scroll wheel buttons: ``wheel-up`` (4), ``wheel-down`` (5).

    Args:
        conn: Active libvirt connection.
        domain_name: Libvirt domain name.
        x: Screen X coordinate.
        y: Screen Y coordinate.
        direction: ``"up"``, ``"down"``, ``"left"``, or ``"right"``.
        amount: Number of scroll steps.
    """
    import json

    try:
        import libvirt_qemu  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "libvirt_qemu not available — install libvirt-python "
            "with QMP support"
        ) from exc

    import libvirt
    domain = conn.lookupByName(domain_name)

    # Move pointer first.
    abs_x = int(x * 32767 / 1279) if x > 0 else 0
    abs_y = int(y * 32767 / 799) if y > 0 else 0

    move_cmd = json.dumps({
        "execute": "input-send-event",
        "arguments": {
            "device": "video0",
            "events": [
                {"type": "abs", "data": {"axis": "x", "value": abs_x}},
                {"type": "abs", "data": {"axis": "y", "value": abs_y}},
            ],
        },
    })
    resp = libvirt_qemu.qemuMonitorCommand(
        domain, move_cmd,
        libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
    )
    _check_qmp_response(resp, "scroll move")

    # QMP InputButton enum values for scroll.
    btn_name_map = {
        "up": "wheel-up",
        "down": "wheel-down",
        "left": "wheel-left",   # May not be supported by all guests.
        "right": "wheel-right",
    }
    btn_name = btn_name_map.get(direction, "wheel-down")

    # Send N scroll steps as press+release pairs.
    for _ in range(amount):
        for is_down in (True, False):
            cmd = json.dumps({
                "execute": "input-send-event",
                "arguments": {
                    "device": "video0",
                    "events": [
                        {"type": "btn", "data": {"button": btn_name, "down": is_down}},
                    ],
                },
            })
            resp = libvirt_qemu.qemuMonitorCommand(
                domain, cmd,
                libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
            )
            _check_qmp_response(resp, f"scroll {direction}")


def qmp_type_text(
    conn: "libvirt.virConnect",  # type: ignore[name-defined]
    domain_name: str,
    text: str,
) -> None:
    """Type a string by sending QMP key events for each character.

    Handles lowercase/uppercase ASCII, digits, and common symbols.
    Special characters are mapped to their shifted QCode equivalents.
    """
    # Map characters to (qcode, needs_shift).
    _CHAR_TO_QCODE: dict[str, tuple[str, bool]] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        _CHAR_TO_QCODE[c] = (c, False)
        _CHAR_TO_QCODE[c.upper()] = (c, True)
    for i, c in enumerate("1234567890"):
        _CHAR_TO_QCODE[c] = (c, False)
    _CHAR_TO_QCODE[" "] = ("spc", False)
    _CHAR_TO_QCODE["\n"] = ("ret", False)
    _CHAR_TO_QCODE["\t"] = ("tab", False)
    # Shifted digit-row symbols.
    for sym, qc in [
        ("!", "1"), ("@", "2"), ("#", "3"), ("$", "4"), ("%", "5"),
        ("^", "6"), ("&", "7"), ("*", "8"), ("(", "9"), (")", "0"),
    ]:
        _CHAR_TO_QCODE[sym] = (qc, True)
    # Common punctuation.
    for sym, qc, shifted in [
        ("-", "minus", False), ("_", "minus", True),
        ("=", "equal", False), ("+", "equal", True),
        ("[", "bracket_left", False), ("{", "bracket_left", True),
        ("]", "bracket_right", False), ("}", "bracket_right", True),
        (";", "semicolon", False), (":", "semicolon", True),
        ("'", "apostrophe", False), ('"', "apostrophe", True),
        (",", "comma", False), ("<", "comma", True),
        (".", "dot", False), (">", "dot", True),
        ("/", "slash", False), ("?", "slash", True),
        ("\\", "backslash", False), ("|", "backslash", True),
        ("`", "grave_accent", False), ("~", "grave_accent", True),
    ]:
        _CHAR_TO_QCODE[sym] = (qc, shifted)

    for ch in text:
        entry = _CHAR_TO_QCODE.get(ch)
        if entry is None:
            # Skip unknown characters.
            logger.debug("qmp_type_text: skipping unknown char %r", ch)
            continue
        qcode, needs_shift = entry
        if needs_shift:
            qmp_send_key_event(conn, domain_name, "shift", down=True)
        qmp_send_key_event(conn, domain_name, qcode)
        if needs_shift:
            qmp_send_key_event(conn, domain_name, "shift", down=False)


# QCode name mapping for common special keys and modifiers.
NAMED_KEY_TO_QCODE: dict[str, str] = {
    "return": "ret",
    "enter": "ret",
    "tab": "tab",
    "escape": "esc",
    "backspace": "backspace",
    "space": "spc",
    "delete": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
    "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
    "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    # Modifiers.
    "ctrl": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "super": "meta_l",
}


def qmp_send_key_combo(
    conn: "libvirt.virConnect",  # type: ignore[name-defined]
    domain_name: str,
    key_str: str,
) -> None:
    """Send a key or key combination (e.g. ``"ctrl+a"``, ``"Return"``).

    Parses modifier+key combos separated by ``+``.  Named keys are
    mapped to QCode via :data:`NAMED_KEY_TO_QCODE`.  Single ASCII
    characters are used as-is.
    """
    parts = key_str.split("+")

    if len(parts) == 1:
        # Single key.
        key = parts[0]
        qcode = NAMED_KEY_TO_QCODE.get(key.lower())
        if qcode:
            qmp_send_key_event(conn, domain_name, qcode)
        elif len(key) == 1:
            # Single printable character — type it.
            qmp_type_text(conn, domain_name, key)
        else:
            # Try as literal qcode.
            qmp_send_key_event(conn, domain_name, key.lower())
    else:
        # Modifier combo: press modifiers, press key, release all.
        modifiers = parts[:-1]
        key = parts[-1]

        mod_qcodes = [
            NAMED_KEY_TO_QCODE.get(m.lower(), m.lower())
            for m in modifiers
        ]
        key_qcode = NAMED_KEY_TO_QCODE.get(key.lower())
        if key_qcode is None:
            key_qcode = key.lower() if len(key) == 1 else key.lower()

        # Press modifiers.
        for mq in mod_qcodes:
            qmp_send_key_event(conn, domain_name, mq, down=True)
        # Press and release the main key.
        qmp_send_key_event(conn, domain_name, key_qcode)
        # Release modifiers (reverse order).
        for mq in reversed(mod_qcodes):
            qmp_send_key_event(conn, domain_name, mq, down=False)


# ---------------------------------------------------------------------------
# domain.screenshot() helper
# ---------------------------------------------------------------------------


def domain_screenshot_png(
    conn: "libvirt.virConnect",  # type: ignore[name-defined]
    domain_name: str,
    output_path: Path,
) -> None:
    """Take a screenshot via ``domain.screenshot()`` and save as PNG.

    ``domain.screenshot()`` returns PNG data directly when using
    virtio-gpu.  The data is streamed via a libvirt stream object and
    written to *output_path*.

    Args:
        conn: Active libvirt connection.
        domain_name: Libvirt domain name.
        output_path: Path to write the PNG file.
    """
    import libvirt

    domain = conn.lookupByName(domain_name)
    stream = conn.newStream(0)

    try:
        _mime = domain.screenshot(stream, 0)
        chunks: list[bytes] = []
        while True:
            data = stream.recv(65536)
            if not data:
                break
            chunks.append(data)
        stream.finish()
    except Exception:
        try:
            stream.abort()
        except Exception:
            pass
        raise

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"".join(chunks))


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def _log(log_file: Path | None, message: str) -> None:
    """Append a message to the build log file (if provided)."""
    if log_file:
        with open(log_file, "a") as f:
            f.write(message + "\n")
