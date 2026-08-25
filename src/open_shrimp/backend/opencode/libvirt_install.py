"""Libvirt in-VM installer for the opencode CLI binary.

Fetches the pinned host binary and hands it to
:func:`open_shrimp.sandbox.libvirt_helpers.install_cli_via_ssh`, which compares
host and guest by sha256 — so bumping the pin propagates into an existing VM on
its next sandbox start with nothing else to do.

A libvirt host is Linux and the guest is Linux, so the host's own build is the
one the guest wants.
"""

from __future__ import annotations

import logging
from pathlib import Path

from open_shrimp.backend.opencode.install import ensure_opencode_binary
from open_shrimp.sandbox.libvirt_helpers import install_cli_via_ssh
from open_shrimp.sandbox.prefetch import log_progress

logger = logging.getLogger(__name__)


def install_opencode_cli_via_ssh(
    ssh_key: Path,
    ssh_port: int,
    ssh_user: str,
    *,
    log_file: Path | None = None,
) -> None:
    """Install the opencode CLI inside a libvirt VM if not already present.

    On a cold host this is where the 60 MB fetch happens, in the middle of
    provisioning and after the chat's boot message has stopped moving — so it
    is counted into the build log the "View build log" button opens.
    """
    binary = ensure_opencode_binary(
        progress=log_progress(log_file, "Downloading the opencode CLI"),
    )
    install_cli_via_ssh(
        "opencode", binary,
        ssh_key=ssh_key, ssh_port=ssh_port, ssh_user=ssh_user,
    )
