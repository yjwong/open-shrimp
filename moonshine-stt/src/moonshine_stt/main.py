"""CLI entry point for moonshine-stt."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .audio import decode_audio
from .download import get_model_dir
from .model import MoonshineModel
from .tokenizer import Tokenizer

logger = logging.getLogger(__name__)


def _cmd_transcribe(args: argparse.Namespace) -> None:
    """Transcribe one or more audio files."""
    # Resolve model directory — download if needed.
    if args.model_dir:
        model_dir = Path(args.model_dir)
    else:
        model_dir = get_model_dir(args.model)

    model = MoonshineModel(model_dir, num_threads=args.threads)
    tokenizer = Tokenizer(model_dir / "tokens.txt")

    for audio_path in args.files:
        audio = decode_audio(audio_path)
        token_ids = model.transcribe(audio)
        text = tokenizer.decode(token_ids)
        duration = audio.shape[1] / 16000

        result = {
            "file": audio_path,
            "text": text,
            "duration": round(duration, 2),
        }
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.stdout.flush()


def _cmd_download(args: argparse.Namespace) -> None:
    """Download model files."""
    model_dir = get_model_dir(args.model, cache_dir=args.cache_dir)
    print(f"Model ready at: {model_dir}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="moonshine-stt",
        description="Speech-to-text using Moonshine ONNX models.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )

    sub = parser.add_subparsers(dest="command")

    # -- transcribe --
    tx = sub.add_parser(
        "transcribe",
        help="Transcribe audio files to text.",
    )
    tx.add_argument(
        "files",
        nargs="+",
        help="Audio files to transcribe (OGG, WAV, MP3, etc.).",
    )
    tx.add_argument(
        "--model",
        default="tiny",
        choices=["tiny", "base"],
        help="Model size (default: tiny). Ignored if --model-dir is set.",
    )
    tx.add_argument(
        "--model-dir",
        default=None,
        help="Path to a directory containing ONNX model files.",
    )
    tx.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of inference threads (default: 1).",
    )

    # -- download --
    dl = sub.add_parser(
        "download",
        help="Download model files ahead of time.",
    )
    dl.add_argument(
        "--model",
        default="tiny",
        choices=["tiny", "base"],
        help="Model to download (default: tiny).",
    )
    dl.add_argument(
        "--cache-dir",
        default=None,
        help="Override the default model cache directory.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if args.command == "transcribe":
        _cmd_transcribe(args)
    elif args.command == "download":
        _cmd_download(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
