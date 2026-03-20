"""Moonshine tokenizer — maps token IDs back to text.

Reads the ``tokens.txt`` file shipped with Moonshine ONNX models.
Each line has the format ``<token_string>\\t<token_id>``.
"""

from __future__ import annotations

from pathlib import Path


class Tokenizer:
    """Simple ID-to-token lookup built from ``tokens.txt``."""

    def __init__(self, tokens_path: str | Path) -> None:
        self.id2token: dict[int, str] = {}
        with open(tokens_path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t", maxsplit=1)
                if len(parts) == 2:
                    token, idx = parts
                    self.id2token[int(idx)] = token

    def decode(self, ids: list[int]) -> str:
        """Decode a list of token IDs into a string."""
        text = "".join(self.id2token.get(i, "") for i in ids)
        # Moonshine uses U+2581 (LOWER ONE EIGHTH BLOCK) as the word separator.
        return text.replace("\u2581", " ").strip()
