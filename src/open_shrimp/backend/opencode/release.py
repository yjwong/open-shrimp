"""OpenCode release resolution: which version, and which Linux asset.

Shared by every in-guest installer (Lima, HCS).  OpenCode ships per-platform
archives on GitHub Releases; for Linux guests on common arches the unqualified
``opencode-linux-{arch}.tar.gz`` asset is the right one.  ``-baseline`` (CPUs
without AVX2) and ``-musl`` (Alpine/musl guests) variants exist but are only
needed in exotic configurations and are not pre-detected.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request

from open_shrimp.backend.opencode.binary import find_opencode_binary

logger = logging.getLogger(__name__)

_RELEASES_API = (
    "https://api.github.com/repos/anomalyco/opencode/releases/latest"
)
_DOWNLOAD_TEMPLATE = (
    "https://github.com/anomalyco/opencode/releases/download/"
    "v{version}/opencode-linux-{arch}.tar.gz"
)


def _host_version() -> str | None:
    try:
        binary = find_opencode_binary()
    except RuntimeError:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip().split()[0]
    return raw.lstrip("v") or None


def _latest_release_version() -> str:
    req = urllib.request.Request(
        _RELEASES_API,
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Failed to resolve latest opencode release from GitHub API"
        ) from exc
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise RuntimeError(
            "GitHub releases payload missing tag_name for opencode"
        )
    return tag.lstrip("v")


def resolve_opencode_version() -> str:
    """The version to install in a guest.

    Prefers the host's own opencode version so guest and host stay in
    lockstep, and falls back to the latest published release when the host has
    no opencode (a Windows host running the HCS backend typically has none —
    its guest is the only place opencode runs).
    """
    version = _host_version()
    if version is not None:
        logger.info("Using host opencode version %s for guest install", version)
        return version
    version = _latest_release_version()
    logger.info("Using latest opencode release %s for guest install", version)
    return version


def opencode_download_url(version: str, arch_str: str) -> str:
    """URL of the Linux release archive; *arch_str* is ``x64`` or ``arm64``."""
    return _DOWNLOAD_TEMPLATE.format(version=version, arch=arch_str)
