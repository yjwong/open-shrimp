"""In-guest opencode CLI installer for the HCS sandbox backend.

A Windows host has only a Windows ``opencode`` build, which can't be donated
to the Linux guest — so the libvirt installer's approach (hand the host binary
to the VM) is not available here, and following it would put a PE executable
on the guest's PATH.  Instead the pinned Linux release archive is fetched and
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
    OPENCODE_VERSION,
    guest_install_script,
    linux_guest_slug,
    opencode_download_url,
    version_token,
)

logger = logging.getLogger(__name__)


def install_opencode_cli_in_hcs(sandbox) -> None:
    """Ensure the pinned ``opencode`` is on PATH inside the HCS rootfs.

    Idempotent, and version-aware: a guest already reporting the pinned version
    downloads nothing, while one carrying an older build — a rootfs that ships
    the CLI, or a guest provisioned before a pin bump — is upgraded in place
    rather than left behind until somebody deletes the binary by hand.
    """
    code, out = sandbox.guest_exec(["opencode", "--version"], read_timeout=60.0)
    guest_version = version_token(out) if code == 0 else None
    if guest_version == OPENCODE_VERSION:
        logger.info("HCS guest already has opencode %s", guest_version)
        return
    if guest_version is not None:
        logger.info(
            "HCS guest has opencode %s, replacing it with %s",
            guest_version, OPENCODE_VERSION,
        )

    code, machine = sandbox.guest_exec(["uname", "-m"], read_timeout=30.0)
    if code != 0:
        raise RuntimeError(
            f"Failed to detect HCS guest architecture for opencode:\n{machine}"
        )
    slug = linux_guest_slug(machine)
    if slug is None:
        raise RuntimeError(
            f"No Linux opencode release for HCS guest architecture "
            f"{machine.strip()!r} (expected x86_64 or aarch64)."
        )

    url = opencode_download_url(slug)
    logger.info("Installing OpenCode CLI into HCS rootfs from %s...", url)
    # No ``sudo``: the chroot runs as root, which is the only way this differs
    # from what the Lima installer runs.
    code, out = sandbox.guest_exec(
        ["/bin/sh", "-c", guest_install_script(slug, sudo=False)],
        read_timeout=600.0,
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
