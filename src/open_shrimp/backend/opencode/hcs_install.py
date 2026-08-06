"""In-guest OpenCode CLI installer for the HCS sandbox backend.

A Windows host has only a Windows ``opencode`` build, which can't be donated
to the Linux guest — so the libvirt installer's approach (hand the host binary
to the VM) is not available here, and following it would put a PE executable
on the guest's PATH.  Instead the Linux release archive is fetched and
unpacked *inside* the rootfs, over the exec channel the launcher uses; this is
the Lima installer's approach with ``limactl shell`` swapped for
``guest_exec`` and the ``sudo`` hop dropped (the chroot runs as root).

Keeping this under ``backend/opencode/`` honours the invariant that the
``sandbox/`` package never names an agent: the sandbox exposes a generic
``guest_exec`` + ``hcs_install`` hook, and the agent-specific install command
lives here.
"""

from __future__ import annotations

import logging

from open_shrimp.backend.opencode.release import (
    opencode_download_url,
    resolve_opencode_version,
)

logger = logging.getLogger(__name__)

#: ``uname -m`` → the arch slug in the release asset name.  Only the glibc
#: builds are used: the rootfs is a Debian-family image.  The ``-baseline``
#: variant (CPUs without AVX2) exists but is not pre-detected, matching the
#: Lima installer.
_GUEST_ARCH_ASSETS = {"x86_64": "x64", "aarch64": "arm64"}

#: Download + install, run as ``sh -c <script> _ <url>``.  Either downloader
#: will do — the rootfs images carry one or the other, not reliably both.
_INSTALL_SCRIPT = (
    "set -e; "
    'if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o /tmp/opencode.tar.gz; '
    'else wget -q -O /tmp/opencode.tar.gz "$1"; fi; '
    "tar -xzf /tmp/opencode.tar.gz -C /tmp; "
    "install -m 755 /tmp/opencode /usr/local/bin/opencode; "
    "rm -f /tmp/opencode.tar.gz /tmp/opencode"
)


def install_opencode_cli_in_hcs(sandbox) -> None:
    """Ensure ``opencode`` is on PATH inside the HCS rootfs.

    Idempotent: when the base rootfs image already ships the CLI, the version
    probe short-circuits and nothing is downloaded.  When absent, the Linux
    release archive matching the guest's architecture is installed to
    ``/usr/local/bin``.
    """
    code, out = sandbox.guest_exec(["opencode", "--version"], read_timeout=60.0)
    if code == 0 and out.strip():
        logger.info("HCS guest already has opencode: %s", out.strip())
        return

    code, machine = sandbox.guest_exec(["uname", "-m"], read_timeout=30.0)
    if code != 0:
        raise RuntimeError(
            f"Failed to detect HCS guest architecture for opencode:\n{machine}"
        )
    arch = _GUEST_ARCH_ASSETS.get(machine.strip())
    if arch is None:
        raise RuntimeError(
            f"No Linux opencode release for HCS guest architecture "
            f"{machine.strip()!r} (expected one of "
            f"{', '.join(sorted(_GUEST_ARCH_ASSETS))})."
        )

    url = opencode_download_url(resolve_opencode_version(), arch)
    logger.info("Installing OpenCode CLI into HCS rootfs from %s...", url)
    code, out = sandbox.guest_exec(
        ["/bin/sh", "-c", _INSTALL_SCRIPT, "_", url], read_timeout=600.0,
    )
    if code != 0:
        raise RuntimeError(
            f"Install of {url} failed in HCS guest (exit {code}):\n{out}"
        )
    code, out = sandbox.guest_exec(["opencode", "--version"], read_timeout=60.0)
    if code != 0 or not out.strip():
        raise RuntimeError(
            f"opencode still not runnable after install:\n{out}"
        )
    logger.info("HCS guest opencode installed: %s", out.strip())
