"""HTTP endpoint for resolving agent tool approvals from the Android app.

The companion's Live Update exposes inline approve/deny actions on the
permission-required segment.  Tapping one POSTs here, authenticated with the
device's existing per-request signature scheme (see
:func:`~open_shrimp.android_companion.authenticate_android_request`).

Approvals do **not** route through Telegram and need no bot token on the
phone.  Both triggers — the Telegram inline button and this endpoint —
converge on the same ``asyncio.Future`` in ``handlers/state._approval_futures``
(resolved server-side by the ``canUseTool`` callback), so there is a single
source of truth and no second approval decision path to keep in sync.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from open_shrimp.android_companion import authenticate_android_request
from open_shrimp.review.auth import AuthError
from open_shrimp.security_key.api import _json_body

logger = logging.getLogger(__name__)


async def resolve_agent_approval_endpoint(request: Request) -> JSONResponse:
    """POST /api/agent/approvals/{tool_use_id}  body: {"decision": "approve"|"deny"}.

    No-ops gracefully if the future is already resolved (e.g. the user tapped
    the Telegram button first), mirroring the "This approval has expired" path.
    """
    try:
        await authenticate_android_request(request)
        body = await _json_body(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    tool_use_id = request.path_params["tool_use_id"]
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        return JSONResponse(
            {"error": "decision must be 'approve' or 'deny'"}, status_code=400
        )

    # The keyboard sender registers the same future under both the
    # ``approve:`` and ``deny:`` callback keys; either resolves it.
    from open_shrimp.handlers.state import (
        RESOLVED_VIA_ANDROID,
        _approval_futures,
        _approval_resolved_via,
    )

    future = _approval_futures.get(f"approve:{tool_use_id}")
    if future is None or future.done():
        # Already resolved or never existed — treat as a benign no-op so the
        # phone doesn't surface an error when the user was simply too late.
        return JSONResponse({"status": "expired"})

    _approval_resolved_via[tool_use_id] = RESOLVED_VIA_ANDROID
    future.set_result(decision == "approve")
    logger.info(
        "Resolved agent approval %s via Android: %s", tool_use_id, decision,
    )
    return JSONResponse({"status": "resolved", "decision": decision})


def create_agent_status_routes() -> list[Route]:
    return [
        Route(
            "/api/agent/approvals/{tool_use_id}",
            resolve_agent_approval_endpoint,
            methods=["POST"],
        ),
    ]
