"""Locate the Claude CLI binary for host-side execution.

Used by anything on the host that needs a *path* to ``claude``: sandbox
provisioning (Docker, libvirt, Lima) and the ``/login`` pty endpoint.
The SDK-bundled binary is preferred so the version Claude runs with
matches the SDK version pinned in this project, instead of drifting to
whatever ``claude`` the user happens to have on PATH.

This mirrors ``SubprocessCLITransport._find_cli`` in the Agent SDK,
which is private and reachable only by constructing a transport — it
resolves at spawn time and never hands back a path.  Because the SDK
spawns the CLI that answers turns while this resolves the one ``/login``
authenticates, the *preferred* binary must be the same one: the bundled
CLI, else whatever ``claude`` is on PATH.  Those two steps are what keep
the two resolvers agreeing, and they must not diverge from the SDK's.

The last-resort locations below are best-effort by comparison — reached
only when neither resolver would find anything else — so they may be
platform-shaped where the SDK's list is not.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from open_shrimp.binaries import EXE_SUFFIX

# The bundled CLI is a native executable; the wheel names it per platform.
BUNDLED_NAME = f"claude{EXE_SUFFIX}"


def _install_locations() -> list[Path]:
    """Per-user install locations, in preference order."""
    home = Path.home()
    if sys.platform == "win32":
        # PATHEXT makes shutil.which cover the .cmd/.exe shims, so the
        # only gap is npm's global prefix, which is not on PATH when the
        # bot runs as a service.  The POSIX paths below cannot hold a
        # Windows executable, so none of them carry over.
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        return [appdata / "npm/claude.cmd"]
    return [
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ]


def find_claude_binary() -> str:
    """Find the Claude CLI binary on disk.

    Resolution order:
    1. Bundled binary inside the claude_agent_sdk package
    2. System PATH via shutil.which
    3. Common installation locations

    Returns:
        Absolute path to the Claude CLI binary.

    Raises:
        RuntimeError: If no binary is found on disk.
    """
    # 1. Bundled binary in the SDK package.
    try:
        import claude_agent_sdk
        bundled = (
            Path(claude_agent_sdk.__file__).parent / "_bundled" / BUNDLED_NAME
        )
        if bundled.exists() and bundled.is_file():
            return str(bundled)
    except (ImportError, AttributeError):
        pass

    # 2. System PATH.
    system_claude = shutil.which("claude")
    if system_claude:
        return system_claude

    # 3. Common install locations.
    locations = _install_locations()
    for location in locations:
        if location.exists() and location.is_file():
            return str(location)

    searched = ", ".join(str(location) for location in locations)
    raise RuntimeError(
        "Could not find the Claude CLI binary. Searched: "
        f"claude_agent_sdk bundled binary ({BUNDLED_NAME}), system PATH, "
        f"{searched}. "
        "Install claude-agent-sdk or ensure 'claude' is on your PATH."
    )
