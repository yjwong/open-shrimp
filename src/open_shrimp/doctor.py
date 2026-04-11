"""Detect common issues with optional components.

Run via ``openshrimp doctor`` to check that optional dependencies
(moonshine-stt, cloudflared, Docker, libvirt, virtiofsd, Lima, etc.)
are available and functional.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys

from platformdirs import user_data_path

_BIN_DIR = user_data_path("openshrimp") / "bin"


def _find_managed_or_path(name: str) -> str | None:
    """Check managed bin dir then $PATH for *name*."""
    local_bin = _BIN_DIR / name
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return shutil.which(name)


def _check_moonshine_stt() -> tuple[bool, str]:
    path = _find_managed_or_path("moonshine-stt")
    if path:
        return True, f"found at {path}"
    return False, "not found (voice transcription unavailable)"


def _check_cloudflared() -> tuple[bool, str]:
    path = _find_managed_or_path("cloudflared")
    if path:
        return True, f"found at {path}"
    return False, "not found (tunnel support unavailable)"


def _check_docker() -> tuple[bool, str]:
    path = shutil.which("docker")
    if not path:
        return False, "docker CLI not found"
    # Check if daemon is responsive.
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, "docker CLI found but daemon is not running"
    except subprocess.TimeoutExpired:
        return False, "docker CLI found but daemon timed out"
    except Exception as e:
        return False, f"docker CLI found but check failed: {e}"
    return True, f"found at {path}, daemon running"


def _check_libvirt() -> tuple[bool, str]:
    try:
        import libvirt  # type: ignore[import-untyped]
    except ImportError:
        return False, "libvirt-python not installed"

    try:
        conn = libvirt.open("qemu:///session")
        conn.close()
    except Exception as e:
        return False, f"libvirt-python installed but cannot connect: {e}"
    return True, "libvirt-python installed, qemu:///session reachable"


def _check_virtiofsd() -> tuple[bool, str]:
    # Re-use the same logic from libvirt_helpers.
    from open_shrimp.sandbox.libvirt_helpers import find_virtiofsd

    path = find_virtiofsd()
    if path:
        return True, f"found at {path}"
    return False, "not found (required for libvirt sandbox)"


def _check_lima() -> tuple[bool, str]:
    path = _find_managed_or_path("limactl")
    if path:
        return True, f"found at {path}"
    return False, "not found (required for Lima sandbox on macOS)"


# Each check: (label, function, platform filter or None for all).
_CHECKS: list[tuple[str, callable, str | None]] = [
    ("moonshine-stt", _check_moonshine_stt, None),
    ("cloudflared", _check_cloudflared, None),
    ("Docker", _check_docker, "Linux"),
    ("libvirt", _check_libvirt, "Linux"),
    ("virtiofsd", _check_virtiofsd, "Linux"),
    ("Lima", _check_lima, "Darwin"),
]


def run_doctor() -> int:
    """Run all checks and print results. Returns 0 if all pass, 1 otherwise."""
    current_platform = platform.system()
    has_failure = False

    for label, check_fn, plat in _CHECKS:
        if plat is not None and current_platform != plat:
            continue
        ok, detail = check_fn()
        icon = "\u2705" if ok else "\u274c"
        print(f"  {icon} {label}: {detail}")
        if not ok:
            has_failure = True

    return 1 if has_failure else 0
