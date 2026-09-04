"""HTTP endpoints for answering the agent from the Android app.

Two things park a turn on the human, and the companion's Live Update can
resolve either: a tool approval (inline approve/deny actions) and an
AskUserQuestion (option actions, or the bottom sheet the notification opens).
Both POST here, authenticated with the device's existing per-request
signature scheme (see
:func:`~open_shrimp.android_companion.authenticate_android_request`).

Neither routes through Telegram, and neither needs a bot token on the phone.
Each converges on the same ``asyncio.Future`` the Telegram buttons resolve —
``handlers/state._approval_futures`` for approvals, the ``_QuestionState``
future for questions — so there is a single source of truth per decision and
no second answer path to keep in sync.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from open_shrimp.android_companion import authenticate_android_request
from open_shrimp.review.auth import AuthError, read_json_body

logger = logging.getLogger(__name__)


async def resolve_agent_approval_endpoint(request: Request) -> JSONResponse:
    """POST /api/agent/approvals/{tool_use_id}  body: {"decision": "approve"|"deny"}.

    No-ops gracefully if the future is already resolved (e.g. the user tapped
    the Telegram button first), mirroring the "This approval has expired" path.
    """
    try:
        await authenticate_android_request(request)
        body = await read_json_body(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    tool_use_id = request.path_params["tool_use_id"]
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        return JSONResponse(
            {"error": "decision must be 'approve' or 'deny'"}, status_code=400
        )

    from open_shrimp.handlers.approval import resolve_approval_from_device

    if not await resolve_approval_from_device(
        tool_use_id, decision == "approve",
    ):
        # Already resolved or never existed — treat as a benign no-op so the
        # phone doesn't surface an error when the user was simply too late.
        return JSONResponse({"status": "expired"})
    logger.info(
        "Resolved agent approval %s via Android: %s", tool_use_id, decision,
    )
    return JSONResponse({"status": "resolved", "decision": decision})


async def answer_agent_question_endpoint(request: Request) -> JSONResponse:
    """POST /api/agent/questions/{question_id}.

    Body: ``{"option_indexes": [0, 2], "other_texts": ["…"]}``.  Options are
    named by position in the list the phone was pushed rather than by label,
    so an answer cannot miss by a character; ``other_texts`` carries whatever
    the user typed instead of picking.  Single-select questions send exactly
    one entry across the two lists.

    Answers "expired" rather than failing when the question is already gone,
    matching the approval endpoint: the user was simply too late, or answered
    in Telegram first.
    """
    try:
        await authenticate_android_request(request)
        body = await read_json_body(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    question_id = request.path_params["question_id"]
    raw_indexes = body.get("option_indexes", [])
    raw_others = body.get("other_texts", [])
    # bool is an int subclass, and JSON true would otherwise select option 1.
    if (
        not isinstance(raw_indexes, list)
        or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in raw_indexes
        )
        or not isinstance(raw_others, list)
        or not all(isinstance(t, str) for t in raw_others)
    ):
        return JSONResponse(
            {
                "error": "option_indexes must be integers and "
                         "other_texts strings",
            },
            status_code=400,
        )

    from open_shrimp.handlers.questions import resolve_question_from_device

    answer = await resolve_question_from_device(
        question_id, raw_indexes, [t for t in raw_others if t.strip()],
    )
    if answer is None:
        return JSONResponse({"status": "expired"})
    logger.info("Answered question %s via Android: %s", question_id, answer)
    return JSONResponse({"status": "resolved", "answer": answer})


def create_agent_status_routes() -> list[Route]:
    return [
        Route(
            "/api/agent/approvals/{tool_use_id}",
            resolve_agent_approval_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/agent/questions/{question_id}",
            answer_agent_question_endpoint,
            methods=["POST"],
        ),
    ]
