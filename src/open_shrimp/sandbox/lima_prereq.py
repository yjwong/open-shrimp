"""Prerequisites declared by the Lima sandbox backend."""

from __future__ import annotations

import platform
from typing import Any


def _check_lima(config: Any) -> tuple[bool, str]:
    """``limactl``, which the backend downloads when the host has none.

    A check must not fetch anything, so an absent ``limactl`` passes on the
    strength of that download — the same rule the HCS assets follow.  Only a
    platform Lima publishes no build for fails, because there the download
    has nothing to fetch and Homebrew is the one remaining route.

    Not ``find_binary``: that is for programs this project does not manage,
    and a ``limactl`` already downloaded into the managed bin directory is on
    nobody's ``$PATH``.
    """
    from open_shrimp.sandbox.lima_helpers import (
        LIMA_VERSION,
        find_limactl,
        limactl_downloadable,
    )

    path = find_limactl()
    if path:
        return True, f"found at {path}"
    if not limactl_downloadable():
        return False, (
            f"not found, and Lima publishes no {platform.system()} "
            f"{platform.machine()} build to download — install it with: "
            "brew install lima"
        )
    return True, (
        f"not installed — Lima {LIMA_VERSION} will be downloaded on first use"
    )


DECLARATION = (
    "lima",
    "Darwin",
    (("Lima", _check_lima),),
)
