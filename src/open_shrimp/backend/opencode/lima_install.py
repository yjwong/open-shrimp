"""Lima in-VM installer for the opencode CLI binary.

A Lima host is macOS and the guest is Linux, so the host's own build is not the
one the guest wants: the pinned Linux archive is downloaded inside the VM
instead, keyed on the guest's ``uname -m``.  The version and the checksum come
from :mod:`open_shrimp.backend.opencode.release`, shared with the HCS
installer, so what the guest verifies is what the host would have.
"""

from __future__ import annotations

import logging

from open_shrimp.backend.opencode.release import (
    OPENCODE_VERSION,
    guest_install_script,
)

logger = logging.getLogger(__name__)


def ensure_opencode_cli_in_vm(
    limactl: str,
    inst_name: str,
    guest_os: str = "linux",
) -> None:
    """Ensure the pinned opencode CLI binary is installed inside the Lima VM."""
    if guest_os != "linux":
        logger.info(
            "OpenCode Lima install only supports linux guests; skipping "
            "guest_os=%s", guest_os,
        )
        return

    from open_shrimp.sandbox.lima_helpers import install_cli_in_linux_vm

    install_cli_in_linux_vm(
        limactl,
        inst_name,
        "opencode",
        install_cmd_for=lambda arch: guest_install_script(
            f"linux-{arch}", sudo=True,
        ),
        expected_version=OPENCODE_VERSION,
        timeout=300,
    )
