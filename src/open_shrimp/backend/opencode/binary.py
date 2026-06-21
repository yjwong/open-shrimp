"""Locate the OpenCode CLI binary for host-side execution.

Used by anything on the host that needs to spawn ``opencode``: sandbox
image builds (Docker), sandbox provisioning (libvirt installer that SCPs
the host binary into the guest), and the host-local ``opencode serve``
spawn used by non-sandboxed contexts.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_opencode_binary() -> str:
    """Find the OpenCode CLI binary on disk.

    Resolution order:
    1. ``OPENCODE_BIN`` env var (when it points at an existing file)
    2. ``~/.opencode/bin/opencode``
    3. System PATH via :func:`shutil.which`

    Returns:
        Absolute path to the OpenCode CLI binary.

    Raises:
        RuntimeError: If no binary is found on disk.
    """
    env_bin = os.environ.get("OPENCODE_BIN")
    if env_bin and Path(env_bin).is_file():
        return env_bin
    home_bin = Path.home() / ".opencode" / "bin" / "opencode"
    if home_bin.is_file():
        return str(home_bin)
    which = shutil.which("opencode")
    if which:
        return which
    raise RuntimeError(
        "Could not find the `opencode` binary for the sandbox image. "
        "Set OPENCODE_BIN or install it at ~/.opencode/bin/opencode."
    )
