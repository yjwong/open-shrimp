"""Tests for the GFM to rich-message Markdown converter.

The load-bearing property throughout: Telegram's rich parser reads both
Markdown and HTML, so "<" has to leave as an entity and every markup character
mistune did *not* claim has to leave backslash-escaped.  These tests assert on
what Telegram *renders*, by undoing the escaping the same way its parser does —
verified against the live API, which echoes its own parse back.
"""

from __future__ import annotations

import re

import pytest

from open_shrimp.markdown import (
    RICH_MAX_LENGTH,
    escape_rich,
    escape_rich_inline,
    gfm_to_rich,
    rich_code_block,
    rich_details,
)

# Telegram drops the backslash of any "\x" pair and takes x literally.
_UNESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def render(rich: str) -> str:
    """Approximate what Telegram displays for a rich body.

    Only models escape and entity handling, which is all these tests care
    about; markup characters are left in place.
    """
    text = _UNESCAPE_RE.sub(r"\1", rich.replace("<br>", "\n"))
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text


def render_all(text: str) -> str:
    return "".join(render(chunk) for chunk in gfm_to_rich(text))


# ── Backslashes ──
#
# A Windows path, a regex and a LaTeX fragment are all ordinary agent output,
# and all of them are mostly the characters the escaper touches.


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
    # Inside a span the wire form *is* the rendered form: a rich message takes
    # a code entity literally, so nothing is escaped and nothing is undone.
    assert 're.match(r"\\d+")' in "".join(gfm_to_rich('`re.match(r"\\d+")`'))


def test_backslash_survives_a_code_block() -> None:
    out = "".join(gfm_to_rich('```python\nprint("a\\\\nb")\n```'))
    assert 'print("a\\\\nb")' in out


def test_no_chunk_ends_mid_escape() -> None:
    """A trailing lone backslash is an unterminated escape; Telegram 400s."""
    text = ("word\\ " * 20000).strip()
    chunks = gfm_to_rich(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= RICH_MAX_LENGTH
        assert len(chunk) - len(chunk.rstrip("\\")) != 1


def test_split_preserves_every_backslash() -> None:
    line = "path C:\\Users\\me\\file\n"
    text = line * 4000
    chunks = gfm_to_rich(text)
    assert len(chunks) > 1
    assert render_all(text).count("C:\\Users\\me\\file") == 4000


def test_backslash_run_splitting_terminates() -> None:
    """Backing off a split point must never stall the loop."""
    chunks = gfm_to_rich("\\" * 40000)
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)


def test_code_block_fence_detection_survives_backticks() -> None:
    """A backtick in a code body must not read as a fence."""
    body = "\n".join(f"line {i} with ` tick" for i in range(3000))
    chunks = gfm_to_rich(f"```\n{body}\n```")
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_link_url_backslash_is_percent_encoded() -> None:
    """mistune encodes it before we see it, so no escape leaks into the URL."""
    assert gfm_to_rich("[t](http://e.com/a\\b)")[0] == "[t](http://e.com/a%5Cb)"


def test_link_text_backslash_survives() -> None:
    assert render_all("[a\\b](http://e.com)") == "[a\\b](http://e.com)"


def test_angle_brackets_survive_as_entities() -> None:
    """An unescaped "<" opens a tag and eats through the next ">"."""
    assert "".join(gfm_to_rich("Generic<T> List<int>")) == (
        "Generic&lt;T&gt; List&lt;int&gt;"
    )


def test_script_tag_keeps_its_body() -> None:
    out = "".join(gfm_to_rich("<script>alert(1)</script>"))
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_ampersand_is_escaped() -> None:
    assert "".join(gfm_to_rich("A & B")) == "A &amp; B"


def test_stray_asterisks_are_escaped() -> None:
    """Telegram italicises the 3 in "5 * 3 * 2"; mistune leaves it as text."""
    assert "".join(gfm_to_rich("5 * 3 * 2")) == "5 \\* 3 \\* 2"


def test_dunder_stays_bold_and_snake_case_does_not() -> None:
    out = "".join(gfm_to_rich("__dunder__ and snake_case_name"))
    assert out == "**dunder** and snake\\_case\\_name"


def test_blockquote_lines_get_an_explicit_break() -> None:
    """Newlines inside a blockquote collapse without one."""
    assert "".join(gfm_to_rich(">line one\n>line two")) == (
        "> line one<br>line two"
    )


def test_unclosed_fence_is_closed() -> None:
    assert "".join(gfm_to_rich("```\nunclosed")) == "```\nunclosed\n```"


def test_code_fence_content_is_not_escaped() -> None:
    """A fence is taken literally, so escaping there would show the entities."""
    out = "".join(gfm_to_rich('```python\nprint("a<b & c")\n```'))
    assert 'print("a<b & c")' in out


def test_fence_grows_past_backticks_in_the_body() -> None:
    out = "".join(gfm_to_rich("    ```\n    inner\n    ```"))
    assert out.startswith("````")


def test_table_keeps_its_alignment() -> None:
    out = "".join(gfm_to_rich("| a | b |\n|:--|--:|\n| 1 | 2 |"))
    assert out == "| a | b |\n| :--- | ---: |\n| 1 | 2 |"


def test_task_list_survives() -> None:
    out = "".join(gfm_to_rich("- [x] done\n- [ ] todo"))
    assert out == "- [x] done\n- [ ] todo"


def test_nested_list_stays_nested() -> None:
    out = "".join(gfm_to_rich("- plain\n  - nested"))
    assert out == "- plain\n  - nested"


def test_headings_are_clamped_to_h3() -> None:
    """h1 and h2 are centred on web and left-aligned on Android."""
    assert "".join(gfm_to_rich("# Big")) == "### Big"


def test_link_parens_are_percent_encoded() -> None:
    out = "".join(gfm_to_rich("[t](http://e.com/a(b)"))
    assert "http://e.com/a%28b" in out


def test_rich_chunks_use_the_larger_ceiling() -> None:
    text = "word " * 2000
    assert len(gfm_to_rich(text)) == 1
    assert len(gfm_to_rich("word " * 20000)) > 1


def test_no_rich_chunk_splits_an_entity() -> None:
    text = ("a & b\n" * 8000)
    chunks = gfm_to_rich(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= RICH_MAX_LENGTH
        assert chunk.count("&") == chunk.count("&amp;")


def test_escape_rich_turns_newlines_into_breaks() -> None:
    assert escape_rich("a\nb") == "a<br>b"


def test_escape_rich_inline_collapses_newlines() -> None:
    assert escape_rich_inline("a\n b") == "a b"


def test_rich_details_wraps_an_escaped_body() -> None:
    out = rich_details("Bash", rich_code_block("ls -la", "bash"))
    assert out.startswith("<details><summary>Bash</summary>")
    assert "```bash\nls -la\n```" in out


def test_rich_details_can_start_open() -> None:
    assert rich_details("s", "b", open=True).startswith("<details open>")
