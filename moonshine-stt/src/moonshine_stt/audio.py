"""Audio decoding via PyAV.

Handles any format PyAV/FFmpeg supports (OGG/Opus, MP3, WAV, etc.)
and resamples to 16 kHz mono float32 PCM — what Moonshine expects.
"""

from __future__ import annotations

import numpy as np
import av


def decode_audio(path: str) -> np.ndarray:
    """Decode an audio file to 16 kHz mono float32 PCM.

    Args:
        path: Path to the audio file (OGG/Opus, WAV, MP3, etc.).

    Returns:
        numpy array of shape ``(1, num_samples)`` with float32 values
        in the ``[-1.0, 1.0]`` range.
    """
    container = av.open(path)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    frames: list[np.ndarray] = []
    for frame in container.decode(audio=0):
        for resampled in resampler.resample(frame):
            arr = resampled.to_ndarray().flatten()
            frames.append(arr)

    container.close()

    if not frames:
        raise ValueError(f"No audio frames decoded from {path}")

    pcm = np.concatenate(frames).astype(np.float32) / 32768.0
    return pcm.reshape(1, -1)
