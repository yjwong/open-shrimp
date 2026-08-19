"""Cloudflared tunnel management for the review app.

The tunnel runs on a cloudflared this project downloads and owns, reading a
config this project writes.  A cloudflared already installed on the machine
is never used and the operator's own cloudflared settings never apply: both
carry a version, an autoupdate policy, log destinations and ingress rules
outside this project's control, and a throwaway quick tunnel to the review
app has nothing to gain from inheriting any of them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from pathlib import Path

from open_shrimp.binaries import (
    BIN_DIR,
    make_executable,
    managed_binary,
    managed_binary_path,
)

logger = logging.getLogger(__name__)

# GitHub release URL template for cloudflared binaries.
_DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# Map (system, machine) to the cloudflared binary name on GitHub releases.
_BINARY_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "cloudflared-linux-amd64",
    ("Linux", "aarch64"): "cloudflared-linux-arm64",
    ("Linux", "armv7l"): "cloudflared-linux-arm",
    ("Darwin", "x86_64"): "cloudflared-darwin-amd64.tgz",
    ("Darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
    ("Windows", "AMD64"): "cloudflared-windows-amd64.exe",
}


def _get_binary_name() -> str | None:
    """Return the cloudflared release binary name for this platform."""
    system = platform.system()
    machine = platform.machine()
    return _BINARY_MAP.get((system, machine))


# The config cloudflared is pointed at.  Not instance-scoped, for the same
# reason the binary is not: its contents never vary.
CONFIG_PATH = BIN_DIR.parent / "cloudflared.yml"

# A quick tunnel needs no settings at all, so this file exists only to
# occupy --config and displace the operator's.  It cannot be empty —
# cloudflared logs an error for an empty config — and disabling autoupdate
# is the one setting worth pinning: the binary is ours to replace.
_CONFIG_BODY = "no-autoupdate: true\n"

# Every environment variable the cloudflared `tunnel` command reads is
# either prefixed or named here, and all of them are the operator's rather
# than ours — including ones --config cannot override, such as
# TUNNEL_LOGFILE, which would divert the output the URL is parsed from.
_ENV_PREFIX = "TUNNEL_"
_ENV_NAMES = frozenset({"NO_AUTOUPDATE", "NO_TLS_VERIFY"})


def managed_cloudflared() -> Path:
    """Path of the one cloudflared this project will run."""
    return managed_binary_path("cloudflared")


def _write_config() -> Path:
    """Write the config cloudflared is run against, and return its path."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_CONFIG_BODY)
    return CONFIG_PATH


def _tunnel_env() -> dict[str, str]:
    """The environment for cloudflared, with its own settings stripped."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_ENV_PREFIX) and key not in _ENV_NAMES
    }


async def _download_cloudflared() -> str:
    """Download the cloudflared binary for this platform.

    Returns the path to the downloaded binary.

    Raises:
        RuntimeError: If the platform is unsupported or download fails.
    """
    binary_name = _get_binary_name()
    if binary_name is None:
        raise RuntimeError(
            f"Unsupported platform for cloudflared auto-download: "
            f"{platform.system()} {platform.machine()}. "
            f"Please install cloudflared manually: "
            f"https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        )

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = managed_cloudflared()
    url = f"{_DOWNLOAD_BASE}/{binary_name}"

    logger.info("Downloading cloudflared from %s ...", url)

    if binary_name.endswith(".tgz"):
        # macOS ships as a tarball.
        await _download_and_extract_tgz(url, target)
    else:
        await _download_file(url, target)

    make_executable(target)
    logger.info("cloudflared downloaded to %s", target)
    return str(target)


async def _download_file(url: str, dest: Path) -> None:
    """Download a file from a URL to a local path using httpx."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            tmp = dest.with_name(dest.name + ".tmp")
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                # Not Path.rename: that refuses an existing destination on
                # Windows, so a re-download would fail.
                os.replace(tmp, dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise


async def _download_and_extract_tgz(url: str, dest: Path) -> None:
    """Download a .tgz and extract the cloudflared binary from it."""
    import httpx
    import tarfile
    import tempfile

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name

    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            # Find the cloudflared binary in the archive.
            for member in tar.getmembers():
                if member.name.endswith("cloudflared") or member.name == "cloudflared":
                    f = tar.extractfile(member)
                    if f is not None:
                        with open(dest, "wb") as out:
                            out.write(f.read())
                        return
            raise RuntimeError(
                "cloudflared binary not found in downloaded archive"
            )
    finally:
        os.unlink(tmp_path)


async def ensure_cloudflared() -> str:
    """Ensure the managed cloudflared is present, downloading if not.

    Returns the path to the cloudflared binary.

    Raises:
        RuntimeError: If cloudflared cannot be downloaded.
    """
    path = managed_binary("cloudflared")
    if path:
        logger.info("Using cloudflared at %s", path)
        return path

    logger.info(
        "cloudflared not present at %s, downloading...", managed_cloudflared()
    )
    return await _download_cloudflared()


async def start_tunnel(port: int) -> tuple[asyncio.subprocess.Process, str]:
    """Start a cloudflared quick tunnel pointing to the given port.

    Args:
        port: Local port the HTTP server is listening on.

    Returns:
        (process, public_url) — the subprocess handle and the assigned
        trycloudflare.com URL.

    Raises:
        RuntimeError: If cloudflared cannot be started or URL cannot be
            parsed from output.
    """
    cloudflared_path = await ensure_cloudflared()

    logger.info(
        "Starting cloudflared tunnel to http://localhost:%d ...", port
    )

    proc = await asyncio.create_subprocess_exec(
        cloudflared_path,
        "tunnel",
        "--config",
        str(_write_config()),
        "--url",
        f"http://localhost:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_tunnel_env(),
    )

    # cloudflared prints the assigned URL to stderr.  We need to read
    # stderr lines until we find it, with a timeout.
    url = await _parse_tunnel_url(proc, timeout=30.0)

    logger.info("Cloudflared tunnel active: %s", url)
    return proc, url


async def _parse_tunnel_url(
    proc: asyncio.subprocess.Process, timeout: float = 30.0
) -> str:
    """Read cloudflared stderr until the tunnel URL appears.

    The URL line looks like:
        ... | https://xxx-yyy-zzz.trycloudflare.com ...

    Raises:
        RuntimeError: If the URL is not found within the timeout or the
            process exits prematurely.
    """
    import re

    url_pattern = re.compile(r"(https://[a-zA-Z0-9_-]+\.trycloudflare\.com)")

    assert proc.stderr is not None

    try:
        async with asyncio.timeout(timeout):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    # Process exited.
                    exit_code = await proc.wait()
                    raise RuntimeError(
                        f"cloudflared exited with code {exit_code} "
                        f"before printing a tunnel URL"
                    )
                decoded = line.decode("utf-8", errors="replace").strip()
                logger.debug("cloudflared: %s", decoded)

                match = url_pattern.search(decoded)
                if match:
                    return match.group(1)
    except TimeoutError:
        proc.terminate()
        raise RuntimeError(
            f"Timed out after {timeout}s waiting for cloudflared tunnel URL"
        )


async def stop_tunnel(proc: asyncio.subprocess.Process) -> None:
    """Gracefully stop a cloudflared tunnel process."""
    if proc.returncode is not None:
        return  # Already exited.

    logger.info("Stopping cloudflared tunnel...")
    proc.terminate()
    try:
        async with asyncio.timeout(10.0):
            await proc.wait()
    except TimeoutError:
        logger.warning("cloudflared did not exit cleanly, killing...")
        proc.kill()
        await proc.wait()

    logger.info("cloudflared tunnel stopped")
