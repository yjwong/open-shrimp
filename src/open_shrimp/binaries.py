"""Locating the helper binaries OpenShrimp downloads on demand.

cloudflared and moonshine-stt are both fetched from GitHub releases into a
managed bin directory.  Windows only resolves and executes files carrying an
executable extension, so the on-disk name is platform-dependent — every
lookup and every download target goes through here so the two agree.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from platformdirs import user_data_path

# Directory where downloaded helper binaries are kept.  Deliberately not
# instance-scoped: the binaries are interchangeable across instances.
BIN_DIR = user_data_path("openshrimp") / "bin"

# Suffix a file must carry for Windows to treat it as executable.
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def local_binary_path(name: str) -> Path:
    """Return the managed path a downloaded *name* binary lives at."""
    return BIN_DIR / f"{name}{EXE_SUFFIX}"


def find_binary(name: str) -> str | None:
    """Find *name*, checking the managed bin dir first, then ``$PATH``."""
    local = local_binary_path(name)
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)

    return shutil.which(name)


def make_executable(path: Path) -> None:
    """Grant execute permission where the filesystem records it.

    A no-op on Windows, where executability follows the extension rather
    than a mode bit.
    """
    if sys.platform == "win32":
        return
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
