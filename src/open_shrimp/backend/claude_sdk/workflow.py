"""Rendering for Claude Code's Workflow tool and its ``local_workflow`` task.

A Workflow call carries a JavaScript script whose ``export const meta = {…}``
literal names the run and its phases.  Once launched it is a background task:
``task_started`` with ``task_type="local_workflow"``, then ``task_progress``
events whose ``workflow_progress`` field is a full snapshot of every phase and
agent the script has announced so far.  The CLI omits the field from progress
events that only move an agent's token count, so a snapshot is only present
when a phase or an agent changed state.
"""

from __future__ import annotations

import json
import re
from typing import Any

from open_shrimp.markdown import (
    RICH_MAX_BODY,
    escape_rich,
    escape_rich_inline,
    rich_code_block,
)
from open_shrimp.tool_cards import plural

WORKFLOW_TOOL_NAME = "Workflow"
WORKFLOW_TASK_TYPE = "local_workflow"

#: The description the CLI gives a workflow task whose script has no summary.
_DEFAULT_TASK_DESCRIPTION = "Dynamic workflow"

#: Running agents listed under their phase before the rest collapse to "+N".
_RUNNING_AGENTS_SHOWN = 3

#: A running agent's last tool summary is cut to this, keeping its line to
#: one row on a phone.
_TOOL_SUMMARY_MAX_CHARS = 40

_ARGS_MAX_CHARS = 200

#: task_id -> the run's transcript directory as the CLI reported it (a guest
#: path for a sandboxed context).  The directory holds one
#: ``agent-<agentId>.jsonl`` per agent the script spawned, written live;
#: the SDK stream carries none of those agents' messages.
_transcript_dirs: dict[str, str] = {}

_META_RE = re.compile(r"export\s+const\s+meta\s*=\s*\{")
_STRING_FIELD_RE = r"\b{key}\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1"


def _code(text: str) -> str:
    """An inline code span, which takes its content literally."""
    return "`" + " ".join(text.replace("`", "'").split()) + "`"


def _meta_literal(script: str) -> str:
    """The text of the ``meta`` literal, up to the first line that closes it.

    ``meta`` must be a pure literal, so its closing brace sits at the start
    of a line in every script the model writes; nested ``phases`` objects
    close on indented lines and do not end the scan.
    """
    match = _META_RE.search(script)
    if match is None:
        return ""
    end = script.find("\n}", match.end())
    return script[match.end():end if end != -1 else len(script)]


def _string_field(literal: str, key: str) -> str:
    match = re.search(_STRING_FIELD_RE.format(key=key), literal, re.DOTALL)
    return match.group(2) if match else ""


def _string_fields(literal: str, key: str) -> list[str]:
    return [
        m.group(2)
        for m in re.finditer(_STRING_FIELD_RE.format(key=key), literal, re.DOTALL)
    ]


def summarize_call(tool_input: dict[str, Any]) -> str:
    """One-line summary for the tool row: the run's name, else its source."""
    if tool_input.get("resumeFromRunId"):
        return f"resume {tool_input['resumeFromRunId']}"
    script = tool_input.get("script") or ""
    name = _string_field(_meta_literal(script), "name") if script else ""
    return name or tool_input.get("name") or tool_input.get("scriptPath") or ""


def format_approval(tool_input: dict[str, Any], *, expanded: bool) -> str:
    """The approval card: what the run is called, its phases, where it came from.

    The script is shown only when *expanded*, and clipped to one message; the
    "View script" Mini App carries it whole.
    """
    script = tool_input.get("script") or ""
    literal = _meta_literal(script)
    name = _string_field(literal, "name")
    description = _string_field(literal, "description")
    # ``meta`` may carry a ``title`` of its own, so phase titles are read
    # only from the ``phases`` array onwards.
    phases_at = re.search(r"\bphases\s*:", literal)
    phases = (
        _string_fields(literal[phases_at.end():], "title") if phases_at else []
    )

    header = "\U0001f9e9 **Workflow**"
    if name:
        header += f" {_code(name)}"
    parts = [header]
    if description:
        parts.append(escape_rich(description))
    if phases:
        parts.append(
            "**Phases:** "
            + " → ".join(escape_rich_inline(title) for title in phases)
        )

    source_rows: list[str] = []
    if tool_input.get("resumeFromRunId"):
        source_rows.append(
            f"**Resumes run:** {_code(str(tool_input['resumeFromRunId']))}"
        )
    if tool_input.get("scriptPath"):
        source_rows.append(
            f"**Script file:** {_code(str(tool_input['scriptPath']))}"
        )
    elif tool_input.get("name") and not script:
        source_rows.append(
            f"**Saved workflow:** {_code(str(tool_input['name']))}"
        )
    if "args" in tool_input:
        args = json.dumps(tool_input["args"], ensure_ascii=False)
        if len(args) > _ARGS_MAX_CHARS:
            args = args[:_ARGS_MAX_CHARS] + "..."
        source_rows.append(f"**Args:** {_code(args)}")
    if source_rows:
        parts.append("<br>".join(source_rows))

    if expanded and script:
        clipped = script
        if len(clipped) > RICH_MAX_BODY:
            clipped = clipped[:RICH_MAX_BODY] + "\n..."
        parts.append(rich_code_block(clipped, "js"))
    return "\n\n".join(parts)


def script_document(tool_input: dict[str, Any]) -> str | None:
    """The script as a Markdown document for the preview Mini App."""
    script = tool_input.get("script")
    if not script:
        return None
    fence = "`" * max(3, _longest_backtick_run(script) + 1)
    return f"{fence}js\n{script}\n{fence}\n"


def _longest_backtick_run(text: str) -> int:
    return max((len(m) for m in re.findall(r"`+", text)), default=0)


def task_description(description: str | None, workflow_name: str | None) -> str | None:
    """The ⏳ line for a workflow task.

    The CLI fills the description from the script's summary and falls back to
    "Dynamic workflow", which says nothing once two runs share a chat, so the
    ``meta.name`` stands in for it.
    """
    if not workflow_name:
        return description
    if not description or description == _DEFAULT_TASK_DESCRIPTION:
        return workflow_name
    return f"{workflow_name}: {description}"


def record_launch(tool_use_result: Any) -> None:
    """Remember a launched run's transcript directory under its task id.

    The Workflow tool result is the only place the CLI names the directory;
    the task's ``.output`` file is written once, when the run ends, and
    carries the agents' ids but not where their transcripts are.
    """
    if not isinstance(tool_use_result, dict):
        return
    if tool_use_result.get("taskType") != WORKFLOW_TASK_TYPE:
        return
    task_id = tool_use_result.get("taskId")
    transcript_dir = tool_use_result.get("transcriptDir")
    if isinstance(task_id, str) and isinstance(transcript_dir, str):
        _transcript_dirs[task_id] = transcript_dir


def transcript_dir(task_id: str) -> str | None:
    """The transcript directory recorded for a run's task, if any."""
    return _transcript_dirs.get(task_id)


def _agent_bucket(agent: dict[str, Any]) -> str:
    state = agent.get("state")
    if state == "done":
        return "done"
    if state == "error":
        return "failed"
    return "running"


def render_progress(data: dict[str, Any]) -> str | None:
    """One line per phase from a ``task_progress`` payload, or None.

    None when the event carries no snapshot, which the CLI does for progress
    that only moves token counts.
    """
    snapshot = data.get("workflow_progress")
    if not isinstance(snapshot, list):
        return None

    phase_titles: dict[int, str] = {}
    agents_by_phase: dict[int | None, list[dict[str, Any]]] = {}
    for entry in snapshot:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "workflow_phase":
            phase_titles[entry.get("index", 0)] = str(entry.get("title", ""))
        elif entry.get("type") == "workflow_agent":
            agents_by_phase.setdefault(entry.get("phaseIndex"), []).append(entry)

    order: list[int | None] = sorted(phase_titles)
    order += [key for key in agents_by_phase if key not in phase_titles]
    if not order:
        return None

    lines: list[str] = []
    for key in order:
        agents = agents_by_phase.get(key, [])
        title = phase_titles.get(key) if key is not None else None
        if title is None and agents:
            title = str(agents[0].get("phaseTitle") or "")
        lines.append(_phase_line(title or "Agents", agents))
    return "<br>".join(lines)


def _running_line(agent: dict[str, Any]) -> str:
    """``↳ label · Tool `summary``` for an agent still working.

    The tool is the last one the agent called when the snapshot was taken;
    calls made between two snapshots never appear.
    """
    line = "↳ " + escape_rich_inline(str(agent.get("label") or "agent"))
    tool = agent.get("lastToolName")
    if tool:
        line += f" · {escape_rich_inline(str(tool))}"
        summary = " ".join(str(agent.get("lastToolSummary") or "").split())
        if summary:
            if len(summary) > _TOOL_SUMMARY_MAX_CHARS:
                summary = summary[:_TOOL_SUMMARY_MAX_CHARS] + "..."
            line += f" {_code(summary)}"
    return line


def _phase_line(title: str, agents: list[dict[str, Any]]) -> str:
    counts = {"done": 0, "failed": 0, "running": 0}
    running: list[dict[str, Any]] = []
    for agent in agents:
        bucket = _agent_bucket(agent)
        counts[bucket] += 1
        if bucket == "running":
            running.append(agent)

    if not agents:
        icon = "▫️"
    elif counts["running"]:
        icon = "⏳"
    elif counts["failed"]:
        icon = "⚠️"
    else:
        icon = "✅"

    line = f"{icon} **{escape_rich_inline(title)}**"
    facts: list[str] = []
    if agents:
        facts.append(f"{counts['done']}/{plural(len(agents), 'agent')} done")
    if counts["failed"]:
        facts.append(f"{counts['failed']} failed")
    if facts:
        line += " — " + " · ".join(facts)
    for agent in running[:_RUNNING_AGENTS_SHOWN]:
        line += "<br>" + _running_line(agent)
    hidden = len(running) - _RUNNING_AGENTS_SHOWN
    if hidden > 0:
        line += f"<br>↳ +{hidden} more"
    return line


__all__ = [
    "WORKFLOW_TASK_TYPE",
    "WORKFLOW_TOOL_NAME",
    "format_approval",
    "record_launch",
    "render_progress",
    "script_document",
    "summarize_call",
    "task_description",
    "transcript_dir",
]
