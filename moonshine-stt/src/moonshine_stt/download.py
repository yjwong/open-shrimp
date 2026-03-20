"""Model download and management.

Downloads Moonshine ONNX model files from sherpa-onnx GitHub releases
on first use and caches them locally.
"""

from __future__ import annotations

import io
import logging
import tarfile
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".config" / "moonshine-stt" / "models"

# sherpa-onnx provides pre-quantized models with tokens.txt included.
_SHERPA_MODEL_BASE = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
)
_SHERPA_MODELS: dict[str, str] = {
    "tiny": "sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
    "base": "sherpa-onnx-moonshine-base-en-int8.tar.bz2",
}


def get_model_dir(
    model_name: str = "tiny",
    cache_dir: str | Path | None = None,
) -> Path:
    """Return the local directory for the given model, downloading if needed.

    Args:
        model_name: ``"tiny"`` or ``"base"``.
        cache_dir: Override the default cache directory.

    Returns:
        Path to the model directory containing the ONNX files and
        ``tokens.txt``.
    """
    cache = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
    model_dir = cache / model_name

    # Check if already downloaded — tokens.txt is the last file written.
    if (model_dir / "tokens.txt").exists():
        return model_dir

    return _download_sherpa_model(model_name, model_dir)


def _download_sherpa_model(model_name: str, dest: Path) -> Path:
    """Download a pre-packaged model from sherpa-onnx releases.

    The archive includes ``tokens.txt`` and int8-quantized models
    ready to use.
    """
    archive_name = _SHERPA_MODELS.get(model_name)
    if archive_name is None:
        raise ValueError(
            f"Unknown model {model_name!r}. Choose from: {list(_SHERPA_MODELS)}"
        )

    url = f"{_SHERPA_MODEL_BASE}/{archive_name}"
    logger.info("Downloading %s model from %s ...", model_name, url)

    dest.mkdir(parents=True, exist_ok=True)

    # Download the tar.bz2 archive into memory.
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()

    logger.info("Extracting model files to %s ...", dest)

    # The archive contains a top-level directory; we flatten into dest.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tar:
        # Find the common prefix (top-level dir in the archive).
        members = tar.getmembers()
        prefix = ""
        for m in members:
            if m.isdir() and m.name.count("/") == 0:
                prefix = m.name + "/"
                break

        for member in members:
            if member.isdir():
                continue
            # Strip the top-level directory prefix.
            name = member.name
            if prefix and name.startswith(prefix):
                name = name[len(prefix):]
            if not name:
                continue

            # Only extract model files and tokens.txt, skip test wavs etc.
            if name.endswith(".onnx") or name == "tokens.txt":
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(member)
                if f is not None:
                    target.write_bytes(f.read())
                    logger.info("  %s", name)

    logger.info("Model %s ready at %s", model_name, dest)
    return dest
