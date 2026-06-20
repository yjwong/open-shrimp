"""Lima in-VM installer for the Claude CLI binary.

The Lima host is often macOS while the guest is Linux, so the host
binary isn't usable in the guest.  Downloads the right Linux binary
inside the VM from the Claude GCS distribution URL, keyed on the host's
Claude version and the guest's ``uname -m``.  The macOS-guest path
delegates to :mod:`open_shrimp.sandbox.lima_macos_helpers` (host binary
copied directly because OS + arch match).
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from open_shrimp.backend.claude_sdk.binary import find_claude_binary

logger = logging.getLogger(__name__)

# Claude CLI binary download (GCS distribution).
_CLAUDE_CLI_GCS_BASE = (
    "https://storage.googleapis.com/"
    "claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/"
    "claude-code-releases"
)


def _get_host_claude_version() -> str:
    """Return the Claude CLI version reported by the host binary.

    Raises :class:`RuntimeError` when the binary is unavailable or
    reports no version — callers depend on the host version to pin the
    in-VM download, so a missing version is fatal.
    """
    try:
        claude = find_claude_binary()
    except RuntimeError as exc:
        raise RuntimeError(
            "Cannot determine Claude CLI version from host. "
            "Ensure 'claude' is installed and on your PATH."
        ) from exc

    try:
        result = subprocess.run(
            [claude, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            "Cannot determine Claude CLI version from host."
        ) from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "Cannot determine Claude CLI version from host."
        )
    # Output format: "2.1.87 (Claude Code)" or just "2.1.87".
    return result.stdout.strip().split()[0]


def _claude_download_url(version: str, arch_str: str) -> str:
    return f"{_CLAUDE_CLI_GCS_BASE}/{version}/linux-{arch_str}/claude"


def _claude_install_cmd(download_url: str) -> str:
    return (
        f"curl -fsSL {shlex.quote(download_url)} -o /tmp/claude "
        f"&& sudo mv /tmp/claude /usr/local/bin/claude "
        f"&& sudo chmod +x /usr/local/bin/claude"
    )


def ensure_claude_cli_in_vm(
    limactl: str,
    inst_name: str,
    guest_os: str = "linux",
) -> None:
    """Ensure the Claude CLI binary is installed inside the Lima VM."""
    if guest_os == "macos":
        from open_shrimp.sandbox.lima_macos_helpers import (
            ensure_claude_cli_in_vm_macos,
        )

        return ensure_claude_cli_in_vm_macos(limactl, inst_name)

    from open_shrimp.sandbox.lima_helpers import install_cli_in_linux_vm

    install_cli_in_linux_vm(
        limactl,
        inst_name,
        "claude",
        url_for=_claude_download_url,
        version_resolver=_get_host_claude_version,
        install_cmd_for=_claude_install_cmd,
        timeout=120,
    )
