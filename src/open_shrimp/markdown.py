"""GFM to Telegram's rich-message Markdown.

``gfm_to_rich`` walks a mistune parse and re-emits it as the GFM dialect
``sendRichMessage`` accepts, which keeps tables, task lists and collapsible
blocks and holds 32768 characters.  Every message the bot sends goes through
it.
"""

from __future__ import annotations

import re
from typing import Any

import mistune

# A regular message — plain text, no rich body — still caps at 4096.
TELEGRAM_MAX_LENGTH = 4096
RICH_MAX_LENGTH = 32768
# What one body inside a card may take.  The headroom is for the header, the
# fence around it, and whatever the caller composes on top; the number belongs
# here because no single call site can see the whole card it ends up in.
RICH_MAX_BODY = RICH_MAX_LENGTH - 500

# Characters that can open a markup construct in Telegram's rich GFM dialect
# and so must leave a text run backslash-escaped.  The backslash itself leads
# the set so a literal one survives the same pass.  Telegram's inline parser is
# looser than GFM — it italicises the 3 in "5 * 3 * 2" and bolds "__dunder__" —
# so an asterisk or underscore mistune left as text has to be escaped even
# where GFM would have ignored it.
_RICH_ESCAPE_CHARS = "\\" + "`*_[]~=#"
_RICH_ESCAPE_RE = re.compile(r"([" + re.escape(_RICH_ESCAPE_CHARS) + r"])")

# A rich message is parsed as HTML as well as Markdown: an unescaped "<" opens
# a tag and swallows everything through the next ">", so "Generic<T>" renders as
# "Generic" and "<script>alert(1)</script>" loses its body.  ">" goes through
# the same escape so a text run can't open a blockquote.
_RICH_HTML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

# Newlines inside a block collapse the way GFM collapses them, so a hard break
# has to be spelled out.
_RICH_BREAK = "<br>"


def escape_rich(text: str) -> str:
    """Escape literal text for a rich message body."""
    text = _RICH_ESCAPE_RE.sub(r"\\\1", text)
    for char, entity in _RICH_HTML_ESCAPES:
        text = text.replace(char, entity)
    return text.replace("\n", _RICH_BREAK)


def escape_rich_inline(text: str) -> str:
    """Escape literal text for a rich body that must stay on one line.

    Table cells and ``<summary>`` rows have no room for a line break, so the
    newlines become spaces instead of ``<br>``.
    """
    return " ".join(escape_rich(text.replace("\n", " ")).split())


def _longest_backtick_run(code: str) -> int:
    return max((len(run) for run in re.findall(r"`+", code)), default=0)


def rich_code_block(code: str, info: str = "") -> str:
    """Wrap *code* in a fence, with no escaping — a fence is taken literally."""
    body = code.rstrip("\n")
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    return f"{fence}{info}\n{body}\n{fence}"


def rich_details(summary: str, body: str, *, open: bool = False) -> str:
    """Build a collapsible block from an already-escaped summary and body."""
    tag = "<details open>" if open else "<details>"
    return f"{tag}<summary>{summary}</summary>\n\n{body}\n\n</details>"


class RichRenderer(mistune.BaseRenderer):
    """Render mistune AST tokens back out as Telegram rich-message Markdown.

    The target dialect is GFM, so the work is escaping the text runs and
    spelling out the breaks GFM would collapse.
    """

    NAME = "rich"

    def render_token(self, token: dict[str, Any], state: Any) -> str:
        ttype = token["type"]

        # Tables and lists need the raw token tree: a GFM table carries its
        # alignment in a delimiter row the cells can't see, and an ordered
        # list numbers items the items can't count.
        if ttype == "table":
            return self._render_table(token, state)
        if ttype == "list":
            return self._render_list(token, state)

        func = self._get_method(ttype)
        attrs = token.get("attrs", {})

        if "raw" in token:
            children = token["raw"]
        elif "children" in token:
            children = self.render_tokens(token["children"], state)
        else:
            return func(**attrs) if attrs else func()

        return func(children, **attrs) if attrs else func(children)

    # ── Block-level ──

    def text(self, text: str) -> str:
        return escape_rich(text)

    def paragraph(self, text: str) -> str:
        return text + "\n\n"

    def heading(self, text: str, **attrs: Any) -> str:
        # h1 and h2 are centred on web and left-aligned on Android; h3 and
        # below agree, so everything larger is clamped down to h3.
        level = max(int(attrs.get("level", 3)), 3)
        return f"{'#' * level} {text}\n\n"

    def blank_line(self) -> str:
        return ""

    def thematic_break(self) -> str:
        return "---\n\n"

    def block_code(self, code: str, **attrs: Any) -> str:
        info = (attrs.get("info") or "").strip()
        # Telegram highlights on the first word of the info string.
        lang = info.split()[0] if info else ""
        return rich_code_block(code, lang) + "\n\n"

    def block_quote(self, text: str) -> str:
        lines = text.strip().split("\n")
        return "\n".join("> " + line if line else ">" for line in lines) + "\n\n"

    def block_text(self, text: str) -> str:
        # The trailing newline keeps a nested list off the parent item's line.
        return text + "\n"

    def block_error(self, text: str) -> str:
        return ""

    # ── Lists ──

    def _render_list(self, token: dict[str, Any], state: Any) -> str:
        attrs = token.get("attrs", {})
        ordered = bool(attrs.get("ordered"))
        start = int(attrs.get("start") or 1)

        lines: list[str] = []
        for index, item in enumerate(token.get("children", [])):
            body = self.render_tokens(item.get("children", []), state)
            body = body.strip("\n")
            if item["type"] == "task_list_item":
                checked = item.get("attrs", {}).get("checked")
                marker = "- [x] " if checked else "- [ ] "
            elif ordered:
                marker = f"{start + index}. "
            else:
                marker = "- "
            pad = " " * len(marker)
            first, *rest = body.split("\n")
            lines.append(marker + first)
            # A nested list or a second paragraph has to keep clear of the
            # marker or it reads as a new item.
            lines.extend(pad + line if line else "" for line in rest)

        return "\n".join(lines) + "\n\n"

    # Stubs so mistune finds the method; actual rendering is in _render_list
    def list(self, text: str, **attrs: Any) -> str:
        return text  # pragma: no cover

    def list_item(self, text: str) -> str:
        return text  # pragma: no cover

    def task_list_item(self, text: str, **attrs: Any) -> str:
        return text  # pragma: no cover

    # ── Tables ──

    _ALIGN_RULES = {
        "left": ":---",
        "right": "---:",
        "center": ":---:",
        None: "---",
    }

    def _render_table(self, token: dict[str, Any], state: Any) -> str:
        head: list[str] = []
        aligns: list[str] = []
        body: list[list[str]] = []

        for child in token.get("children", []):
            if child["type"] == "table_head":
                for cell in child.get("children", []):
                    head.append(self._render_cell(cell, state))
                    align = cell.get("attrs", {}).get("align")
                    aligns.append(self._ALIGN_RULES.get(align, "---"))
            elif child["type"] == "table_body":
                for row in child.get("children", []):
                    body.append([
                        self._render_cell(cell, state)
                        for cell in row.get("children", [])
                    ])

        if not head:
            return ""

        lines = [
            "| " + " | ".join(head) + " |",
            "| " + " | ".join(aligns) + " |",
        ]
        for row in body:
            padded = row + [""] * (len(head) - len(row))
            lines.append("| " + " | ".join(padded[:len(head)]) + " |")
        return "\n".join(lines) + "\n\n"

    def _render_cell(self, cell: dict[str, Any], state: Any) -> str:
        text = self.render_tokens(cell.get("children", []), state)
        # A pipe would end the cell and a newline would end the row.
        return text.replace("\n", " ").replace("|", "\\|").strip()

    # Stubs so mistune finds the method; actual rendering is in _render_table
    def table(self, text: str) -> str:
        return text  # pragma: no cover

    def table_head(self, text: str) -> str:
        return text  # pragma: no cover

    def table_body(self, text: str) -> str:
        return text  # pragma: no cover

    def table_row(self, text: str) -> str:
        return text  # pragma: no cover

    def table_cell(self, text: str, **attrs: Any) -> str:
        return text  # pragma: no cover

    # ── Inline-level ──

    def emphasis(self, text: str) -> str:
        return f"*{text}*"

    def strong(self, text: str) -> str:
        return f"**{text}**"

    def strikethrough(self, text: str) -> str:
        return f"~~{text}~~"

    def codespan(self, code: str) -> str:
        ticks = "`" * (_longest_backtick_run(code) + 1)
        # A span that starts or ends with a backtick needs a space the parser
        # strips back off, or the delimiter runs merge.
        pad = " " if code.startswith("`") or code.endswith("`") else ""
        return f"{ticks}{pad}{code}{pad}{ticks}"

    def link(self, text: str, **attrs: Any) -> str:
        url = attrs.get("url", "")
        # Percent-encode what would end the destination early.  GFM's
        # angle-bracket form would do the same job, but "<" is the one
        # character a rich message reads as markup wherever it appears.
        for char, encoded in (
            ("\\", "%5C"), ("(", "%28"), (")", "%29"), (" ", "%20"),
        ):
            url = url.replace(char, encoded)
        return f"[{text}]({url})"

    def image(self, text: str, **attrs: Any) -> str:
        # Media rides in InputRichMessage.media, not in the markdown, so an
        # image collapses to its alt text.
        return text if text else ""

    def linebreak(self) -> str:
        return _RICH_BREAK

    def softbreak(self) -> str:
        return _RICH_BREAK

    def inline_html(self, html: str) -> str:
        # Agent output is not trusted to carry markup: "Generic<T>" arrives
        # here as "<T>" and has to come out visible rather than swallowed.
        return escape_rich(html)

    def block_html(self, html: str) -> str:
        return escape_rich(html.rstrip("\n")) + "\n\n"


def _is_inside_code_block(text: str, position: int) -> tuple[bool, str]:
    """Check if a position in rendered MarkdownV2 text is inside a code block.

    Counts unmatched ``` fences before the position.  Returns (is_inside, fence)
    where fence is the opening fence line (e.g. "```python") so we can re-open
    it in the next chunk.
    """
    inside = False
    fence = "```"
    i = 0
    while i < position:
        if text[i:i + 3] == "```":
            if not inside:
                # Capture the full fence line (e.g. ```python)
                end = text.find("\n", i)
                if end == -1 or end > position:
                    end = position
                fence = text[i:end]
                inside = True
            else:
                inside = False
            i += 3
        else:
            i += 1
    return inside, fence


#: What a continuation chunk adds to the summary row it repeats.
_DETAILS_CONTINUED = " (cont.)"

#: How much of a summary row a continuation repeats.  The cap keeps the
#: reopening tag far shorter than the progress each split makes, so a card
#: with a long row cannot grow its own continuations faster than the body
#: shrinks.
_DETAILS_SUMMARY_MAX = 200

#: A card's opening tag with its summary row, or a card's closing tag.
_DETAILS_TAG_RE = re.compile(
    r"<details(?P<expanded> open)?><summary>(?P<summary>.*?)</summary>"
    r"|(?P<close></details>)",
    re.DOTALL,
)


def _open_details(text: str, position: int) -> str | None:
    """The tag that reopens a card left open at *position*, or None.

    A ``<details>`` cut in half leaves the first chunk holding an unclosed
    block and the second opening with a bare body.  The continuation repeats
    the summary row so the reader can tell what the rest belongs to.
    """
    depth = 0
    summary = ""
    expanded = False
    for match in _DETAILS_TAG_RE.finditer(text, 0, position):
        if match.group("close"):
            depth = max(0, depth - 1)
        else:
            depth += 1
            summary = match.group("summary") or ""
            expanded = bool(match.group("expanded"))
    if depth <= 0:
        return None
    tag = "<details open>" if expanded else "<details>"
    # A card can span three chunks; the mark is idempotent so the third
    # summary reads "… (cont.)" rather than "… (cont.) (cont.)".
    if not summary.endswith(_DETAILS_CONTINUED):
        # An over-long row is dropped rather than clipped: a cut through
        # "**" would leave the continuation's bold unterminated.
        if len(summary) > _DETAILS_SUMMARY_MAX:
            summary = "…"
        summary += _DETAILS_CONTINUED
    return f"{tag}<summary>{summary}</summary>"


def _back_off_escape(text: str, split_at: int) -> int:
    """Move a split point off the middle of a backslash escape sequence.

    A chunk ending in an odd run of backslashes ends with a backslash whose
    escaped character landed in the next chunk, which Telegram rejects as an
    unterminated escape.  Give that backslash back to the next chunk.
    """
    run = 0
    while run < split_at and text[split_at - 1 - run] == "\\":
        run += 1
    if run % 2 and split_at > 1:
        return split_at - 1
    return split_at


# The longest thing "&" can open ("&amp;") plus the longest tag the rich
# renderer emits; a split inside either would leave both halves malformed.
_RICH_MARKUP_LOOKBACK = 16


def _back_off_markup(text: str, split_at: int) -> int:
    """Move a split point off the middle of an entity or a tag.

    Only the last-resort split at ``max_length`` can land here — every other
    candidate is a newline, and neither ``&amp;`` nor ``<br>`` contains one.
    """
    window = text[max(0, split_at - _RICH_MARKUP_LOOKBACK):split_at]
    for opener in ("&", "<"):
        start = window.rfind(opener)
        if start == -1:
            continue
        closer = ";" if opener == "&" else ">"
        if closer not in window[start:]:
            return max(1, split_at - (len(window) - start))
    return split_at


#: Room held back from every split for the closers a chunk may have to grow:
#: a fence (4) and a card's closing tag (12).  Without it a chunk that lands
#: exactly on the limit overflows it by closing what it opened.
_SPLIT_RESERVE = 16


def split_message(text: str, max_length: int = RICH_MAX_LENGTH) -> list[str]:
    """Split a rendered message into chunks of at most max_length characters.

    Splits at natural boundaries: paragraph breaks, then line breaks.  A split
    that lands inside a fenced code block or a ``<details>`` card closes it in
    the chunk and reopens it in the next, so neither half carries a construct
    the other half terminates.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    budget = max_length - _SPLIT_RESERVE

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining.strip())
            break

        # Try a paragraph boundary (double newline), then a line break, then
        # the budget.  A boundary in the first half of the budget is refused
        # rather than taken: a card whose body is one long paragraph has its
        # only blank line right after the summary row, and splitting there
        # ships a chunk holding nothing but the header — which the next chunk
        # then reopens, so the loop never shortens what is left.
        split_at = remaining.rfind("\n\n", 0, budget)
        if split_at < budget // 2:
            split_at = remaining.rfind("\n", 0, budget)
        if split_at < budget // 2:
            split_at = budget

        split_at = _back_off_markup(remaining, _back_off_escape(remaining, split_at))
        chunk = remaining[:split_at].strip()
        rest = remaining[split_at:].lstrip("\n")

        # Check if we're splitting inside a code block
        inside, fence = _is_inside_code_block(remaining, split_at)
        if inside:
            # Close the code block in this chunk, reopen in the next
            chunk = chunk + "\n```"
            rest = fence + "\n" + rest

        # …and inside a card, whose closing tag goes outside the fence's
        reopen = _open_details(remaining, split_at)
        if reopen:
            chunk = chunk + "\n\n</details>"
            rest = reopen + "\n\n" + rest

        chunks.append(chunk)
        remaining = rest

    return [c for c in chunks if c]


#: One parser for the process.  Building it compiles the block and inline
#: scanners and registers three plugins; parse state is per-call, so the
#: single-threaded event loop can share the instance across every message.
_MARKDOWN = mistune.create_markdown(
    renderer=RichRenderer(),
    plugins=["strikethrough", "table", "task_lists"],
)


def gfm_to_rich_text(text: str) -> str:
    """Convert GFM to Telegram's rich-message Markdown, without splitting.

    Callers that assemble several converted runs into one body need the whole
    rendering; ``gfm_to_rich`` splits it for sending.
    """
    return _MARKDOWN(text).strip()


def gfm_to_rich(text: str) -> list[str]:
    """Convert GitHub-Flavored Markdown to Telegram's rich-message Markdown.

    Returns a list of strings, each at most 32768 characters, suitable for the
    ``markdown`` field of an ``InputRichMessage``.
    """
    return split_message(gfm_to_rich_text(text), RICH_MAX_LENGTH)
