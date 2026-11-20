"""Render a ``SessionTranscript`` as one self-contained HTML page for ``/share``.

The page is meant to be forwarded as a file and opened wherever it lands, so
it carries everything inline: CSS in a ``<style>`` block, images as ``data:``
URLs, and no script, since in-app viewers that block JavaScript would
otherwise show a blank or half-built page.  Folding is ``<details>``.

Each prompt opens a turn.  Replies render as Markdown with raw HTML escaped;
prompts, tool input and tool output render verbatim as text.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from typing import Any

import mistune

from open_shrimp.backend.types import (
    SessionTranscript,
    TranscriptImage,
    TranscriptItem,
    TranscriptText,
    TranscriptToolCall,
)

#: Telegram's upload ceiling for bots.  A page over it cannot be sent.
MAX_SHARE_BYTES = 50 * 1024 * 1024

_markdown = mistune.create_markdown(
    escape=True, plugins=["strikethrough", "table", "url"],
)

#: Input keys tried in order for a tool call's one-line summary.
_SUMMARY_KEYS = (
    "command", "file_path", "filePath", "notebook_path", "path", "pattern",
    "url", "query", "description", "prompt",
)
_SUMMARY_MAX = 120
_TITLE_MAX = 120

_CSS = """
:root {
  --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --line: #e7e5e4;
  --card: #ffffff; --prompt: #fef2f2; --prompt-line: #fca5a5;
  --code: #f5f5f4; --add: #dcfce7; --del: #fee2e2; --hunk: #e0e7ff;
  --err: #b91c1c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1917; --fg: #e7e5e4; --muted: #a8a29e; --line: #44403c;
    --card: #292524; --prompt: #3b1d1d; --prompt-line: #7f1d1d;
    --code: #0c0a09; --add: #14532d; --del: #7f1d1d; --hunk: #312e81;
    --err: #f87171;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 860px; margin: 0 auto; padding: 20px 14px 48px; }
header h1 { font-size: 1.35rem; line-height: 1.3; margin: 0 0 6px; overflow-wrap: anywhere; }
header p { margin: 2px 0; color: var(--muted); font-size: .9rem; }
section.turn { margin-top: 28px; }
h2 {
  font-size: .8rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); border-bottom: 1px solid var(--line); padding-bottom: 4px; margin: 0 0 12px;
}
h2 time { font-weight: 400; text-transform: none; letter-spacing: 0; margin-left: 6px; }
.prompt {
  background: var(--prompt); border-left: 3px solid var(--prompt-line);
  border-radius: 6px; padding: 10px 12px; margin: 10px 0;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.who { font-size: .75rem; font-weight: 600; color: var(--muted); margin-bottom: 4px; white-space: normal; }
.reply { margin: 10px 0; overflow-wrap: anywhere; }
.reply > :first-child { margin-top: 0; }
.reply > :last-child { margin-bottom: 0; }
.reply table { border-collapse: collapse; display: block; overflow-x: auto; }
.reply th, .reply td { border: 1px solid var(--line); padding: 4px 8px; }
code { font: .86em/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
:not(pre) > code { background: var(--code); padding: 1px 4px; border-radius: 4px; }
pre {
  background: var(--code); border-radius: 6px; padding: 8px 10px; margin: 4px 0 10px;
  overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  font: .82rem/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
details { margin: 6px 0; }
details.tool, details.thinking {
  background: var(--card); border: 1px solid var(--line); border-radius: 6px;
}
details.tool > summary, details.thinking > summary {
  cursor: pointer; padding: 6px 10px; font-size: .9rem; overflow-wrap: anywhere;
}
details.tool[open] > summary, details.thinking[open] > summary { border-bottom: 1px solid var(--line); }
details.tool > .body, details.thinking > .body { padding: 8px 10px; }
details.thinking > summary { color: var(--muted); font-style: italic; }
summary .name { font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85rem; }
summary .arg { color: var(--muted); }
details.tool.error > summary .name { color: var(--err); }
.label { font-size: .72rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); margin-top: 6px; }
.label.error { color: var(--err); }
.pending { color: var(--muted); font-style: italic; font-size: .9rem; }
pre.diff span { display: block; }
pre.diff .add { background: var(--add); }
pre.diff .del { background: var(--del); }
pre.diff .hunk { background: var(--hunk); }
img { max-width: 100%; height: auto; border: 1px solid var(--line); border-radius: 6px; margin: 6px 0; display: block; }
.subagent { border-left: 2px solid var(--line); padding-left: 10px; margin-top: 8px; }
footer { margin-top: 40px; color: var(--muted); font-size: .8rem; text-align: center; }
"""


def render_html(transcript: SessionTranscript, *, agent_name: str) -> str:
    """The whole page for *transcript*, which *agent_name* produced."""
    title = _title(transcript)
    stamps = _timestamps(transcript.items)
    start = min(stamps) if stamps else None
    end = max(stamps) if stamps else None
    multi_day = (
        start is not None and end is not None
        and start.astimezone().date() != end.astimezone().date()
    )

    meta = [
        " · ".join(html.escape(m) for m in transcript.models) + f" via {html.escape(agent_name)}"
        if transcript.models else f"Agent: {html.escape(agent_name)}"
    ]
    if start is not None and end is not None:
        meta.append(
            f"Started {_time(start, full=True)} · ended {_time(end, full=True)}"
            f" · {_duration(end - start)} · {_zone(start)}"
        )

    body: list[str] = []
    turn: list[str] | None = None
    turn_no = 0
    for item in transcript.items:
        if isinstance(item, TranscriptText) and item.role == "user":
            if turn is not None:
                body.append(_close_turn(turn))
            turn_no += 1
            stamp = (
                f'<time>{_time(item.timestamp, full=multi_day)}</time>'
                if item.timestamp is not None else ""
            )
            turn = [f'<section class="turn"><h2>Turn {turn_no}{stamp}</h2>']
        elif turn is None:
            turn = ['<section class="turn">']
        turn.append(_item(item, nested=False))
    if turn is not None:
        body.append(_close_turn(turn))
    if not body:
        body.append('<p class="pending">This session has no messages yet.</p>')

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        "<body><main>"
        f"<header><h1>{html.escape(title)}</h1>"
        + "".join(f"<p>{line}</p>" for line in meta)
        + "</header>"
        + "".join(body)
        + "<footer>Shared from OpenShrimp</footer></main></body></html>\n"
    )


def share_filename(transcript: SessionTranscript) -> str:
    """``<title-slug>-<session id prefix>.html``, ASCII only."""
    slug = re.sub(r"[^a-z0-9]+", "-", _title(transcript).lower()).strip("-")[:50].strip("-")
    short_id = re.sub(r"[^A-Za-z0-9]", "", transcript.session_id)[:8] or "session"
    return f"{slug}-{short_id}.html" if slug else f"session-{short_id}.html"


def _title(transcript: SessionTranscript) -> str:
    title = " ".join((transcript.title or "").split())
    if not title:
        return "Agent session"
    return title if len(title) <= _TITLE_MAX else title[: _TITLE_MAX - 1] + "…"


def _close_turn(parts: list[str]) -> str:
    return "".join(parts) + "</section>"


def _item(item: TranscriptItem, *, nested: bool) -> str:
    if isinstance(item, TranscriptToolCall):
        return _tool(item)
    if item.role == "user":
        who = "Prompt" if nested else "You"
        text = html.escape(item.text)
        return (
            f'<div class="prompt"><div class="who">{who}</div>{text}'
            f"{_images(item.images)}</div>"
        )
    if item.role == "thinking":
        return (
            '<details class="thinking"><summary>Thinking</summary>'
            f'<div class="body reply">{_markdown(item.text)}</div></details>'
        )
    return f'<div class="reply">{_markdown(item.text)}{_images(item.images)}</div>'


def _tool(call: TranscriptToolCall) -> str:
    arg = _summary(call.input)
    summary = f'<span class="name">{html.escape(call.name)}</span>'
    if arg:
        summary += f' <span class="arg">{html.escape(arg)}</span>'

    parts: list[str] = []
    if call.diff:
        parts.append(f'<div class="label">Diff</div>{_diff(call.diff)}')
        if call.input:
            parts.append(
                '<details><summary class="label">Input</summary>'
                f"{_input(call.input)}</details>"
            )
    elif call.input:
        parts.append(f'<div class="label">Input</div>{_input(call.input)}')

    if call.output is None:
        parts.append('<p class="pending">No result was recorded for this call.</p>')
    elif call.output or not call.images:
        label = '<div class="label error">Error</div>' if call.is_error else '<div class="label">Output</div>'
        parts.append(f"{label}<pre>{html.escape(call.output) or '(empty)'}</pre>")
    parts.append(_images(call.images))

    if call.subagent:
        parts.append(
            '<div class="label">Subagent</div><div class="subagent">'
            + "".join(_item(i, nested=True) for i in call.subagent)
            + "</div>"
        )

    css = "tool error" if call.is_error else "tool"
    return (
        f'<details class="{css}"><summary>{summary}</summary>'
        f'<div class="body">{"".join(parts)}</div></details>'
    )


def _summary(tool_input: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            line = " ".join(value.split())
            return line if len(line) <= _SUMMARY_MAX else line[: _SUMMARY_MAX - 1] + "…"
    return ""


def _input(tool_input: dict[str, Any]) -> str:
    """One labelled block per key: strings verbatim, everything else as JSON."""
    out: list[str] = []
    for key, value in tool_input.items():
        text = value if isinstance(value, str) else json.dumps(
            value, indent=2, ensure_ascii=False, default=str,
        )
        out.append(
            f'<div class="label">{html.escape(str(key))}</div>'
            f"<pre>{html.escape(text)}</pre>"
        )
    return "".join(out)


def _diff(diff: str) -> str:
    lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            css = "hunk"
        elif line.startswith("+") and not line.startswith("+++"):
            css = "add"
        elif line.startswith("-") and not line.startswith("---"):
            css = "del"
        else:
            css = ""
        cls = f' class="{css}"' if css else ""
        lines.append(f"<span{cls}>{html.escape(line) or ' '}</span>")
    return f'<pre class="diff">{"".join(lines)}</pre>'


def _images(images: list[TranscriptImage]) -> str:
    return "".join(
        f'<img alt="" loading="lazy" '
        f'src="data:{html.escape(i.media_type)};base64,{html.escape(i.data)}">'
        for i in images
    )


def _timestamps(items: list[TranscriptItem]) -> list[datetime]:
    out: list[datetime] = []
    for item in items:
        if item.timestamp is not None:
            out.append(item.timestamp)
        if isinstance(item, TranscriptToolCall):
            out.extend(_timestamps(item.subagent))
    return out


def _time(value: datetime, *, full: bool) -> str:
    local = value.astimezone()
    return local.strftime("%Y-%m-%d %H:%M" if full else "%H:%M")


def _zone(value: datetime) -> str:
    offset = value.astimezone().utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{mins:02d}"


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{mins} min"


__all__ = ["MAX_SHARE_BYTES", "render_html", "share_filename"]
