"""``/share`` for Claude: read a session's JSONL transcript into a
``SessionTranscript``.

Three things a plain ``parentUuid`` walk from the leaf gets wrong:

* A compaction boundary has ``parentUuid: null`` and keeps the real parent in
  ``logicalParentUuid``, so the walk stops at the last compaction.  It is
  followed here, and the ``isCompactSummary`` prompt that replaced the earlier
  history is dropped instead.
* A tool result's parent is the assistant entry holding its ``tool_use``
  (``sourceToolAssistantUUID``), not the entry before it, so of several
  parallel calls only the last result sits on the walked chain.  Results are
  taken from anywhere whose parent is on the chain.
* Subagents write their own transcripts under ``<session>/subagents/``, tied
  to the spawning ``tool_use`` by an ``agent-<id>.meta.json`` sidecar.

Instruction files, hook output and reminders are ``attachment`` entries or
``isMeta`` prompts, and the system prompt is never written to the
transcript, so reading only user and assistant messages leaves them out.
"""

from __future__ import annotations

import difflib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from open_shrimp.backend.types import (
    SessionTranscript,
    TranscriptImage,
    TranscriptItem,
    TranscriptText,
    TranscriptToolCall,
)

logger = logging.getLogger(__name__)

#: Tools whose call spawns a subagent with its own transcript.
_SUBAGENT_TOOLS = frozenset({"Agent", "Task"})

#: Model id the CLI stamps on assistant entries it wrote itself (an
#: interruption notice, an API error), which no model answered.
_SYNTHETIC_MODEL = "<synthetic>"


def export_transcript(path: Path, session_id: str) -> SessionTranscript:
    """Read the transcript at *path* and its subagents into a transcript."""
    from claude_agent_sdk._internal.sessions import (
        _jsonl_to_lite,
        _parse_session_info_from_lite,
        _parse_transcript_entries,
    )

    content = path.read_text(encoding="utf-8", errors="replace")
    entries = _parse_transcript_entries(content)

    title: str | None = None
    try:
        info = _parse_session_info_from_lite(session_id, _jsonl_to_lite(content, 0))
        title = info.summary if info is not None else None
    except Exception:
        logger.debug("Could not read a title for %s", session_id, exc_info=True)

    models: list[str] = []
    items = _items(
        _main_entries(entries),
        _subagent_files(path.with_suffix("") / "subagents"),
        models,
        seen_files=set(),
    )
    return SessionTranscript(
        session_id=session_id, items=items, title=title, models=models,
    )


def _main_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries of the conversation that ends at the newest leaf.

    The chain runs leaf to root through ``parentUuid`` and across compaction
    boundaries through ``logicalParentUuid``.  Tool results hanging off a
    chain entry are added, and file order is kept: the CLI appends in the
    order things happened.
    """
    from claude_agent_sdk._internal.sessions import _build_conversation_chain

    chain = _build_conversation_chain(entries)
    if not chain:
        return []
    by_uuid = {e["uuid"]: e for e in entries}
    spine: set[str] = set()
    cur: dict[str, Any] | None = chain[-1]
    while cur is not None and cur["uuid"] not in spine:
        spine.add(cur["uuid"])
        parent = cur.get("parentUuid") or cur.get("logicalParentUuid")
        cur = by_uuid.get(parent) if parent else None

    return [
        e for e in entries
        if e["uuid"] in spine
        or (
            e.get("type") == "user"
            and e.get("parentUuid") in spine
            and _is_tool_result_entry(e)
        )
    ]


def _is_tool_result_entry(entry: dict[str, Any]) -> bool:
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _subagent_files(subagents_dir: Path) -> dict[str, Path]:
    """``tool_use`` id -> the transcript of the subagent that call spawned.

    Nested subagents are in the same tree, keyed by the ``tool_use`` in
    their parent subagent's transcript, so one map serves every depth.
    """
    from claude_agent_sdk._internal.sessions import (
        _collect_agent_files,
        _parent_ids_from_agent_metadata,
        _read_agent_metadata_sidecar,
    )

    if not subagents_dir.is_dir():
        return {}
    files: dict[str, Path] = {}
    for _agent_id, file_path in _collect_agent_files(subagents_dir):
        try:
            meta = _read_agent_metadata_sidecar(file_path)
        except OSError:
            continue
        tool_use_id, _parent_agent = _parent_ids_from_agent_metadata(meta)
        if tool_use_id is not None:
            files[tool_use_id] = file_path
    return files


def _subagent_items(
    path: Path,
    agents: dict[str, Path],
    models: list[str],
    seen_files: set[Path],
) -> list[TranscriptItem]:
    from claude_agent_sdk._internal.sessions import (
        _build_subagent_chain,
        _parse_transcript_entries,
    )

    if path in seen_files:
        return []
    seen_files.add(path)
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.debug("Could not read subagent transcript %s", path, exc_info=True)
        return []
    entries = _parse_transcript_entries(content)
    chain = {e["uuid"] for e in _build_subagent_chain(entries)}
    # Same rule as the main chain: parallel tool results hang off the
    # assistant entry that called them.
    kept = [
        e for e in entries
        if e["uuid"] in chain
        or (e.get("parentUuid") in chain and _is_tool_result_entry(e))
    ]
    return _items(kept, agents, models, seen_files=seen_files, sidechain=True)


def _items(
    entries: list[dict[str, Any]],
    agents: dict[str, Path],
    models: list[str],
    *,
    seen_files: set[Path],
    sidechain: bool = False,
) -> list[TranscriptItem]:
    items: list[TranscriptItem] = []
    calls: dict[str, TranscriptToolCall] = {}
    for entry in entries:
        kind = entry.get("type")
        if kind not in ("user", "assistant"):
            continue
        if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("teamName"):
            continue
        if entry.get("isSidechain") and not sidechain:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        timestamp = _parse_timestamp(entry.get("timestamp"))
        content = message.get("content")

        if kind == "user":
            if isinstance(content, str):
                if content.strip():
                    items.append(TranscriptText("user", content, timestamp))
                continue
            if not isinstance(content, list):
                continue
            texts: list[str] = []
            images: list[TranscriptImage] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_result":
                    call = calls.get(block.get("tool_use_id", ""))
                    if call is not None:
                        _fill_result(call, block, entry.get("toolUseResult"))
                elif btype == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif btype == "image":
                    image = _image(block)
                    if image is not None:
                        images.append(image)
            text = "\n\n".join(texts).strip()
            if text or images:
                items.append(TranscriptText("user", text, timestamp, images))
            continue

        model = message.get("model")
        if isinstance(model, str) and model != _SYNTHETIC_MODEL and model not in models:
            models.append(model)
        if isinstance(content, str):
            if content.strip():
                items.append(TranscriptText("assistant", content, timestamp))
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    items.append(TranscriptText("assistant", block["text"], timestamp))
            elif btype == "thinking":
                # Redacted reasoning keeps only its signature.
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    items.append(TranscriptText("thinking", thinking, timestamp))
            elif btype == "tool_use":
                call_id = str(block.get("id", ""))
                name = str(block.get("name", ""))
                tool_input = block.get("input")
                call = TranscriptToolCall(
                    name=name,
                    input=tool_input if isinstance(tool_input, dict) else {},
                    timestamp=timestamp,
                )
                if name in _SUBAGENT_TOOLS and call_id in agents:
                    call.subagent = _subagent_items(
                        agents[call_id], agents, models, seen_files,
                    )
                calls[call_id] = call
                items.append(call)
    return items


def _fill_result(
    call: TranscriptToolCall, block: dict[str, Any], tool_use_result: Any,
) -> None:
    call.is_error = bool(block.get("is_error"))
    content = block.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif ptype == "image":
                image = _image(part)
                if image is not None:
                    call.images.append(image)
            elif ptype == "tool_reference" and isinstance(part.get("tool_name"), str):
                texts.append(part["tool_name"])
    call.output = "\n".join(texts)
    if not call.is_error:
        call.diff = _diff(call.name, call.input, tool_use_result)


def _image(block: dict[str, Any]) -> TranscriptImage | None:
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str):
        return None
    return TranscriptImage(
        media_type=str(source.get("media_type") or "image/png"), data=data,
    )


def _diff(name: str, tool_input: dict[str, Any], result: Any) -> str | None:
    """A unified diff of what an Edit, MultiEdit, Write or NotebookEdit did.

    The CLI's own ``structuredPatch`` carries real line numbers, so it wins.
    A new file's patch is empty, and older transcripts have none; those fall
    back to diffing the tool input.
    """
    if name not in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return None
    hunks = result.get("structuredPatch") if isinstance(result, dict) else None
    if isinstance(hunks, list) and hunks:
        try:
            return "\n".join(
                f"@@ -{h['oldStart']},{h['oldLines']} +{h['newStart']},{h['newLines']} @@\n"
                + "\n".join(h["lines"])
                for h in hunks
            )
        except (KeyError, TypeError):
            logger.debug("Unreadable structuredPatch on %s", name, exc_info=True)

    if name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            return None
        return "\n".join(f"+{line}" for line in content.splitlines())

    if name == "MultiEdit":
        edits = tool_input.get("edits")
        pairs = [
            (e.get("old_string", ""), e.get("new_string", ""))
            for e in edits if isinstance(e, dict)
        ] if isinstance(edits, list) else []
    elif name == "NotebookEdit":
        pairs = [(tool_input.get("old_source", ""), tool_input.get("new_source", ""))]
    else:
        pairs = [(tool_input.get("old_string", ""), tool_input.get("new_string", ""))]

    bodies: list[str] = []
    for old, new in pairs:
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        lines = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm="",
        ))
        if lines:
            bodies.append("\n".join(lines[2:]))
    return "\n".join(bodies) or None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = ["export_transcript"]
