"""Lima macOS guest helper functions.

macOS-specific equivalents of the Linux-specific functions in
``lima_helpers.py``.  Handles YAML template generation, provisioning,
Claude CLI installation, and CLI wrapper script generation for macOS
guest VMs running under Lima with Apple Virtualization.framework.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

import yaml
from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.lima_helpers import (
    _lima_state_dir,
    _run_limactl,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YAML template generation
# ---------------------------------------------------------------------------


def generate_lima_yaml_macos(
    sdir: Path,
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None = None,
    computer_use: bool = False,
    *,
    context_name: str = "",
) -> Path:
    """Generate a Lima YAML template for a macOS guest.

    Uses ``base: [template:_images/macos]`` to inherit the IPSW image
    URL, ``os: Darwin``, ``arch: aarch64``, and ``vmType: vz`` from
    Lima's built-in macOS template.

    Writes to ``sdir/lima.yaml`` and returns the path.
    """
    sdir.mkdir(parents=True, exist_ok=True)

    mounts = _build_mounts_macos(sdir, project_dir, additional_directories, computer_use)
    provision = _build_provision_scripts_macos(config, computer_use)

    template: dict = {
        "minimumLimaVersion": "2.1.0",
        "base": ["template:_images/macos"],
        "cpus": config.cpus,
        "memory": f"{config.memory}MiB",
        "disk": f"{config.disk_size}GiB",
        "video": _video_config_macos(computer_use),
        "mountType": "virtiofs",
        "mounts": mounts,
        "provision": provision,
        "ssh": {"forwardAgent": True},
    }

    yaml_path = sdir / "lima.yaml"
    yaml_path.write_text(
        yaml.dump(template, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("Generated macOS Lima YAML template at %s", yaml_path)
    return yaml_path


def _video_config_macos(computer_use: bool) -> dict:
    """Lima ``video`` config for macOS guests.

    For computer-use contexts, request the OpenShrimp-patched ``vnc``
    display mode: the patched ``limactl`` attaches Apple's private
    ``_VZVNCServer`` SPI to the running ``VZVirtualMachine`` and binds a
    localhost TCP listener instead of opening a graphics window.  The
    bound port is published via ``DisplayConnection`` and recorded by the
    hostagent in ``<LIMA_HOME>/<instance>/vncdisplay``.

    The ``to=`` option in ``video.vnc.display`` is required to make the
    hostagent call ``DisplayConnection`` — without it, Lima trusts the
    static ``host:display`` from the YAML and never asks the driver for
    the real port.
    """
    if computer_use:
        return {
            "display": "vnc",
            "vnc": {"display": "127.0.0.1:0,to=99"},
        }
    return {"display": "default"}


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


def _build_mounts_macos(
    sdir: Path,
    project_dir: str,
    additional_directories: list[str] | None,
    computer_use: bool = False,
) -> list[dict]:
    """Build Lima mount entries for a macOS guest.

    macOS Lima guests handle VirtioFS mounts via symlinks created by
    the guest agent.  The symlink target's parent directory must
    already exist in the guest.  On first boot, the guest agent may
    fail to create symlinks if parents are missing.  We fix this up
    in ``ensure_mounts_macos()`` which runs after the VM is responsive.
    """
    mounts = []

    # Project directory (writable).
    mounts.append({"location": project_dir, "writable": True})

    # Additional directories.
    for d in additional_directories or []:
        mounts.append({"location": d, "writable": True})

    # Host-side .claude home (shared into VM).
    # macOS Lima guest user home is /Users/<username>.guest.
    vm_home = f"/Users/{getpass.getuser()}.guest"
    claude_home = str(sdir / "claude-home")
    Path(claude_home).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": claude_home,
        "mountPoint": f"{vm_home}/.claude",
        "writable": True,
    })

    host_skills = Path.home() / ".claude" / "skills"
    if host_skills.is_dir():
        mounts.append({
            "location": str(host_skills),
            "mountPoint": f"{vm_home}/.claude/skills",
            "writable": False,
        })

    # Host-side tmp directory (for task output files).
    tmp_dir = str(sdir / "tmp")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": tmp_dir,
        "mountPoint": "/tmp/claude-1000",
        "writable": True,
    })

    if computer_use:
        # Screenshots directory.
        screenshots_dir = str(sdir / "screenshots")
        Path(screenshots_dir).mkdir(parents=True, exist_ok=True)
        mounts.append({
            "location": screenshots_dir,
            "mountPoint": "/tmp/screenshots",
            "writable": True,
        })

        # Text-input-state directory.
        text_input_state_dir = str(sdir / "text-input-state-dir")
        Path(text_input_state_dir).mkdir(parents=True, exist_ok=True)
        mounts.append({
            "location": text_input_state_dir,
            "mountPoint": "/tmp/text-input-state-dir",
            "writable": True,
        })

    return mounts


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

# Combined askpass + Homebrew install script.  Runs as a single
# mode:user provision so ordering is guaranteed — the askpass helper
# is created before brew tries to use it.
#
# NOTE: We inline the askpass creation here instead of using a separate
# mode:data or mode:system provision because:
# 1. Lima's yq merge pipeline (triggered by `base:`) corrupts newlines
#    in mode:data content fields (#magic___^_^___line markers).
# 2. mode:system provisions may not run before mode:user on macOS guests.
_HOMEBREW_INSTALL_SCRIPT = textwrap.dedent("""\
    #!/bin/bash
    set -eux -o pipefail

    # Skip post-login Setup Assistant buddy screens (iCloud, Siri, etc.).
    # Lima writes .skipbuddy to /Library/User Template/ but on Tahoe the
    # user home is populated from /System/Library/User Template/English.lproj/
    # which doesn't include it — so we create it explicitly.
    touch ~/.skipbuddy

    # Create sudo askpass helper (reads password from ~/password).
    PW=$(cat "$HOME/password")
    echo "$PW" | sudo -S mkdir -p /usr/local/bin
    echo "$PW" | sudo -S bash -c 'printf "#!/bin/sh\\nset -eu\\ncat \\"\\$HOME/password\\"\\n" > /usr/local/bin/lima-sudo-askpass.sh && chmod 755 /usr/local/bin/lima-sudo-askpass.sh'

    # Auto-login: write autoLoginUser plist key + /etc/kcpassword manually.
    # `sysadminctl -autologin set` half-fails on macOS Tahoe (26):
    # SACSetAutoLoginPassword returns error:22, autoLoginUser is set, but
    # /etc/kcpassword is never written — loginwindow then has no password
    # and shows the prompt instead of auto-logging in.  Doing it manually
    # is portable across versions.  Use perl since /usr/bin/python3 triggers
    # the Xcode CLT install dialog on fresh images.
    # Takes effect on next boot — reboot_if_first_provision() handles that.
    echo "$PW" | sudo -S defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser -string "$(whoami)"
    perl -e '
        my @key = (0x7d, 0x89, 0x52, 0x23, 0xd2, 0xbc, 0xdd, 0xea, 0xa3, 0xb9, 0x1f);
        my $pw = $ARGV[0];
        my @out;
        for (my $i = 0; $i < length($pw); $i++) {
            push @out, ord(substr($pw, $i, 1)) ^ $key[$i % scalar(@key)];
        }
        while (scalar(@out) % 12 != 0) {
            push @out, $key[scalar(@out) % scalar(@key)];
        }
        print pack("C*", @out);
    ' "$PW" > /tmp/kcpassword.new
    echo "$PW" | sudo -S install -m 600 -o root -g wheel /tmp/kcpassword.new /etc/kcpassword
    rm -f /tmp/kcpassword.new

    # Disable screen lock and screensaver.
    echo "$PW" | sudo -S sysadminctl -screenLock off -password "$PW"
    echo "$PW" | sudo -S defaults write /Library/Preferences/com.apple.screensaver loginWindowIdleTime 0
    defaults -currentHost write com.apple.screensaver idleTime 0

    # Disable sleep.
    echo "$PW" | sudo -S pmset -a sleep 0 displaysleep 0 disksleep 0

    # Install Homebrew.
    [ -e /opt/homebrew ] && exit 0
    curl -o homebrew-install.sh -fsSL \\
        https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh
    SUDO_ASKPASS=/usr/local/bin/lima-sudo-askpass.sh \\
        NONINTERACTIVE=1 /bin/bash homebrew-install.sh
    rm -f homebrew-install.sh
    # No /etc/profile.d on macOS — add Homebrew to shell profiles.
    echo >> ~/.zprofile
    echo 'eval "$(/opt/homebrew/bin/brew shellenv zsh)"' >> ~/.zprofile
    echo >> ~/.bash_profile
    echo 'eval "$(/opt/homebrew/bin/brew shellenv bash)"' >> ~/.bash_profile
""")


def _build_provision_scripts_macos(
    config: SandboxConfig,
    computer_use: bool = False,
) -> list[dict]:
    """Build Lima provision script entries for a macOS guest."""
    scripts: list[dict] = []

    # Askpass helper + Homebrew installation (single mode: user script).
    scripts.append({"mode": "user", "script": _HOMEBREW_INSTALL_SCRIPT})

    # User-provided provision script.
    if config.provision:
        scripts.append({"mode": "user", "script": config.provision})

    if computer_use:
        scripts.extend(_build_computer_use_provisions_macos())

    return scripts


def _build_computer_use_provisions_macos() -> list[dict]:
    """Build provision entries for the macOS computer-use stack.

    Enables Screen Sharing (built-in VNC), installs Node.js, Chrome,
    and Playwright MCP via Homebrew, and grants Accessibility access
    to sshd for osascript/CGEvent input injection over SSH.
    """
    provisions: list[dict] = []

    # System provision: enable Screen Sharing and grant Accessibility.
    system_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # Enable Screen Sharing (macOS built-in VNC server).
        /System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart \\
            -activate -configure -access -on -restart -agent -privs -all || true

        # Grant Accessibility access to sshd for osascript/CGEvent over SSH.
        # This modifies the system TCC database — fragile across macOS versions.
        TCC_DB="/Library/Application Support/com.apple.TCC/TCC.db"
        if [ -f "$TCC_DB" ]; then
            sqlite3 "$TCC_DB" \\
                "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version) \\
                 VALUES ('kTCCServiceAccessibility', '/usr/sbin/sshd', 1, 2, 4, 1);" 2>/dev/null || true
            # Also grant to bash and python3 for CGEvent usage.
            sqlite3 "$TCC_DB" \\
                "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version) \\
                 VALUES ('kTCCServiceAccessibility', '/bin/bash', 1, 2, 4, 1);" 2>/dev/null || true
            sqlite3 "$TCC_DB" \\
                "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version) \\
                 VALUES ('kTCCServiceAccessibility', '/usr/bin/python3', 1, 2, 4, 1);" 2>/dev/null || true
        fi
    """)
    provisions.append({"mode": "system", "script": system_script})

    # User provision: install browser, Node.js, Playwright MCP via Homebrew.
    user_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux -o pipefail
        eval "$(/opt/homebrew/bin/brew shellenv)"

        # Node.js for Playwright MCP.
        brew install node

        # Google Chrome.
        brew install --cask google-chrome

        # Claude Code and Playwright MCP.
        npm install -g @anthropic-ai/claude-code@latest @playwright/mcp
    """)
    provisions.append({"mode": "user", "script": user_script})

    return provisions


def ensure_mounts_macos(
    limactl: str,
    inst_name: str,
    mount_points: list[str],
) -> None:
    """Fix up VirtioFS mounts on a macOS guest.

    The Lima guest agent creates symlinks from mount points to
    ``/Volumes/My Shared Files/<tag>``, but fails if parent directories
    don't exist.  This function creates the parents and re-runs the
    guest agent's fake-cloud-init to retry the symlinks.
    """
    if not mount_points:
        return

    # Create parent directories then re-run the guest agent's
    # fake-cloud-init which reads mount entries from /Volumes/cidata/user-data
    # and creates symlinks from mount points to /Volumes/My Shared Files/<tag>.
    parents = sorted({str(Path(p).parent) for p in mount_points})
    mkdir_cmd = "mkdir -p " + " ".join(shlex.quote(p) for p in parents)

    cmd = f'echo "$(cat ~/password)" | sudo -S bash -c {shlex.quote(mkdir_cmd)}'
    result = _run_limactl(
        limactl,
        ["shell", inst_name, "--", "bash", "-c", cmd],
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to create mount point parents in macOS VM: %s",
            result.stderr.strip(),
        )
        return

    # Re-run the guest agent to create mount symlinks and run
    # provision scripts (askpass + Homebrew install, etc.).
    result = _run_limactl(
        limactl,
        [
            "shell", inst_name, "--", "bash", "-c",
            'echo "$(cat ~/password)" | '
            "sudo -S /Volumes/cidata/lima-guestagent fake-cloud-init",
        ],
        check=False,
        timeout=600,  # Homebrew + Xcode CLT install can be slow
    )
    if result.returncode != 0:
        logger.warning(
            "Guest agent re-run returned %d: %s",
            result.returncode, result.stderr.strip(),
        )
    else:
        logger.info("macOS VirtioFS mounts and provisioning fixed up")


def reboot_if_first_provision(
    limactl: str,
    inst_name: str,
    *,
    log_file: Path | None = None,
) -> None:
    """Reboot the macOS VM once after first provisioning.

    Auto-login only takes effect on boot, so the VM must
    be rebooted after the first provision that sets it up.  A marker
    file inside the VM tracks whether this has already happened.
    """
    marker = "/var/tmp/.openshrimp-provisioned"

    # Check if already rebooted after provisioning.
    result = _run_limactl(
        limactl,
        ["shell", inst_name, "--", "test", "-f", marker],
        check=False,
        timeout=10,
    )
    if result.returncode == 0:
        return  # Already done.

    logger.info("First provision detected — rebooting macOS VM for auto-login")
    if log_file:
        from open_shrimp.sandbox.lima_helpers import _log
        _log(log_file, "Rebooting VM for auto-login...")

    # Create marker before rebooting.
    _run_limactl(
        limactl,
        ["shell", inst_name, "--", "touch", marker],
        check=False,
        timeout=10,
    )

    # Reboot via limactl stop + start, then wait for SSH.
    from open_shrimp.sandbox.lima_helpers import (
        limactl_stop, limactl_start, limactl_shell_check,
        limactl_instance_status,
    )
    limactl_stop(limactl, inst_name)
    try:
        limactl_start(limactl, inst_name, log_file=log_file)
    except subprocess.CalledProcessError:
        # macOS guests often start in DEGRADED state — tolerate it
        # as long as the VM is running.
        if limactl_instance_status(limactl, inst_name) != "Running":
            raise

    # Wait for shell to be responsive after reboot.
    import time
    for _ in range(120):
        if limactl_shell_check(limactl, inst_name):
            break
        time.sleep(1)
    else:
        logger.warning("Shell not responsive after auto-login reboot")

    logger.info("macOS VM rebooted for auto-login")


# ---------------------------------------------------------------------------
# Config fingerprinting (drift detection)
# ---------------------------------------------------------------------------


def lima_config_fingerprint_macos(
    sdir: Path,
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None,
    computer_use: bool,
    *,
    context_name: str = "",
) -> str:
    """SHA-256 fingerprint of the macOS Lima YAML template content."""
    mounts = _build_mounts_macos(sdir, project_dir, additional_directories, computer_use)
    provision = _build_provision_scripts_macos(config, computer_use)

    template: dict = {
        "minimumLimaVersion": "2.1.0",
        "base": ["template:_images/macos"],
        "cpus": config.cpus,
        "memory": f"{config.memory}MiB",
        "disk": f"{config.disk_size}GiB",
        "video": _video_config_macos(computer_use),
        "mountType": "virtiofs",
        "mounts": mounts,
        "provision": provision,
        "ssh": {"forwardAgent": True},
    }

    content = yaml.dump(template, default_flow_style=False, sort_keys=False)
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Claude CLI installation
# ---------------------------------------------------------------------------


def ensure_claude_cli_in_vm_macos(
    limactl: str,
    inst_name: str,
    env: dict[str, str] | None = None,
) -> None:
    """Ensure the Claude CLI binary is installed inside the macOS VM.

    Since host and guest are both macOS ARM, copies the host binary
    directly via ``limactl copy`` instead of downloading.
    """
    # Check if claude is already available.
    result = _run_limactl(
        limactl,
        ["shell", inst_name, "--", "which", "claude"],
        check=False,
        timeout=10,
    )
    if result.returncode == 0:
        logger.info("Claude CLI already installed in macOS VM %s", inst_name)
        return

    # Find host binary.
    from open_shrimp.claude_binary import find_claude_binary
    host_binary = find_claude_binary()
    logger.info("Copying host Claude CLI (%s) into macOS VM %s...", host_binary, inst_name)

    # Copy into VM via limactl copy.
    _run_limactl(
        limactl,
        ["copy", host_binary, f"{inst_name}:/tmp/claude"],
        check=True,
        timeout=60,
    )

    # Move to /usr/local/bin using sudo -S with piped password.
    # Can't rely on SUDO_ASKPASS here — it may not exist yet on
    # first boot (provision scripts haven't run).
    install_cmd = (
        'PW=$(cat ~/password) && '
        'echo "$PW" | sudo -S mkdir -p /usr/local/bin && '
        'echo "$PW" | sudo -S mv /tmp/claude /usr/local/bin/claude && '
        'echo "$PW" | sudo -S chmod +x /usr/local/bin/claude'
    )
    _run_limactl(
        limactl,
        ["shell", inst_name, "--", "bash", "-c", install_cmd],
        check=True,
        timeout=30,
    )
    logger.info("Claude CLI installed in macOS VM %s", inst_name)


# ---------------------------------------------------------------------------
# CLI wrapper script generation
# ---------------------------------------------------------------------------


def build_cli_wrapper_macos(
    context_name: str,
    sdir: Path,
    limactl_path: str,
    project_dir: str,
    inst_name: str,
    claude_home_dir: Path | None = None,
) -> str:
    """Generate a bash wrapper that uses ``limactl shell`` to run Claude CLI
    inside a macOS guest VM.

    Returns the absolute path to the generated wrapper script.
    """
    # Credential copy block — extract from macOS Keychain into host-side
    # VirtioFS-shared directory so the macOS VM can pick it up.
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
        STATUS=$("$LIMACTL" list --json 2>/dev/null </dev/null | \\
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
        # Source shell profile for full PATH (Homebrew, npm, etc.).
        REMOTE_CMD="source ~/.zprofile 2>/dev/null || true{api_key_export}{git_env_export} && cd {shlex.quote(project_dir)} && claude"
        for arg in "$@"; do
            REMOTE_CMD+=" $(printf '%q' "$arg")"
        done

        exec "$LIMACTL" shell "$INSTANCE_NAME" -- bash -c "$REMOTE_CMD"
    """)

    wrapper_path = Path(tempfile.mktemp(
        prefix=f"openshrimp-lima-macos-{context_name}-",
        suffix=".sh",
    ))
    wrapper_path.write_text(script, encoding="utf-8")
    wrapper_path.chmod(stat.S_IRWXU)
    logger.info("Generated macOS Lima CLI wrapper at %s", wrapper_path)
    return str(wrapper_path)
