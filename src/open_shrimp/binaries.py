"""Locating the helper binaries OpenShrimp downloads on demand.

cloudflared and moonshine-stt are both fetched from GitHub releases into a
managed bin directory.  Windows only resolves and executes files carrying an
executable extension, so the on-disk name is platform-dependent — every
lookup and every download target goes through here so the two agree.

A managed binary is only ever run from that directory.  A copy of the same
program installed on the machine carries a version, an update policy and a
configuration this project does not control, and the download exists so that
none of it is inherited — so a caller that finds nothing downloads rather
than falling back to ``$PATH``.
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


def managed_binary_path(name: str) -> Path:
    """Return the only path a managed *name* binary is ever run from."""
    return BIN_DIR / f"{name}{EXE_SUFFIX}"


def managed_binary(name: str) -> str | None:
    """Return the managed *name* binary, or *None* if it is not downloaded."""
    path = managed_binary_path(name)
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def find_binary(name: str) -> str | None:
    """Find *name* on ``$PATH`` — for programs this project does not manage."""
    return shutil.which(name)


def make_executable(path: Path) -> None:
    """Grant execute permission where the filesystem records it.

    A no-op on Windows, where executability follows the extension rather
    than a mode bit.
    """
    if sys.platform == "win32":
        return
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
