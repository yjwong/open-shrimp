"""Locate the Claude CLI binary for host-side execution.

Used by anything on the host that needs to spawn ``claude``: sandbox
provisioning (Docker, libvirt, Lima) and the ``/login`` PTY endpoint.
The SDK-bundled binary is preferred so the version Claude runs with
matches the SDK version pinned in this project, instead of drifting to
whatever ``claude`` the user happens to have on PATH.
"""

from __future__ import annotations

import shutil
from pathlib import Path


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
            Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
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
    home = Path.home()
    for location in [
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ]:
        if location.exists() and location.is_file():
            return str(location)

    raise RuntimeError(
        "Could not find the Claude CLI binary. Searched: "
        "claude_agent_sdk bundled binary, system PATH, "
        "~/.npm-global/bin/claude, /usr/local/bin/claude, "
        "~/.local/bin/claude, ~/node_modules/.bin/claude, "
        "~/.yarn/bin/claude, ~/.claude/local/claude. "
        "Install claude-agent-sdk or ensure 'claude' is on your PATH."
    )
