"""GFM to Telegram converters.

Two renderers share the mistune parse.  ``gfm_to_rich`` emits the GFM dialect
``sendRichMessage`` accepts, which keeps tables, task lists and collapsible
blocks and raises the per-message ceiling from 4096 to 32768 characters; every
message the bot sends goes through it.

``gfm_to_telegram`` emits MarkdownV2, which escapes 18 metacharacters and
flattens a table into a code fence.  Nothing calls it: it is the way back if a
client turns out not to render rich messages, and iOS and desktop are still
unchecked.
"""

from __future__ import annotations

import re
from typing import Any

import mistune

TELEGRAM_MAX_LENGTH = 4096
RICH_MAX_LENGTH = 32768

# Characters that must be escaped in Telegram MarkdownV2 (outside code spans/blocks).
# The backslash is in the set because Telegram treats an unescaped one as an
# escape character and drops it: a literal backslash must be sent doubled.
_ESCAPE_CHARS = "\\" + r"_*[]()~`>#+-=|{}.!"
_ESCAPE_RE = re.compile(r"([" + re.escape(_ESCAPE_CHARS) + r"])")

# Inside pre and code entities Telegram only honours these two escapes; every
# other character is literal, so escaping more would show the backslashes.
_CODE_ESCAPE_RE = re.compile(r"([\\`])")


def escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _ESCAPE_RE.sub(r"\\\1", text)


def escape_code(text: str) -> str:
    """Escape the characters Telegram honours inside pre and code entities."""
    return _CODE_ESCAPE_RE.sub(r"\\\1", text)


def _plain_text(token: dict[str, Any]) -> str:
    """Extract plain text from a token tree (no formatting)."""
    if "raw" in token and isinstance(token["raw"], str):
        return token["raw"]
    if "children" in token:
        children = token["children"]
        if isinstance(children, str):
            return children
        if isinstance(children, list):
            return "".join(_plain_text(c) for c in children)
    return ""


class TelegramRenderer(mistune.BaseRenderer):
    """Render mistune AST tokens into Telegram MarkdownV2 strings.

    Follows the same render_token pattern as mistune's HTMLRenderer:
    methods receive pre-rendered children text + keyword attrs.
    """

    NAME = "telegram"

    def render_token(self, token: dict[str, Any], state: Any) -> str:
        ttype = token["type"]

        # Tables need raw token access to extract cell text
        if ttype == "table":
            return self._render_table(token, state)

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
        return escape(text)

    def paragraph(self, text: str) -> str:
        return text + "\n\n"

    def heading(self, text: str, **attrs: Any) -> str:
        return f"*{text}*\n\n"

    def blank_line(self) -> str:
        return ""

    def thematic_break(self) -> str:
        return escape("---") + "\n\n"

    def block_code(self, code: str, **attrs: Any) -> str:
        info = attrs.get("info", "")
        code = escape_code(code.rstrip("\n"))
        if info:
            return f"```{info}\n{code}\n```\n\n"
        return f"```\n{code}\n```\n\n"

    def block_quote(self, text: str) -> str:
        lines = text.strip().split("\n")
        # Filter out empty lines: Telegram ends a blockquote at a bare ">"
        # line, so empty lines between paragraphs would break a single
        # blockquote into multiple separate ones with unquoted gaps.
        non_empty = [line for line in lines if line]
        quoted = "\n".join(">" + line for line in non_empty)
        return quoted + "\n\n"

    def list(self, text: str, **attrs: Any) -> str:
        return text + "\n"

    def list_item(self, text: str) -> str:
        return escape("- ") + text.strip() + "\n"

    def block_text(self, text: str) -> str:
        return text

    def block_error(self, text: str) -> str:
        return ""

    # ── Tables → monospace preformatted ──
    # Tables need access to the raw token tree, so we override render_token
    # for the table type and handle it specially.

    def _render_table(self, token: dict[str, Any], state: Any) -> str:
        rows: list[list[str]] = []
        for child in token.get("children", []):
            if child["type"] == "table_head":
                row = [_plain_text(cell) for cell in child.get("children", [])]
                rows.append(row)
            elif child["type"] == "table_body":
                for table_row in child.get("children", []):
                    row = [_plain_text(cell) for cell in table_row.get("children", [])]
                    rows.append(row)

        if not rows:
            return ""

        col_count = max(len(r) for r in rows)
        col_widths = [0] * col_count
        for r in rows:
            for i, cell in enumerate(r):
                col_widths[i] = max(col_widths[i], len(cell))

        lines: list[str] = []
        for idx, r in enumerate(rows):
            padded = [
                (r[i] if i < len(r) else "").ljust(col_widths[i])
                for i in range(col_count)
            ]
            lines.append(" | ".join(padded))
            if idx == 0:
                lines.append("-+-".join("-" * w for w in col_widths))

        # Escape after padding: the escapes are invisible once Telegram parses
        # them, so column widths must be measured on the unescaped text.
        table_text = escape_code("\n".join(lines))
        return f"```\n{table_text}\n```\n\n"

    # Stubs so mistune finds the method; actual rendering is in _render_table
    def table(self, text: str) -> str:
        return text  # pragma: no cover

    def table_head(self, text: str) -> str:
        return text  # pragma: no cover

    def table_body(self, text: str) -> str:
        return text  # pragma: no cover

    def table_cell(self, text: str, **attrs: Any) -> str:
        return text  # pragma: no cover

    # ── Inline-level ──

    def emphasis(self, text: str) -> str:
        return f"_{text}_"

    def strong(self, text: str) -> str:
        return f"*{text}*"

    def codespan(self, code: str) -> str:
        return f"`{escape_code(code)}`"

    def link(self, text: str, **attrs: Any) -> str:
        url = attrs.get("url", "")
        # Escape only ) and \ in URLs for MarkdownV2
        escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{text}]({escaped_url})"

    def image(self, text: str, **attrs: Any) -> str:
        # Strip images, return alt text (already rendered from children)
        return text if text else ""

    def linebreak(self) -> str:
        return "\n"

    def softbreak(self) -> str:
        return "\n"

    def inline_html(self, html: str) -> str:
        return ""

    def block_html(self, html: str) -> str:
        return ""

    def strikethrough(self, text: str) -> str:
        return f"~{text}~"


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

    Where ``TelegramRenderer`` rewrites the tree into MarkdownV2's smaller
    vocabulary, this one re-emits GFM: the target dialect is GFM, so the work
    is escaping the text runs and spelling out the breaks GFM would collapse.
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


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split a rendered message into chunks of at most max_length characters.

    Splits at natural boundaries: paragraph breaks, then line breaks.
    Code-block-aware: if the split point falls inside a fenced code block,
    the current chunk is closed with ``` and the next chunk reopens the fence.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining.strip())
            break

        # Try to split at a paragraph boundary (double newline)
        split_at = remaining.rfind("\n\n", 0, max_length)
        if split_at <= 0:
            # Try to split at a single newline
            split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            # Last resort: split at max_length
            split_at = max_length

        split_at = _back_off_markup(remaining, _back_off_escape(remaining, split_at))
        chunk = remaining[:split_at].strip()
        rest = remaining[split_at:].lstrip("\n")

        # Check if we're splitting inside a code block
        inside, fence = _is_inside_code_block(remaining, split_at)
        if inside:
            # Close the code block in this chunk, reopen in the next
            chunk = chunk + "\n```"
            rest = fence + "\n" + rest

        chunks.append(chunk)
        remaining = rest

    return [c for c in chunks if c]


def gfm_to_telegram(text: str) -> list[str]:
    """Convert GitHub-Flavored Markdown to Telegram MarkdownV2.

    Returns a list of strings, each at most 4096 characters, suitable for
    sending as individual Telegram messages.
    """
    md = mistune.create_markdown(
        renderer=TelegramRenderer(),
        plugins=["strikethrough", "table"],
    )
    rendered = md(text)
    return split_message(rendered)


def gfm_to_rich_text(text: str) -> str:
    """Convert GFM to Telegram's rich-message Markdown, without splitting.

    Callers that assemble several converted runs into one body need the whole
    rendering; ``gfm_to_rich`` splits it for sending.
    """
    md = mistune.create_markdown(
        renderer=RichRenderer(),
        plugins=["strikethrough", "table", "task_lists"],
    )
    return md(text).strip()


def gfm_to_rich(text: str) -> list[str]:
    """Convert GitHub-Flavored Markdown to Telegram's rich-message Markdown.

    Returns a list of strings, each at most 32768 characters, suitable for the
    ``markdown`` field of an ``InputRichMessage``.
    """
    return split_message(gfm_to_rich_text(text), RICH_MAX_LENGTH)
