"""Speech-to-text via the moonshine-stt binary.

Downloads the moonshine-stt binary on first use (same pattern as
cloudflared in tunnel.py) and shells out to it for transcription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import stat
import tempfile
from pathlib import Path

from platformdirs import user_data_path

logger = logging.getLogger(__name__)

# Directory where we download moonshine-stt if not found in $PATH.
_BIN_DIR = user_data_path("openshrimp") / "bin"

# GitHub release URL — uses the same repo as OpenShrimp.
_REPO = "yjwong/open-shrimp"
_DOWNLOAD_BASE = f"https://github.com/{_REPO}/releases/latest/download"

# Map (system, machine) to the moonshine-stt binary name on GitHub releases.
_BINARY_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "moonshine-stt-linux-x86_64",
    ("Linux", "aarch64"): "moonshine-stt-linux-aarch64",
    ("Darwin", "arm64"): "moonshine-stt-macos-aarch64",
}


def _find_moonshine_stt() -> str | None:
    """Find the moonshine-stt binary, checking our bin dir first, then $PATH."""
    local_bin = _BIN_DIR / "moonshine-stt"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    import shutil

    path = shutil.which("moonshine-stt")
    if path:
        return path

    return None


async def _download_moonshine_stt() -> str:
    """Download the moonshine-stt binary for this platform.

    Returns the path to the downloaded binary.

    Raises:
        RuntimeError: If the platform is unsupported or download fails.
    """
    system = platform.system()
    machine = platform.machine()
    binary_name = _BINARY_MAP.get((system, machine))
    if binary_name is None:
        raise RuntimeError(
            f"Unsupported platform for moonshine-stt auto-download: "
            f"{system} {machine}. "
            f"Please build moonshine-stt manually from the moonshine-stt/ "
            f"directory in the open-shrimp repository."
        )

    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = _BIN_DIR / "moonshine-stt"
    url = f"{_DOWNLOAD_BASE}/{binary_name}"

    logger.info("Downloading moonshine-stt from %s ...", url)

    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            tmp = target.with_suffix(".tmp")
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                tmp.rename(target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

    # Make executable.
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("moonshine-stt downloaded to %s", target)
    return str(target)


async def ensure_moonshine_stt() -> str:
    """Ensure moonshine-stt is available, downloading if necessary.

    Returns the path to the moonshine-stt binary.

    Raises:
        RuntimeError: If moonshine-stt cannot be found or downloaded.
    """
    path = _find_moonshine_stt()
    if path:
        logger.info("Found moonshine-stt at %s", path)
        return path

    logger.info("moonshine-stt not found, attempting auto-download...")
    return await _download_moonshine_stt()


async def transcribe(audio_data: bytes, binary_path: str | None = None) -> str:
    """Transcribe audio data (OGG/Opus) to text.

    Writes the audio bytes to a temp file, invokes moonshine-stt, and
    returns the transcribed text.

    Args:
        audio_data: Raw audio file bytes (OGG/Opus from Telegram).
        binary_path: Path to the moonshine-stt binary.  If None, will
            be located/downloaded automatically.

    Returns:
        Transcribed text string.

    Raises:
        RuntimeError: If transcription fails.
    """
    if binary_path is None:
        binary_path = await ensure_moonshine_stt()

    # Write audio to a temp file.
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_data)
        tmp_path = tmp.name

    try:
        proc = await asyncio.create_subprocess_exec(
            binary_path, "transcribe", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"moonshine-stt failed (exit {proc.returncode}): {err_msg}"
            )

        # Parse JSON output (one line per file).
        output = stdout.decode("utf-8").strip()
        if not output:
            raise RuntimeError("moonshine-stt returned empty output")

        result = json.loads(output)
        text = result.get("text", "").strip()
        duration = result.get("duration", 0)
        logger.info(
            "Transcribed %.1fs of audio: %s",
            duration, text[:100] + ("..." if len(text) > 100 else ""),
        )
        return text

    finally:
        os.unlink(tmp_path)
