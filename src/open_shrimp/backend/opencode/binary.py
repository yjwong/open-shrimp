"""Locate the opencode CLI this project installs and owns.

Only the copy under :data:`open_shrimp.binaries.BIN_DIR` is ever run.  An
opencode installed by other means — ``~/.opencode/bin/opencode``, a package
manager, anything on ``$PATH`` — carries a version and an update policy this
project does not control, and inheriting it is the drift the pin in
:mod:`open_shrimp.backend.opencode.release` exists to close.  So a caller that
finds nothing here downloads, through
:func:`open_shrimp.backend.opencode.install.ensure_opencode_binary`, rather
than falling back.

``OPENCODE_BIN`` stays as the one explicit escape hatch, for running a build
this project did not fetch.
"""

from __future__ import annotations

import os
from pathlib import Path

from open_shrimp.binaries import managed_binary, managed_binary_path

_NAME = "opencode"


def opencode_path() -> Path:
    """The only path a managed opencode is ever run from."""
    return managed_binary_path(_NAME)


def version_stamp_path() -> Path:
    """Where the version of the managed binary is recorded.

    Asked on every turn that starts a client, and ``opencode --version`` would
    answer it by spawning a 180 MB executable each time; a file written beside
    the binary costs a read.  A guest is asked once per sandbox start, which is
    why the in-guest installers probe the binary itself and carry no stamp.
    """
    return opencode_path().with_name(f"{_NAME}.version")


def opencode_override() -> str | None:
    """The binary ``OPENCODE_BIN`` names, when it names an existing one.

    Kept separate from :func:`managed_opencode` so a caller can tell an
    override from a download without comparing paths back against the
    environment: the two answer different questions, and only the download is
    this project's to keep at the pin.
    """
    env_bin = os.environ.get("OPENCODE_BIN")
    return env_bin if env_bin and Path(env_bin).is_file() else None


def managed_opencode() -> str | None:
    """The opencode to run, or ``None`` when nothing has been downloaded yet.

    ``None`` is an answer rather than an error: the caller's next move is to
    fetch it.
    """
    return opencode_override() or managed_binary(_NAME)
