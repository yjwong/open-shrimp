"""Render agent JSONL transcripts as ANSI-formatted text for xterm.js.

Agent background tasks produce JSONL transcript files (one JSON object per
line).  This module parses those transcripts and renders them as readable
ANSI-colored text suitable for display in the terminal Mini App.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from open_shrimp.stream import extract_tool_summary

logger = logging.getLogger(__name__)

# ANSI escape sequences.
_BOLD_CYAN = "\x1b[1;36m"
_BOLD_YELLOW = "\x1b[1;33m"
_DIM_RED = "\x1b[2;31m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def render_jsonl_content(raw_text: str) -> str:
    """Render a complete JSONL transcript as ANSI-formatted text."""
    parts: list[str] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        rendered = _render_line(line)
        if rendered:
            parts.append(rendered)
    return "".join(parts)


def render_jsonl_lines(raw_text: str) -> tuple[str, str]:
    """Render complete JSONL lines, returning rendered text and remainder.

    The *remainder* is any trailing text that does not end with a newline
    (i.e. an incomplete line that should be buffered for the next chunk).
    """
    if not raw_text:
        return "", ""

    # Split on newlines.  The last element is either "" (if raw_text
    # ends with \n) or an incomplete line.
    segments = raw_text.split("\n")
    remainder = segments.pop()  # incomplete trailing line (or "")

    parts: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        rendered = _render_line(seg)
        if rendered:
            parts.append(rendered)

    return "".join(parts), remainder


def render_openshrimp_agent_content(raw_text: str) -> str:
    """Render an OpenShrimp-normalized Agent transcript."""
    parts: list[str] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        rendered = _render_openshrimp_agent_line(line)
        if rendered:
            parts.append(rendered)
    return "".join(parts)


def render_openshrimp_agent_lines(raw_text: str) -> tuple[str, str]:
    """Render complete OpenShrimp Agent JSONL lines with a remainder."""
    if not raw_text:
        return "", ""

    segments = raw_text.split("\n")
    remainder = segments.pop()

    parts: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        rendered = _render_openshrimp_agent_line(seg)
        if rendered:
            parts.append(rendered)

    return "".join(parts), remainder


def _render_line(line: str) -> str:
    """Parse a single JSON line and render it."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return f"{_DIM_RED}[unreadable line]{_RESET}\n"

    if not isinstance(obj, dict):
        return ""

    return _render_message(obj)


def _render_openshrimp_agent_line(line: str) -> str:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return f"{_DIM_RED}[unreadable line]{_RESET}\n"

    if not isinstance(obj, dict):
        return ""

    event = obj.get("event")
    if event == "launched":
        prompt = str(obj.get("prompt") or "").strip()
        if not prompt:
            return ""
        return f"{_BOLD_CYAN}> {prompt}{_RESET}\n\n"
    if event == "assistant_text":
        text = str(obj.get("text") or "")
        return text if text.endswith("\n") else text + "\n"
    if event == "tool_start":
        tool = str(obj.get("tool") or "unknown")
        tool_input = obj.get("tool_input")
        summary = ""
        if isinstance(tool_input, dict):
            summary = extract_tool_summary(tool, tool_input)
        if summary:
            return f"{_BOLD_YELLOW}🔧 {tool}: {summary}{_RESET}\n"
        return f"{_BOLD_YELLOW}🔧 {tool}{_RESET}\n"
    if event == "tool_result":
        return ""
    if event == "stopped":
        return f"{_DIM_RED}[stopped]{_RESET}\n"
    if event == "finished":
        status = str(obj.get("status") or "completed")
        if status == "completed":
            return ""
        detail = str(obj.get("error") or status)
        return f"\n{_DIM_RED}[{status}] {detail}{_RESET}\n"

    return ""


def _render_message(obj: dict[str, Any]) -> str:
    """Render a single JSONL message object."""
    msg_type = obj.get("type")

    if msg_type == "user":
        return _render_user(obj)
    if msg_type == "assistant":
        return _render_assistant(obj)
    # Skip system, result, stream_event, etc.
    return ""


def _render_user(obj: dict[str, Any]) -> str:
    """Render a user message."""
    message = obj.get("message", {})
    content = message.get("content")
    if content is None:
        return ""

    # String content = the initial user prompt.
    if isinstance(content, str):
        truncated = content[:200] + ("..." if len(content) > 200 else "")
        return f"{_BOLD_CYAN}> {truncated}{_RESET}\n\n"

    # List content with tool_result blocks = tool responses.  Skip.
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return ""

    return ""


def _render_assistant(obj: dict[str, Any]) -> str:
    """Render an assistant message."""
    message = obj.get("message", {})
    content = message.get("content")
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text", "")
            if text.strip():
                parts.append(text + "\n")

        elif block_type == "tool_use":
            name = block.get("name", "unknown")
            tool_input = block.get("input", {})
            summary = extract_tool_summary(name, tool_input)
            if summary:
                parts.append(f"{_BOLD_YELLOW}🔧 {name}: {summary}{_RESET}\n")
            else:
                parts.append(f"{_BOLD_YELLOW}🔧 {name}{_RESET}\n")

        # Skip thinking blocks and other types.

    if parts:
        parts.append("\n")  # blank line between assistant turns
    return "".join(parts)
