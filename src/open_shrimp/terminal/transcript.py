"""Parse agent JSONL transcripts into events for the terminal Mini App.

Agent background tasks write JSONL transcripts (one JSON object per line).
The Mini App renders them as a scrollable page of cards rather than a
terminal, so this module hands it structured events and leaves layout to
the client:

``{"kind": "prompt", "text"}``
    The task the agent was given, in full.
``{"kind": "text", "text"}``
    Assistant prose, as markdown.
``{"kind": "tool", "id", "name", "summary", "input"}``
    A tool call: the one-line summary for the collapsed row, and the
    complete input for the expanded one.
``{"kind": "tool_result", "id", "text", "is_error", "truncated"}``
    A tool call's output, matched to its call by ``id``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from open_shrimp.stream import extract_tool_summary

logger = logging.getLogger(__name__)

#: Tool output beyond this many characters is cut off.  A ``Read`` of a large
#: file returns the whole file, which would dwarf the transcript around it.
TOOL_RESULT_MAX_CHARS = 8000


def parse_transcript(raw_text: str) -> list[dict[str, Any]]:
    """Parse a complete JSONL transcript into events."""
    events: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        events.extend(_parse_line(line))
    return events


def parse_transcript_lines(raw_text: str) -> tuple[list[dict[str, Any]], str]:
    """Parse the complete lines of *raw_text*, returning events and remainder.

    The remainder is any trailing text without a newline: a line the CLI is
    still writing, to be prepended to the next chunk.
    """
    if not raw_text:
        return [], ""
    segments = raw_text.split("\n")
    remainder = segments.pop()
    events: list[dict[str, Any]] = []
    for seg in segments:
        events.extend(_parse_line(seg))
    return events, remainder


def _parse_line(line: str) -> list[dict[str, Any]]:
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return [{"kind": "text", "text": "*[unreadable transcript line]*"}]
    if not isinstance(obj, dict):
        return []
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if obj.get("type") == "user":
        return _parse_user(content)
    if obj.get("type") == "assistant":
        return _parse_assistant(content)
    # system, result, stream_event and the like carry nothing to show.
    return []


def _parse_user(content: Any) -> list[dict[str, Any]]:
    # String content is the prompt the agent was started with.
    if isinstance(content, str):
        return [{"kind": "prompt", "text": _unframe_workflow_task(content)}]
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            text = _tool_result_text(block.get("content"))
            events.append({
                "kind": "tool_result",
                "id": str(block.get("tool_use_id") or ""),
                "text": text[:TOOL_RESULT_MAX_CHARS],
                "is_error": bool(block.get("is_error")),
                "truncated": len(text) > TOOL_RESULT_MAX_CHARS,
            })
    return events


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


#: Ends the paragraph the CLI prepends to a workflow agent's task; the task
#: follows with every line indented by two spaces.
_WORKFLOW_FRAME_END = "The computed task text follows:\n"


def _unframe_workflow_task(prompt: str) -> str:
    """The task a workflow script gave its agent, without the CLI's
    "[Workflow harness — computed task]" preamble around it."""
    if not prompt.startswith("[Workflow harness"):
        return prompt
    _, found, task = prompt.partition(_WORKFLOW_FRAME_END)
    if not found:
        return prompt
    return "\n".join(line.removeprefix("  ") for line in task.splitlines())


def _parse_assistant(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                events.append({"kind": "text", "text": text})
        elif block_type == "tool_use":
            name = str(block.get("name") or "unknown")
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            events.append({
                "kind": "tool",
                "id": str(block.get("id") or ""),
                "name": name,
                "summary": extract_tool_summary(name, tool_input),
                "input": tool_input,
            })
        # Thinking blocks are skipped.
    return events
