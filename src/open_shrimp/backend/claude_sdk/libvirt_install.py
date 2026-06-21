"""Libvirt in-VM installer + credential provisioning for the Claude CLI.

Looks up the host Claude binary and hands it to
:func:`open_shrimp.sandbox.libvirt_helpers.install_cli_via_ssh`, then
exposes :func:`provision_claude_credentials` for the virtiofs-shared
agent home (called by the VM sandbox after the binary install).
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from open_shrimp.backend.claude_sdk.binary import find_claude_binary
from open_shrimp.sandbox.libvirt_helpers import install_cli_via_ssh

logger = logging.getLogger(__name__)


def install_claude_cli_via_ssh(
    ssh_key: Path,
    ssh_port: int,
    ssh_user: str,
) -> None:
    """Install the Claude CLI inside a libvirt VM if not already present.

    Silently skips when the host has no Claude binary — the operator's
    base image or ``provision:`` script must then supply
    ``/usr/local/bin/claude``.
    """
    try:
        cli_binary = find_claude_binary()
    except RuntimeError:
        logger.info(
            "Claude CLI not found on host; skipping VM install (operator "
            "must provide /usr/local/bin/claude via base image or provision)."
        )
        return

    install_cli_via_ssh(
        "claude", cli_binary,
        ssh_key=ssh_key, ssh_port=ssh_port, ssh_user=ssh_user,
    )


def provision_claude_credentials(home_dir: Path) -> None:
    """Copy host Claude credentials into a virtiofs-shared agent home.

    The destination dir is mounted as ``/home/<sandbox-user>/.claude``
    inside the VM (``<sandbox-user>`` is ``openshrimp`` for libvirt and
    Lima), so the CLI picks the credentials up automatically.  On macOS
    the credentials live in the Keychain (read via ``security``);
    elsewhere they sit in ``~/.claude/.credentials.json``.
    """
    home_dir.mkdir(parents=True, exist_ok=True)
    dest = home_dir / ".credentials.json"

    if sys.platform == "darwin":
        from open_shrimp.sandbox.lima_helpers import _read_credentials_json

        payload = _read_credentials_json()
        if payload:
            dest.write_text(payload, encoding="utf-8")
            logger.info("Wrote Claude credentials (Keychain) to %s", dest)
            return

    host_credentials = Path.home() / ".claude" / ".credentials.json"
    if host_credentials.exists():
        shutil.copy2(str(host_credentials), str(dest))
        logger.info("Copied Claude credentials to %s", dest)
