"""Lima VM helper functions.

Handles Lima binary management (auto-download), YAML template generation,
limactl CLI wrappers, config fingerprinting, and CLI wrapper script
generation.  All limactl invocations use ``LIMA_HOME`` to isolate
OpenShrimp's VMs from the user's personal Lima instances.
"""

from __future__ import annotations

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
from pathlib import Path

import yaml
from open_shrimp.config import SandboxConfig
from open_shrimp.paths import data_dir as _data_dir, get_instance_name as _get_instance_name

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
                os.getlogin(),
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

def _bin_dir() -> Path:
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

_DOWNLOAD_BASE = (
    f"https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}"
)

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

# Claude CLI binary download (GCS distribution).
_CLAUDE_CLI_GCS_BASE = (
    "https://storage.googleapis.com/"
    "claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/"
    "claude-code-releases"
)


# ---------------------------------------------------------------------------
# Lima binary management (following tunnel.py pattern)
# ---------------------------------------------------------------------------


def _find_limactl() -> str | None:
    """Find limactl: check managed bin dir first, then ``$PATH``."""
    local_bin = _bin_dir() / "limactl"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    path = shutil.which("limactl")
    if path:
        return path

    return None


def _download_lima_sync() -> str:
    """Download and extract the Lima release tarball (sync).

    Lima tarballs contain a ``bin/`` subdirectory with ``limactl``,
    ``lima``, etc.  All binaries are extracted to ``_bin_dir()``.

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

    bin_dir = _bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    url = f"{_DOWNLOAD_BASE}/{tarball_name}"
    logger.info("Downloading Lima %s from %s ...", LIMA_VERSION, url)

    import httpx
    import tarfile

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)

        # Lima expects share/lima/ (guest agents, templates) relative to
        # the install prefix.
        prefix_dir = bin_dir.parent
        with tarfile.open(tmp_path, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                if not member.isfile():
                    continue
                if name.startswith("bin/"):
                    dest = bin_dir / os.path.basename(name)
                elif name.startswith("share/"):
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

    target = bin_dir / "limactl"
    if not target.is_file():
        raise RuntimeError("limactl not found in downloaded Lima archive")

    logger.info("Lima %s downloaded to %s", LIMA_VERSION, bin_dir)
    return str(target)


def ensure_limactl_sync() -> str:
    """Ensure limactl is available, downloading if necessary (sync).

    Returns the path to the limactl binary.
    """
    path = _find_limactl()
    if path:
        logger.info("Found limactl at %s", path)
        return path

    logger.info("limactl not found, attempting auto-download...")
    return _download_lima_sync()


# ---------------------------------------------------------------------------
# State directory helpers
# ---------------------------------------------------------------------------


def state_dir_for(context_name: str) -> Path:
    """Return per-context state dir (separate from LIMA_HOME).

    This must NOT live under ``_lima_state_dir()`` because Lima treats
    any subdirectory there with a ``lima.yaml`` as an instance.
    """
    return _data_dir() / "lima-state" / context_name


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


def generate_lima_yaml(
    sdir: Path,
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None = None,
    computer_use: bool = False,
) -> Path:
    """Generate a Lima YAML template file.

    Writes to ``sdir/lima.yaml`` and returns the path.
    """
    sdir.mkdir(parents=True, exist_ok=True)

    # Detect host architecture for cloud image selection.
    machine = platform.machine()
    if machine == "arm64":
        arch = "aarch64"
    else:
        arch = "x86_64"

    images = []
    for img_arch, img_url in _CLOUD_IMAGES.items():
        images.append({"location": img_url, "arch": img_arch})

    # Build mounts.
    mounts = _build_mounts(sdir, project_dir, additional_directories)

    # Build provision scripts.
    provision = _build_provision_scripts(config, computer_use)

    # Port forwarding (Phase 2: add VNC).
    port_forward: list[dict] = []

    template: dict = {
        "vmType": "vz",
        "vmOpts": {
            "vz": {"rosetta": {"enabled": True, "binfmt": True}},
        },
        "cpus": config.cpus,
        "memory": f"{config.memory}MiB",
        "disk": f"{config.disk_size}GiB",
        "images": images,
        "mountType": "virtiofs",
        "mounts": mounts,
        "provision": provision,
        "containerd": {"system": False, "user": False},
        "ssh": {"forwardAgent": True},
    }

    yaml_path = sdir / "lima.yaml"
    yaml_path.write_text(yaml.dump(template, default_flow_style=False, sort_keys=False))
    logger.info("Generated Lima YAML template at %s", yaml_path)
    return yaml_path


def _build_mounts(
    sdir: Path,
    project_dir: str,
    additional_directories: list[str] | None,
) -> list[dict]:
    """Build Lima mount entries."""
    mounts = []

    # Project directory (writable).
    mounts.append({"location": project_dir, "writable": True})

    # Additional directories.
    for d in additional_directories or []:
        mounts.append({"location": d, "writable": True})

    # Host-side .claude home (shared into VM).
    # Lima creates the VM user as <username> with home /home/<username>.guest.
    vm_home = f"/home/{os.getlogin()}.guest"
    claude_home = str(sdir / "claude-home")
    Path(claude_home).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": claude_home,
        "mountPoint": f"{vm_home}/.claude",
        "writable": True,
    })

    # Host-side tmp directory (for task output files).
    tmp_dir = str(sdir / "tmp")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": tmp_dir,
        "mountPoint": "/tmp/claude-1000",
        "writable": True,
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
        systemctl restart sshd

        # Enable fstrim for disk space reclamation.
        systemctl enable --now fstrim.timer
    """)
    scripts.append({"mode": "system", "script": base_script})

    # User-provided provision script.
    if config.provision:
        scripts.append({"mode": "system", "script": config.provision})

    # Phase 2: computer_use provision would go here.

    return scripts


# ---------------------------------------------------------------------------
# Config fingerprinting (drift detection)
# ---------------------------------------------------------------------------


def lima_config_fingerprint(
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None,
    computer_use: bool,
) -> str:
    """SHA-256 fingerprint of the Lima YAML template content.

    Used to detect config drift and trigger VM rebuild.
    """
    # Generate the template to a temporary state dir for hashing.
    # We hash the YAML content so any change triggers rebuild.
    sdir_placeholder = Path(tempfile.mkdtemp(prefix="lima-fp-"))
    try:
        yaml_path = generate_lima_yaml(
            sdir_placeholder,
            config,
            project_dir,
            additional_directories,
            computer_use,
        )
        content = yaml_path.read_text()
    finally:
        shutil.rmtree(sdir_placeholder, ignore_errors=True)

    return hashlib.sha256(content.encode()).hexdigest()


def save_config_fingerprint(sdir: Path, fingerprint: str) -> None:
    """Persist the config fingerprint for drift detection."""
    (sdir / "config.sha256").write_text(fingerprint)


def load_config_fingerprint(sdir: Path) -> str | None:
    """Load the saved config fingerprint, or ``None`` if absent."""
    fp_file = sdir / "config.sha256"
    if fp_file.exists():
        return fp_file.read_text().strip()
    return None


# ---------------------------------------------------------------------------
# limactl CLI wrappers
# ---------------------------------------------------------------------------


def _log(log_file: Path | None, msg: str) -> None:
    """Append a line to the build log file (for terminal mini app)."""
    if log_file is not None:
        with open(log_file, "a") as f:
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
        with open(log_file, "a") as f:
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


def limactl_stop(limactl: str, name: str) -> None:
    """Stop a Lima instance."""
    _run_limactl(limactl, ["stop", name], check=False, timeout=120)
    logger.info("Stopped Lima instance %s", name)


def limactl_delete(limactl: str, name: str) -> None:
    """Delete a Lima instance."""
    _run_limactl(
        limactl, ["delete", "--force", name], check=False, timeout=60,
    )
    logger.info("Deleted Lima instance %s", name)


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


def limactl_instance_status(limactl: str, name: str) -> str | None:
    """Return instance status (``Running``, ``Stopped``, etc.) or ``None``."""
    for inst in limactl_list_json(limactl):
        if inst.get("name") == name:
            return inst.get("status")
    return None


def limactl_shell_check(limactl: str, name: str) -> bool:
    """Quick liveness check: ``limactl shell <name> -- true``."""
    result = _run_limactl(
        limactl, ["shell", name, "--", "true"], check=False, timeout=10,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Claude CLI binary provisioning for Linux guest
# ---------------------------------------------------------------------------


def _get_host_claude_version() -> str | None:
    """Get the Claude CLI version from the host binary."""
    claude = shutil.which("claude")
    if claude is None:
        # Try the bundled binary.
        try:
            import claude_agent_sdk

            bundled = (
                Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
            )
            if bundled.exists():
                claude = str(bundled)
        except (ImportError, AttributeError):
            pass

    if claude is None:
        return None

    try:
        result = subprocess.run(
            [claude, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Output format: "2.1.87 (Claude Code)" or just "2.1.87"
            version = result.stdout.strip().split()[0]
            return version
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def ensure_claude_cli_in_vm(limactl: str, inst_name: str) -> None:
    """Ensure the Claude CLI binary is installed inside the Lima VM.

    Downloads the Linux binary directly inside the VM using the GCS
    distribution URL, since the host macOS binary cannot run in the
    Linux guest.
    """
    # Check if claude is already available.
    result = _run_limactl(
        limactl,
        ["shell", inst_name, "--", "which", "claude"],
        check=False,
        timeout=10,
    )
    if result.returncode == 0:
        logger.info("Claude CLI already installed in VM %s", inst_name)
        return

    # Determine version from host.
    version = _get_host_claude_version()
    if version is None:
        raise RuntimeError(
            "Cannot determine Claude CLI version from host. "
            "Ensure 'claude' is installed and on your PATH."
        )

    # Determine guest architecture.
    arch_result = _run_limactl(
        limactl,
        ["shell", inst_name, "--", "uname", "-m"],
        check=True,
        timeout=10,
    )
    guest_arch = arch_result.stdout.strip()
    if guest_arch == "aarch64":
        platform_str = "linux-arm64"
    elif guest_arch == "x86_64":
        platform_str = "linux-x64"
    else:
        raise RuntimeError(f"Unsupported guest architecture: {guest_arch}")

    download_url = f"{_CLAUDE_CLI_GCS_BASE}/{version}/{platform_str}/claude"
    logger.info(
        "Downloading Claude CLI %s (%s) into VM %s...",
        version, platform_str, inst_name,
    )

    # Download and install inside the VM.
    install_cmd = (
        f"curl -fsSL {shlex.quote(download_url)} -o /tmp/claude "
        f"&& sudo mv /tmp/claude /usr/local/bin/claude "
        f"&& sudo chmod +x /usr/local/bin/claude"
    )
    _run_limactl(
        limactl,
        ["shell", inst_name, "--", "bash", "-c", install_cmd],
        check=True,
        timeout=120,
    )
    logger.info("Claude CLI %s installed in VM %s", version, inst_name)


# ---------------------------------------------------------------------------
# CLI wrapper script generation
# ---------------------------------------------------------------------------


def build_cli_wrapper(
    context_name: str,
    sdir: Path,
    limactl_path: str,
    project_dir: str,
    inst_name: str,
    claude_home_dir: Path | None = None,
) -> str:
    """Generate a bash wrapper that uses ``limactl shell`` to run Claude CLI.

    Returns the absolute path to the generated wrapper script.
    """
    # Credential copy block — extract from macOS Keychain into host-side
    # VirtioFS-shared directory so the Linux VM can pick it up.
    cred_block = ""
    if claude_home_dir is not None:
        cred_dest = shlex.quote(str(claude_home_dir / ".credentials.json"))
        cred_block = textwrap.dedent(f"""\
            # Extract fresh credentials from macOS Keychain.
            CRED_JSON=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w 2>/dev/null) || true
            if [ -n "$CRED_JSON" ]; then
                printf '%s' "$CRED_JSON" > {cred_dest}
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

    """) + cred_block + textwrap.dedent(f"""\

        # Build remote command with proper shell-escaping.
        # Source /etc/profile for full PATH (needed for npx / Playwright MCP).
        REMOTE_CMD=". /etc/profile{api_key_export}{git_env_export} && cd {shlex.quote(project_dir)} && claude"
        for arg in "$@"; do
            REMOTE_CMD+=" $(printf '%q' "$arg")"
        done

        exec "$LIMACTL" shell "$INSTANCE_NAME" -- bash -c "$REMOTE_CMD"
    """)

    wrapper_path = Path(tempfile.mktemp(
        prefix=f"openshrimp-lima-{context_name}-",
        suffix=".sh",
    ))
    wrapper_path.write_text(script)
    wrapper_path.chmod(stat.S_IRWXU)
    logger.info("Generated Lima CLI wrapper at %s", wrapper_path)
    return str(wrapper_path)
