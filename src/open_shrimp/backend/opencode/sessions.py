"""Session listing via OpenCode's ``GET /session`` endpoint."""

from __future__ import annotations

from pathlib import Path

from open_shrimp.backend.errors import ProcessError
from open_shrimp.backend.opencode._http import get_json, get_json_from_server
from open_shrimp.backend.sessions import SessionInfo


async def list_sessions(
    directory: str | Path,
    *,
    limit: int = 500,
    base_url: str | None = None,
    auth_header: str | None = None,
) -> list[SessionInfo]:
    """List sessions whose directory matches *directory*, newest first.

    ``directory`` is canonicalised before the request — OpenCode does
    an exact string match server-side. OpenCode silently ignores the
    ``offset`` query parameter, so callers paginate client-side.

    Returns backend-neutral ``backend.SessionInfo`` rows.  OpenCode has no
    equivalent for the vestigial fields (``custom_title`` / ``first_prompt``
    / ``git_branch`` / ``file_size`` / ``cwd`` / ``tag``), so they keep their
    defaults; the resume-detail renderer reads them uniformly across sources.
    """
    # Empty directory returns the global session list across every
    # project — a privacy leak we never want, so reject it explicitly.
    raw = str(directory)
    if not raw.strip():
        raise ValueError("list_sessions requires a non-empty directory")
    canonical = str(Path(raw).resolve())

    params = {"directory": canonical, "limit": limit}
    if base_url is not None or auth_header is not None:
        if base_url is None or auth_header is None:
            raise ValueError("base_url and auth_header must be provided together")
        rows = await get_json_from_server(
            base_url, auth_header, "/session", params=params,
        )
    else:
        rows = await get_json("/session", params=params)
    if not isinstance(rows, list):
        raise ProcessError(f"GET /session returned non-list: {rows!r}")

    out: list[SessionInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        if not sid:
            continue
        time_block = row.get("time") or {}
        out.append(
            SessionInfo(
                session_id=str(sid),
                summary=row.get("title") or "(no title)",
                last_modified=int(time_block.get("updated") or 0),
                created_at=(
                    int(time_block["created"])
                    if isinstance(time_block.get("created"), (int, float))
                    else None
                ),
            )
        )
    return out


__all__ = ["list_sessions"]
