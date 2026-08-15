"""Tests for the GFM to Telegram MarkdownV2 converter.

The load-bearing property throughout: Telegram consumes a backslash as an
escape character, so every literal backslash we want the user to see has to
leave here doubled.  These tests assert on what Telegram *renders*, by undoing
the MarkdownV2 escaping the same way Telegram's parser does.
"""

from __future__ import annotations

import re

import pytest

from open_shrimp.markdown import (
    TELEGRAM_MAX_LENGTH,
    escape,
    escape_code,
    gfm_to_telegram,
)

# Telegram drops the backslash of any "\x" pair and takes x literally.
_UNESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def render(markdown_v2: str) -> str:
    """Approximate what Telegram displays for a MarkdownV2 string.

    Only models escape handling, which is all these tests care about; markup
    characters are left in place.
    """
    return _UNESCAPE_RE.sub(r"\1", markdown_v2)


def render_all(text: str) -> str:
    return "".join(render(chunk) for chunk in gfm_to_telegram(text))


def test_escape_doubles_backslash() -> None:
    assert escape("a\\b") == "a\\\\b"


def test_escape_still_escapes_markup() -> None:
    assert escape("a_b*c") == "a\\_b\\*c"


def test_escape_is_single_pass() -> None:
    """A backslash added for a markup character must not itself be escaped."""
    assert escape("*") == "\\*"
    assert render(escape("*")) == "*"


@pytest.mark.parametrize(
    "text",
    [
        "C:\\Users\\me",
        "regex \\d+ and \\w",
        "\\frac{a}{b}",
        "a lone backslash: \\ here",
        "trailing backslash\\",
    ],
)
def test_backslashes_survive_prose(text: str) -> None:
    assert render_all(text) == text


def test_backslash_survives_a_code_span() -> None:
    assert 're.match(r"\\d+")' in render_all('`re.match(r"\\d+")`')


def test_backslash_survives_a_code_block() -> None:
    out = render_all('```python\nprint("a\\\\nb")\n```')
    assert 'print("a\\\\nb")' in out


def test_backtick_inside_a_code_span_is_escaped() -> None:
    """An unescaped backtick would close the entity early."""
    chunks = gfm_to_telegram("``a ` b``")
    assert "\\`" in chunks[0]
    assert render_all("``a ` b``") == "`a ` b`"


def test_escape_code_leaves_markup_characters_alone() -> None:
    """Over-escaping inside code would show the backslashes verbatim."""
    assert escape_code("a_b*c.d") == "a_b*c.d"
    assert escape_code("a\\b`c") == "a\\\\b\\`c"


def test_table_columns_stay_aligned_despite_escaping() -> None:
    """Widths are measured before escaping, so the rendered table lines up."""
    table = "| a | b |\n| --- | --- |\n| x\\y | z |\n| q | w |\n"
    lines = render_all(table).strip().strip("`").strip().split("\n")
    assert len({len(line) for line in lines}) == 1
    assert "x\\y" in lines[2]


def test_link_url_backslash_is_percent_encoded() -> None:
    """mistune encodes it before we see it, so no escape leaks into the URL."""
    assert gfm_to_telegram("[t](http://e.com/a\\b)")[0] == "[t](http://e.com/a%5Cb)"


def test_link_text_backslash_survives() -> None:
    assert render_all("[a\\b](http://e.com)") == "[a\\b](http://e.com)"


def test_no_chunk_ends_mid_escape() -> None:
    """A trailing lone backslash is an unterminated escape; Telegram 400s."""
    text = ("word\\ " * 4000).strip()
    chunks = gfm_to_telegram(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MAX_LENGTH
        assert len(chunk) - len(chunk.rstrip("\\")) != 1


def test_split_preserves_every_backslash() -> None:
    line = "path C:\\Users\\me\\file\n"
    text = line * 500
    chunks = gfm_to_telegram(text)
    assert len(chunks) > 1
    assert render_all(text).count("C:\\Users\\me\\file") == 500


def test_backslash_run_splitting_terminates() -> None:
    """Backing off a split point must never stall the loop."""
    chunks = gfm_to_telegram("\\" * 6000)
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)


def test_code_block_fence_detection_survives_escaped_backticks() -> None:
    """Escaped backticks in a code body must not read as a fence."""
    body = "\n".join(f"line {i} with ` tick" for i in range(400))
    chunks = gfm_to_telegram(f"```\n{body}\n```")
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
