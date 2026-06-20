"""Lima in-VM installer for the OpenCode CLI binary.

Downloads the right Linux binary inside the VM from the
``anomalyco/opencode`` GitHub Releases archive, keyed on the guest's
``uname -m``.  Version selection prefers the host's opencode version
when available (keeps guest in lockstep with host) and falls back to the
GitHub ``releases/latest`` tag otherwise.

For Linux guests on common arches the unqualified
``opencode-linux-{arch}.tar.gz`` asset is used.  ``-baseline`` (CPUs
without AVX2) and ``-musl`` (Alpine/musl guests) variants exist but are
only needed in exotic configurations and are not pre-detected.
"""

from __future__ import annotations

import json
import logging
import shlex
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


def _get_host_opencode_version() -> str | None:
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


def _get_latest_release_version() -> str:
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


def _resolve_opencode_version() -> str:
    version = _get_host_opencode_version()
    if version is not None:
        logger.info("Using host opencode version %s for Lima install", version)
        return version
    version = _get_latest_release_version()
    logger.info("Using latest opencode release %s for Lima install", version)
    return version


def _opencode_download_url(version: str, arch_str: str) -> str:
    return _DOWNLOAD_TEMPLATE.format(version=version, arch=arch_str)


def _opencode_install_cmd(download_url: str) -> str:
    return (
        f"curl -fsSL {shlex.quote(download_url)} -o /tmp/opencode.tar.gz "
        f"&& tar -xzf /tmp/opencode.tar.gz -C /tmp/ "
        f"&& sudo install -m 755 /tmp/opencode /usr/local/bin/opencode "
        f"&& rm -f /tmp/opencode.tar.gz /tmp/opencode"
    )


def ensure_opencode_cli_in_vm(
    limactl: str,
    inst_name: str,
    guest_os: str = "linux",
) -> None:
    """Ensure the OpenCode CLI binary is installed inside the Lima VM."""
    if guest_os != "linux":
        logger.info(
            "OpenCode Lima install only supports linux guests; skipping "
            "guest_os=%s", guest_os,
        )
        return

    from open_shrimp.sandbox.lima_helpers import install_cli_in_linux_vm

    install_cli_in_linux_vm(
        limactl,
        inst_name,
        "opencode",
        url_for=_opencode_download_url,
        version_resolver=_resolve_opencode_version,
        install_cmd_for=_opencode_install_cmd,
        timeout=300,
    )
