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
    RICH_MAX_LENGTH,
    TELEGRAM_MAX_LENGTH,
    escape,
    escape_code,
    escape_rich,
    escape_rich_inline,
    gfm_to_rich,
    gfm_to_telegram,
    rich_code_block,
    rich_details,
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


# ── Rich messages ──
#
# The rich renderer's job is the mirror image: Telegram's rich parser reads
# both Markdown and HTML, so "<" has to leave as an entity and every markup
# character mistune did *not* claim has to leave backslash-escaped.


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
