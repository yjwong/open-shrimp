"""``/fork`` for OpenCode: copy a session through ``opencode serve``.

``POST /session/{id}/fork`` takes ``messageID`` as the first message to
*drop* (``Session.fork`` copies ``msgs.slice(0, index(messageID))``), while
the backend contract hands us the last message to *keep*.  An id the server
cannot find makes it copy everything, so the cut is looked up here first and
an unknown id fails the fork instead of copying a half-finished turn.
"""

from __future__ import annotations

from typing import Any

import httpx

from open_shrimp.backend.errors import CLIConnectionError, ProcessError
from open_shrimp.backend.opencode.errors import OpenCodeAuthError
from open_shrimp.backend.types import ForkResult


def first_dropped_id(
    rows: list[dict[str, Any]], at_message_id: str | None,
) -> str | None:
    """Translate "last message to keep" into OpenCode's "first to drop".

    ``None`` keeps everything: either no cut was asked for, or the cut is
    already the newest message.  Raises ``ValueError`` for an id that is not
    in *rows*.
    """
    if at_message_id is None:
        return None
    ids = [_row_id(row) for row in rows]
    try:
        index = ids.index(at_message_id)
    except ValueError:
        raise ValueError(
            f"Message {at_message_id} is not in the session"
        ) from None
    return ids[index + 1] if index + 1 < len(ids) else None


def last_exchange(
    rows: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """The last user prompt and the last assistant text after it."""
    reply: str | None = None
    for row in reversed(rows):
        info = row.get("info") if isinstance(row, dict) else None
        if not isinstance(info, dict):
            continue
        text = _row_text(row)
        if info.get("role") == "assistant" and reply is None:
            reply = text
        elif info.get("role") == "user" and text is not None:
            return text, reply
    return None, reply


def _row_id(row: Any) -> str | None:
    info = row.get("info") if isinstance(row, dict) else None
    return info.get("id") if isinstance(info, dict) else None


def _row_text(row: dict[str, Any]) -> str | None:
    """The visible text parts of one message.

    Synthetic parts are text OpenCode injected itself (file contents, tool
    hints), so they are not what anyone typed or read.
    """
    parts = row.get("parts")
    if not isinstance(parts, list):
        return None
    texts = [
        p["text"]
        for p in parts
        if isinstance(p, dict)
        and p.get("type") == "text"
        and not p.get("synthetic")
        and isinstance(p.get("text"), str)
    ]
    return "\n\n".join(texts).strip() or None


async def _request(
    http: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> Any:
    try:
        r = await http.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise CLIConnectionError(f"{method} {path} failed: {exc}") from exc
    if r.status_code == 401:
        raise OpenCodeAuthError("opencode serve rejected our credentials")
    if r.status_code >= 400:
        raise ProcessError(
            f"{method} {path} returned {r.status_code}: {r.text[:300]}"
        )
    return r.json() if r.content else None


async def fork(
    base_url: str,
    auth_header: str,
    session_id: str,
    directory: str,
    *,
    at_message_id: str | None,
) -> ForkResult:
    """Fork *session_id* on the server at *base_url*.

    *directory* rides as ``?directory=`` so the copy lands in the context's
    project, where ``/resume`` lists it.
    """
    params = {"directory": directory}
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=30.0,
        headers={"Authorization": auth_header},
    ) as http:
        info = await _request(
            http, "GET", f"/session/{session_id}", params=params,
        )
        payload = await _request(
            http, "GET", f"/session/{session_id}/message", params=params,
        )
        rows = payload.get("messages") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ProcessError(
                f"GET /session/{session_id}/message returned {payload!r}"
            )
        cut = first_dropped_id(rows, at_message_id)
        kept = rows
        if cut is not None:
            kept = rows[: [_row_id(r) for r in rows].index(cut)]

        body: dict[str, Any] = {"messageID": cut} if cut else {}
        created = await _request(
            http, "POST", f"/session/{session_id}/fork",
            params=params, json=body,
        )
    new_id = created.get("id") if isinstance(created, dict) else None
    if not new_id:
        raise ProcessError(
            f"POST /session/{session_id}/fork returned no id: {created!r}"
        )

    last_prompt, last_reply = last_exchange(kept)
    title = info.get("title") if isinstance(info, dict) else None
    return ForkResult(
        session_id=str(new_id),
        summary=title or None,
        last_prompt=last_prompt,
        last_reply=last_reply,
    )


async def delete(
    base_url: str, auth_header: str, session_id: str, directory: str,
) -> None:
    """Delete *session_id* on the server at *base_url*."""
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=30.0,
        headers={"Authorization": auth_header},
    ) as http:
        await _request(
            http, "DELETE", f"/session/{session_id}",
            params={"directory": directory},
        )
