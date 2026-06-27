"""Security-key VM helper binary resolution and install helpers."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import urllib.error
import urllib.request
from pathlib import Path

from platformdirs import user_data_path

logger = logging.getLogger(__name__)

BINARY_NAME = "openshrimp-security-key-vm-helper"

_BIN_DIR = user_data_path("openshrimp") / "bin"
_REPO = "yjwong/open-shrimp"
_DOWNLOAD_BASE = f"https://github.com/{_REPO}/releases/latest/download"

_LINUX_ARCH_ASSETS = {
    "x86_64": f"{BINARY_NAME}-linux-x86_64",
    "amd64": f"{BINARY_NAME}-linux-x86_64",
    "x64": f"{BINARY_NAME}-linux-x86_64",
    "aarch64": f"{BINARY_NAME}-linux-aarch64",
    "arm64": f"{BINARY_NAME}-linux-aarch64",
}


def asset_name_for_linux_arch(machine: str) -> str:
    """Return the release asset name for a Linux guest architecture."""
    normalized = machine.strip().lower()
    asset = _LINUX_ARCH_ASSETS.get(normalized)
    if asset is None:
        raise RuntimeError(
            f"Unsupported Linux architecture for {BINARY_NAME}: {machine}"
        )
    return asset


def download_url_for_linux_arch(machine: str) -> str:
    """Return the latest-release download URL for a Linux guest architecture."""
    return f"{_DOWNLOAD_BASE}/{asset_name_for_linux_arch(machine)}"


def _cached_path(machine: str) -> Path:
    return _BIN_DIR / asset_name_for_linux_arch(machine)


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_security_key_vm_helper(machine: str | None = None) -> str | None:
    """Find a host-side helper binary suitable for *machine* if available."""
    arch = machine or platform.machine()
    cached = _cached_path(arch)
    if _executable(cached):
        return str(cached)

    generic_cached = _BIN_DIR / BINARY_NAME
    if machine is None and _executable(generic_cached):
        return str(generic_cached)

    path = shutil.which(BINARY_NAME)
    if machine is None and path:
        return path
    return None


def ensure_security_key_vm_helper(machine: str | None = None) -> str:
    """Ensure a host-side Linux helper binary exists and return its path.

    Normal end-user flow downloads a prebuilt GitHub release asset.  Existing
    cached or PATH binaries are reused for development and packaged installs.
    """
    existing = find_security_key_vm_helper(machine)
    if existing is not None:
        logger.info("Found %s at %s", BINARY_NAME, existing)
        return existing

    arch = machine or platform.machine()
    target = _cached_path(arch)
    url = download_url_for_linux_arch(arch)
    target.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading %s from %s ...", BINARY_NAME, url)
    tmp = target.with_suffix(".tmp")
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
        tmp.rename(target)
    except (OSError, urllib.error.URLError) as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download {BINARY_NAME} from {url}") from exc

    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("%s downloaded to %s", BINARY_NAME, target)
    return str(target)


def install_cmd_for_linux_guest(download_url: str) -> str:
    """Return a shell command that installs the helper inside a Linux guest."""
    import shlex

    tmp_path = f"/tmp/{BINARY_NAME}"
    return (
        f"curl -fsSL {shlex.quote(download_url)} -o {shlex.quote(tmp_path)} "
        f"&& sudo install -m 755 {shlex.quote(tmp_path)} "
        f"/usr/local/bin/{BINARY_NAME} "
        f"&& rm -f {shlex.quote(tmp_path)}"
    )
