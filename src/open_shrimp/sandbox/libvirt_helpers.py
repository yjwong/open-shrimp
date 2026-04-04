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


def find_virtiofsd() -> str | None:
    """Locate the virtiofsd binary.

    Checks ``$PATH`` first, then known system locations on Ubuntu/Debian.

    Returns:
        Absolute path to virtiofsd, or ``None`` if not found.
    """
    # Check $PATH first.
    path = shutil.which("virtiofsd")
    if path:
        return path

    # Known system locations (Ubuntu, Debian).
    for candidate in (
        "/usr/libexec/virtiofsd",
        "/usr/lib/qemu/virtiofsd",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


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


def generate_cloud_init_iso(
    sdir: Path,
    public_key: str,
    *,
    provision_script: str | None = None,
) -> Path:
    """Generate a cloud-init ``cloud-init.iso`` with SSH key + user setup.

    Filesystem mounts are **not** handled here — they are managed
    dynamically via SSH in :func:`ensure_mounts`, so that config changes
    (adding/removing ``additional_directories``) take effect without
    rebuilding the VM overlay.

    Args:
        sdir: State directory for this context.
        public_key: SSH public key contents.
        provision_script: Optional shell script to run on first boot.

    Returns:
        Path to the generated ISO.
    """
    iso_path = sdir / "cloud-init.iso"

    user_data = textwrap.dedent(f"""\
        #cloud-config
        users:
          - name: claude
            shell: /bin/bash
            sudo: ALL=(ALL) NOPASSWD:ALL
            ssh_authorized_keys:
              - {public_key}
        write_files:
          # AcceptEnv for API key forwarding via SSH.
          - path: /etc/ssh/sshd_config.d/openshrimp.conf
            content: |
              AcceptEnv ANTHROPIC_API_KEY
          # Regenerate empty SSH host keys on boot (defense-in-depth
          # against virsh destroy / power loss corrupting host keys).
          - path: /etc/systemd/system/ssh-hostkeys-guard.service
            content: |
              [Unit]
              Description=Regenerate empty SSH host keys
              Before=ssh.socket
              [Service]
              Type=oneshot
              ExecStart=/bin/bash -c 'for f in /etc/ssh/ssh_host_*_key; do ssh-keygen -l -f "$f" >/dev/null 2>&1 || {{ rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; ssh-keygen -A; break; }}; done'
              [Install]
              WantedBy=multi-user.target
        runcmd:
          - systemctl enable ssh-hostkeys-guard.service
          - systemctl enable --now fstrim.timer
    """)

    if provision_script:
        user_data += f"  - |\n"
        for line in provision_script.splitlines():
            user_data += f"    {line}\n"

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
    desired: dict[str, tuple[str, str]] = {}  # unit_name -> (mount_path, unit_content)
    for host_dir in shared_dirs:
        tag = _fs_tag_for_dir(host_dir)
        # systemd-escape --path produces the correct unit name stem.
        esc = subprocess.run(
            ["systemd-escape", "--path", host_dir],
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
            Where={host_dir}
            Type={fs_type}
            {options_line}
            [Install]
            WantedBy=multi-user.target
        """).strip() + "\n"

        desired[unit_name] = (host_dir, unit_content)

    # Discover existing openshrimp-managed mount units in the VM.
    # We identify ours by the "Description=Mount ... via virtiofs/9p" pattern.
    result = _ssh_run(
        "grep -rl 'Description=Mount .* via' /etc/systemd/system/*.mount 2>/dev/null "
        "| xargs -r -n1 basename"
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

        # Write new/updated unit.
        escaped_content = shlex.quote(unit_content)
        _ssh_run(
            f"sudo mkdir -p {shlex.quote(mount_path)} && "
            f"sudo chown claude:claude {shlex.quote(mount_path)} && "
            f"echo {escaped_content} | sudo tee {shlex.quote(unit_file)} > /dev/null && "
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
    serial0 = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial0, "target", port="0")

    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")

    # Secondary serial logging to file (boot diagnostics).
    serial1 = ET.SubElement(devices, "serial", type="file")
    ET.SubElement(serial1, "source", path=str(serial_log.resolve()))
    ET.SubElement(serial1, "target", port="1")

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
            "virtiofsd not found — install with: sudo apt install virtiofsd"
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

    Returns:
        Absolute path to the generated wrapper script.
    """
    dom_name = domain_name(context_name, instance_prefix)
    ssh_key = sdir / "ssh_key"

    # Detect host-side credentials for copying into VM.
    claude_dir = Path.home() / ".claude"
    host_credentials = claude_dir / ".credentials.json"

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

        # Forward ANTHROPIC_API_KEY, enable agent forwarding for git, exec Claude CLI.
        # Build a properly quoted remote command string.  SSH concatenates
        # remote args into a single string and passes it to ``$SHELL -c``,
        # so we must shell-escape each argument for the remote shell.
        REMOTE_CMD="cd {shlex.quote(project_dir)} && claude"
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


def cleanup_wrapper(wrapper_path: str) -> None:
    """Remove a CLI wrapper script."""
    try:
        Path(wrapper_path).unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove wrapper %s", wrapper_path)


# ---------------------------------------------------------------------------
# Port persistence
# ---------------------------------------------------------------------------


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
# Logging helper
# ---------------------------------------------------------------------------


def _log(log_file: Path | None, message: str) -> None:
    """Append a message to the build log file (if provided)."""
    if log_file:
        with open(log_file, "a") as f:
            f.write(message + "\n")
