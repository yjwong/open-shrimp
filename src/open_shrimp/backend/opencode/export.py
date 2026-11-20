"""``/share`` for OpenCode: read a session through ``opencode serve``.

``GET /session/{id}/message`` returns ``{info, parts}`` rows.  A compaction
is a user message holding a ``compaction`` part followed by an assistant
message flagged ``summary``; both are dropped, and the messages before them
stay in the list, so nothing else is needed to show the whole session.
Synthetic and ignored text parts are what OpenCode injected itself (file
contents, tool hints) and are dropped too.

A ``task`` call runs its subagent in a child session named by
``state.metadata.sessionId``, which is fetched the same way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from open_shrimp.backend.errors import ProcessError
from open_shrimp.backend.opencode.fork import request
from open_shrimp.backend.types import (
    SessionTranscript,
    TranscriptImage,
    TranscriptItem,
    TranscriptText,
    TranscriptToolCall,
)

logger = logging.getLogger(__name__)

#: The tool whose call runs a subagent in a child session.
_SUBAGENT_TOOL = "task"


async def export(
    base_url: str, auth_header: str, session_id: str, directory: str,
) -> SessionTranscript:
    """Read *session_id* and its subagents from the server at *base_url*."""
    params = {"directory": directory}
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=60.0,
        headers={"Authorization": auth_header},
    ) as http:
        info = await request(http, "GET", f"/session/{session_id}", params=params)
        models: list[str] = []
        items = await _session_items(
            http, session_id, params, models, seen={session_id}, strict=True,
        )
    title = info.get("title") if isinstance(info, dict) else None
    return SessionTranscript(
        session_id=session_id, items=items, title=title or None, models=models,
    )


async def _session_items(
    http: httpx.AsyncClient,
    session_id: str,
    params: dict[str, str],
    models: list[str],
    *,
    seen: set[str],
    strict: bool = False,
) -> list[TranscriptItem]:
    """The items of one session.

    *strict* is for the shared session itself.  A subagent's session that
    cannot be read leaves its call without a transcript rather than failing
    the share.
    """
    try:
        payload = await request(
            http, "GET", f"/session/{session_id}/message", params=params,
        )
    except Exception:
        if strict:
            raise
        logger.debug("Could not read subagent session %s", session_id, exc_info=True)
        return []
    rows = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        if strict:
            raise ProcessError(
                f"GET /session/{session_id}/message returned {payload!r}"
            )
        return []

    items: list[TranscriptItem] = []
    for row in rows:
        info = row.get("info") if isinstance(row, dict) else None
        parts = row.get("parts") if isinstance(row, dict) else None
        if not isinstance(info, dict) or not isinstance(parts, list):
            continue
        parts = [p for p in parts if isinstance(p, dict)]
        created = _from_ms((info.get("time") or {}).get("created"))
        role = info.get("role")

        if role == "user":
            if any(p.get("type") == "compaction" for p in parts):
                continue
            texts = [
                p["text"] for p in parts
                if p.get("type") == "text"
                and not p.get("synthetic") and not p.get("ignored")
                and isinstance(p.get("text"), str)
            ]
            images = [
                image for p in parts
                if p.get("type") == "file"
                for image in [_image(p)] if image is not None
            ]
            text = "\n\n".join(texts).strip()
            if text or images:
                items.append(TranscriptText("user", text, created, images))
            continue

        if role != "assistant" or info.get("summary"):
            continue
        provider, model = info.get("providerID"), info.get("modelID")
        if isinstance(model, str) and model:
            name = f"{provider}/{model}" if provider else model
            if name not in models:
                models.append(name)
        for part in parts:
            ptype = part.get("type")
            stamp = _from_ms((part.get("time") or {}).get("start")) or created
            if ptype == "text":
                text = part.get("text")
                if (
                    isinstance(text, str) and text.strip()
                    and not part.get("synthetic") and not part.get("ignored")
                ):
                    items.append(TranscriptText("assistant", text, stamp))
            elif ptype == "reasoning":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    items.append(TranscriptText("thinking", text, stamp))
            elif ptype == "tool":
                items.append(
                    await _tool_call(http, part, created, params, models, seen)
                )
    return items


async def _tool_call(
    http: httpx.AsyncClient,
    part: dict[str, Any],
    created: datetime | None,
    params: dict[str, str],
    models: list[str],
    seen: set[str],
) -> TranscriptToolCall:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    name = str(part.get("tool", ""))
    tool_input = state.get("input")
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    status = state.get("status")
    call = TranscriptToolCall(
        name=name,
        input=tool_input if isinstance(tool_input, dict) else {},
        timestamp=_from_ms((state.get("time") or {}).get("start")) or created,
    )
    if status == "completed":
        output = state.get("output")
        call.output = output if isinstance(output, str) else ""
        call.images = [
            image for a in state.get("attachments") or []
            if isinstance(a, dict)
            for image in [_image(a)] if image is not None
        ]
        call.diff = _diff(name, call.input, metadata)
    elif status == "error":
        error = state.get("error")
        call.output = error if isinstance(error, str) else ""
        call.is_error = True

    child = metadata.get("sessionId")
    if name == _SUBAGENT_TOOL and isinstance(child, str) and child not in seen:
        seen.add(child)
        call.subagent = await _session_items(http, child, params, models, seen=seen)
    return call


def _diff(name: str, tool_input: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    """The unified diff an ``edit`` or ``apply_patch`` reported, or a new
    file's lines for ``write``, which reports none."""
    diff = metadata.get("diff")
    if name in ("edit", "apply_patch") and isinstance(diff, str) and diff.strip():
        return diff
    if name == "write":
        content = tool_input.get("content")
        if isinstance(content, str):
            return "\n".join(f"+{line}" for line in content.splitlines())
    return None


def _image(part: dict[str, Any]) -> TranscriptImage | None:
    """An image file part whose bytes ride inline as a ``data:`` URL.

    A part pointing at a ``file://`` path refers to a file on the server's
    host, which a shared page cannot reach.
    """
    mime = part.get("mime")
    url = part.get("url")
    if not isinstance(mime, str) or not mime.startswith("image/"):
        return None
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    if not header.endswith(";base64") or not data:
        return None
    return TranscriptImage(media_type=mime, data=data)


def _from_ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


__all__ = ["export"]
