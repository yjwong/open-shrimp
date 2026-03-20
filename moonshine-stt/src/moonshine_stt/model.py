"""Moonshine ONNX inference — V1 four-file pipeline.

Loads the four ONNX model files (preprocess, encode, uncached_decode,
cached_decode) and runs autoregressive greedy decoding.

Compatible with the sherpa-onnx / HuggingFace model layout::

    model_dir/
        preprocess.onnx
        encode.int8.onnx
        uncached_decode.int8.onnx
        cached_decode.int8.onnx
        tokens.txt
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

# Special token IDs.
_SOS = 1  # Start of sequence / BOS.
_EOS = 2  # End of sequence.

# Approximate tokens per second of audio (used for max-length estimate).
_TOKENS_PER_SECOND = 6


def _make_session(path: Path, num_threads: int = 1) -> ort.InferenceSession:
    """Create an ONNX Runtime session with sensible defaults."""
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = num_threads
    opts.intra_op_num_threads = num_threads
    opts.log_severity_level = 3  # Warnings only.
    return ort.InferenceSession(
        str(path), opts, providers=["CPUExecutionProvider"]
    )


class MoonshineModel:
    """Moonshine V1 four-file ONNX model."""

    def __init__(self, model_dir: str | Path, num_threads: int = 1) -> None:
        model_dir = Path(model_dir)
        logger.info("Loading Moonshine model from %s", model_dir)

        self.preprocess = _make_session(
            model_dir / "preprocess.onnx", num_threads
        )

        # Try int8 first, fall back to fp32.
        encode_path = model_dir / "encode.int8.onnx"
        if not encode_path.exists():
            encode_path = model_dir / "encode.onnx"
        self.encode = _make_session(encode_path, num_threads)

        uncached_path = model_dir / "uncached_decode.int8.onnx"
        if not uncached_path.exists():
            uncached_path = model_dir / "uncached_decode.onnx"
        self.uncached = _make_session(uncached_path, num_threads)

        cached_path = model_dir / "cached_decode.int8.onnx"
        if not cached_path.exists():
            cached_path = model_dir / "cached_decode.onnx"
        self.cached = _make_session(cached_path, num_threads)

        logger.info("Moonshine model loaded")

    def transcribe(self, audio: np.ndarray) -> list[int]:
        """Transcribe audio to a list of token IDs.

        Args:
            audio: Float32 array of shape ``(1, num_samples)`` at 16 kHz.

        Returns:
            List of token IDs (excluding SOS/EOS).
        """
        # 1. Preprocess: raw PCM -> features.
        features = self.preprocess.run(
            [self.preprocess.get_outputs()[0].name],
            {self.preprocess.get_inputs()[0].name: audio},
        )[0]

        # 2. Encode.
        enc_inputs = self.encode.get_inputs()
        feed: dict[str, np.ndarray] = {
            enc_inputs[0].name: features,
        }
        if len(enc_inputs) > 1:
            feed[enc_inputs[1].name] = np.array(
                [features.shape[1]], dtype=np.int32
            )
        enc_out = self.encode.run(
            [self.encode.get_outputs()[0].name], feed
        )[0]

        # 3. Decode: autoregressive greedy search.
        duration_s = audio.shape[1] / 16000
        max_len = int(duration_s * _TOKENS_PER_SECOND) + 10

        # First step — uncached decoder (no KV cache).
        outs = self.uncached.run(
            [o.name for o in self.uncached.get_outputs()],
            {
                self.uncached.get_inputs()[0].name: np.array(
                    [[_SOS]], dtype=np.int32
                ),
                self.uncached.get_inputs()[1].name: enc_out,
                self.uncached.get_inputs()[2].name: np.array(
                    [1], dtype=np.int32
                ),
            },
        )
        logits = outs[0]
        states = outs[1:]

        tokens: list[int] = []
        for _ in range(max_len):
            token = int(np.argmax(logits[0, -1]))
            if token == _EOS:
                break
            tokens.append(token)

            # Subsequent steps — cached decoder with KV cache.
            cached_inputs = self.cached.get_inputs()
            feed = {
                cached_inputs[0].name: np.array(
                    [[token]], dtype=np.int32
                ),
                cached_inputs[1].name: enc_out,
                cached_inputs[2].name: np.array(
                    [len(tokens) + 1], dtype=np.int32
                ),
            }
            for j in range(3, len(cached_inputs)):
                feed[cached_inputs[j].name] = states[j - 3]

            outs = self.cached.run(
                [o.name for o in self.cached.get_outputs()], feed
            )
            logits = outs[0]
            states = outs[1:]

        return tokens
