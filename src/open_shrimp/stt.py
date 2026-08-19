"""Speech-to-text via the moonshine-stt binary.

Downloads the moonshine-stt binary on first use (same pattern as
cloudflared in tunnel.py) and shells out to it for transcription.  The
downloaded copy is the only one ever run: a moonshine-stt on ``$PATH`` is a
build of unknown vintage, and the model weights and JSON output shape it
would be invoked for are the downloaded build's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import tempfile
from pathlib import Path

from open_shrimp.binaries import (
    BIN_DIR,
    make_executable,
    managed_binary,
    managed_binary_path,
)

logger = logging.getLogger(__name__)

# GitHub release URL — uses the same repo as OpenShrimp.
_REPO = "yjwong/open-shrimp"
_DOWNLOAD_BASE = f"https://github.com/{_REPO}/releases/latest/download"

# Map (system, machine) to the moonshine-stt binary name on GitHub releases.
_BINARY_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "moonshine-stt-linux-x86_64",
    ("Linux", "aarch64"): "moonshine-stt-linux-aarch64",
    ("Darwin", "arm64"): "moonshine-stt-macos-aarch64",
    ("Darwin", "x86_64"): "moonshine-stt-macos-x86_64",
    ("Windows", "AMD64"): "moonshine-stt-windows-x86_64.exe",
}


def managed_moonshine_stt() -> Path:
    """Path of the one moonshine-stt this project will run."""
    return managed_binary_path("moonshine-stt")


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

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = managed_moonshine_stt()
    url = f"{_DOWNLOAD_BASE}/{binary_name}"

    logger.info("Downloading moonshine-stt from %s ...", url)

    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            tmp = target.with_name(target.name + ".tmp")
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                # Not Path.rename: that refuses an existing destination on
                # Windows, so a re-download would fail.
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

    make_executable(target)
    logger.info("moonshine-stt downloaded to %s", target)
    return str(target)


async def ensure_moonshine_stt() -> str:
    """Ensure the managed moonshine-stt is present, downloading if not.

    Returns the path to the moonshine-stt binary.

    Raises:
        RuntimeError: If moonshine-stt cannot be downloaded.
    """
    path = managed_binary("moonshine-stt")
    if path:
        logger.info("Using moonshine-stt at %s", path)
        return path

    logger.info(
        "moonshine-stt not present at %s, downloading...", managed_moonshine_stt()
    )
    return await _download_moonshine_stt()


async def transcribe(audio_data: bytes) -> str:
    """Transcribe audio data (OGG/Opus) to text.

    Writes the audio bytes to a temp file, invokes moonshine-stt, and
    returns the transcribed text.

    Args:
        audio_data: Raw audio file bytes (OGG/Opus from Telegram).

    Returns:
        Transcribed text string.

    Raises:
        RuntimeError: If transcription fails.
    """
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
