"""Libvirt in-VM installer for the OpenCode CLI binary.

Looks up the host opencode binary and hands it to
:func:`open_shrimp.sandbox.libvirt_helpers.install_cli_via_ssh`.  When
the host has no opencode binary the install is a quiet no-op: the
operator's base image or ``provision:`` script must supply
``/usr/local/bin/opencode``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from open_shrimp.backend.opencode.binary import find_opencode_binary
from open_shrimp.sandbox.libvirt_helpers import install_cli_via_ssh

logger = logging.getLogger(__name__)


def install_opencode_cli_via_ssh(
    ssh_key: Path,
    ssh_port: int,
    ssh_user: str,
) -> None:
    """Install the OpenCode CLI inside a libvirt VM if not already present."""
    try:
        cli_binary = find_opencode_binary()
    except RuntimeError:
        logger.info(
            "OpenCode CLI not found on host; skipping VM install (operator "
            "must provide /usr/local/bin/opencode via base image or provision)."
        )
        return

    install_cli_via_ssh(
        "opencode", cli_binary,
        ssh_key=ssh_key, ssh_port=ssh_port, ssh_user=ssh_user,
    )
