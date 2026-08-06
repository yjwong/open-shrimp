"""Lima in-VM installer for the OpenCode CLI binary.

Downloads the right Linux binary inside the VM from the GitHub Releases
archive, keyed on the guest's ``uname -m``.  Version selection and asset
naming live in :mod:`open_shrimp.backend.opencode.release`, shared with the
HCS installer.
"""

from __future__ import annotations

import logging
import shlex

from open_shrimp.backend.opencode.release import (
    opencode_download_url,
    resolve_opencode_version,
)

logger = logging.getLogger(__name__)


def _opencode_install_cmd(download_url: str) -> str:
    return (
        f"curl -fsSL {shlex.quote(download_url)} -o /tmp/opencode.tar.gz "
        f"&& tar -xzf /tmp/opencode.tar.gz -C /tmp/ "
        f"&& sudo install -m 755 /tmp/opencode /usr/local/bin/opencode "
        f"&& rm -f /tmp/opencode.tar.gz /tmp/opencode"
    )


def ensure_opencode_cli_in_vm(
    limactl: str,
    inst_name: str,
    guest_os: str = "linux",
) -> None:
    """Ensure the OpenCode CLI binary is installed inside the Lima VM."""
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
        url_for=opencode_download_url,
        version_resolver=resolve_opencode_version,
        install_cmd_for=_opencode_install_cmd,
        timeout=300,
    )
