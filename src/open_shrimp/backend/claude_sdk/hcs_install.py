"""In-guest Claude CLI installer for the HCS sandbox backend.

A Windows host has only a Windows ``claude`` build, which can't be donated to
the Linux guest (the libvirt backend copies the host's Linux binary; the Lima
backend, host≠guest OS, installs from npm — the HCS backend is in Lima's
position).  So the CLI is installed from npm *inside* the rootfs, over the
exec channel the launcher uses.

Keeping this under ``backend/claude_sdk/`` honours the invariant that the
``sandbox/`` package never names an agent: the sandbox exposes a generic
``guest_exec`` + ``hcs_install`` hook, and the agent-specific install command
lives here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CLAUDE_NPM_PACKAGE = "@anthropic-ai/claude-code"


def install_claude_cli_in_hcs(sandbox) -> None:
    """Ensure ``claude`` is on PATH inside the HCS rootfs.

    Idempotent: when the base rootfs image already ships the CLI, the version
    probe short-circuits and nothing is installed.  When absent (a plain
    Ubuntu rootfs), it is installed globally from npm — Node is the image's
    precondition.
    """
    code, out = sandbox.guest_exec(["claude", "--version"], read_timeout=60.0)
    if code == 0 and out.strip():
        logger.info("HCS guest already has claude: %s", out.strip())
        return

    logger.info("Installing Claude CLI into HCS rootfs from npm...")
    code, out = sandbox.guest_exec(
        ["npm", "install", "-g", _CLAUDE_NPM_PACKAGE], read_timeout=600.0,
    )
    if code != 0:
        raise RuntimeError(
            f"npm install of {_CLAUDE_NPM_PACKAGE} failed in HCS guest "
            f"(exit {code}):\n{out}"
        )
    code, out = sandbox.guest_exec(["claude", "--version"], read_timeout=60.0)
    if code != 0 or not out.strip():
        raise RuntimeError(
            f"claude still not runnable after npm install:\n{out}"
        )
    logger.info("HCS guest claude installed: %s", out.strip())
