"""Signed HTTP endpoint for a LinkedIn conversation handed over from the phone.

One route.  The bubble over the LinkedIn app captures the thread on screen and
POSTs it here under the per-request Android device signature; the payload goes
to the live :class:`LinkedInAdapter`, and the response says which topic the
card landed in.

There is no ``/messages`` counterpart and no cursor.  A handover is atomic,
carries exactly the conversation a person pointed at, and retires nothing, so
there is no watermark for the phone to advance.

The route decides nothing about whether the thread is acted on.  The signature
is authority to deliver it; the card then waits behind the same Pick up button
as every other event, and the context is the operator's choice.

This lives beside the other companion-facing surfaces rather than in
``events/``, which owns adapters and holds no transport dependency.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from open_shrimp.android_companion import authenticate_android_request
from open_shrimp.events.base import Delivery, SupportsHandover
from open_shrimp.events.manager import get_active_adapter_of_type
from open_shrimp.review.auth import (
    AuthError,
    read_json_body,
    reject_oversized_body,
)

logger = logging.getLogger(__name__)

# A handover costs one Telegram round trip regardless of how many messages it
# carries, so the ceiling is about what a transcript may claim rather than
# about draining a queue.  It sits above the phone's own limit deliberately: a
# bound changed on one side then surfaces as a rejection rather than as a
# silently shortened conversation.
MAX_HANDOVER_MESSAGES = 200
# A one-to-one thread has two; the widest group thread seen in a real store
# has fifteen.  This bounds a malformed push, not a real conversation.
MAX_PARTICIPANTS = 64
# Bounds the declared body, checked before the body is read.
MAX_HANDOVER_BYTES = 4_000_000


def _messages(body: dict) -> list[dict]:
    """The ``messages`` list from *body*, or raise ``AuthError`` saying why not.

    Order is the contract: oldest first, as captured.  A screen capture has no
    id to sort on, so the host does not re-order what arrives.
    """
    rows = body.get("messages")
    if not isinstance(rows, list):
        raise AuthError(400, "messages must be a list")
    if not rows:
        raise AuthError(400, "a handover must carry at least one message")
    if len(rows) > MAX_HANDOVER_MESSAGES:
        raise AuthError(413, f"handover exceeds {MAX_HANDOVER_MESSAGES} messages")
    for row in rows:
        if not isinstance(row, dict):
            raise AuthError(400, "each message must be a mapping")
        if not isinstance(row.get("text", ""), str):
            raise AuthError(400, "message text must be a string")
    return rows


def _participants(body: dict) -> list[dict]:
    """The ``participants`` list from *body*, defaulting to none.

    Absent for a capture the store reader could not serve, which is a
    degradation the transcript reports rather than a failure.
    """
    rows = body.get("participants")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise AuthError(400, "participants must be a list")
    if len(rows) > MAX_PARTICIPANTS:
        raise AuthError(413, f"handover exceeds {MAX_PARTICIPANTS} participants")
    for row in rows:
        if not isinstance(row, dict):
            raise AuthError(400, "each participant must be a mapping")
    return rows


def _validated_handover(body: dict) -> dict:
    """The handover payload from *body*, or raise ``AuthError`` saying why not.

    Only the shape is checked.  Nothing in here decides whether the thread is
    acted on — that is settled by the signature the request arrived under — so
    there is no field to validate for authority, and none is looked for.

    ``conversation.title`` is the one field required of every capture: the
    thread screen always carries ``messaging_toolbar_title``, and it is what
    names the card and the topic spawned from it.
    """
    conversation = body.get("conversation")
    title = conversation.get("title") if isinstance(conversation, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise AuthError(400, "conversation.title must be a non-empty string")
    return {
        "conversation": conversation,
        "participants": _participants(body),
        "messages": _messages(body),
        "truncated": bool(body.get("truncated")),
        "store_read": bool(body.get("store_read")),
    }


async def upload_linkedin_handover_endpoint(request: Request) -> JSONResponse:
    """POST /api/linkedin/handovers — take one conversation to a topic."""
    try:
        reject_oversized_body(request, MAX_HANDOVER_BYTES)
        device = await authenticate_android_request(request)
        body = await read_json_body(request)
        adapter = get_active_adapter_of_type("linkedin")
        if not isinstance(adapter, SupportsHandover):
            # No LinkedIn source is configured, or the bot is still starting.
            raise AuthError(503, "LinkedIn intake is not enabled on this server")
        payload = _validated_handover(body)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    outcome = await adapter.handover(payload)
    logger.info(
        "Handed over a LinkedIn thread of %d messages from device %s; "
        "event %s, topic %s, %s",
        len(payload["messages"]),
        device["device_id"],
        outcome.event_id,
        outcome.thread_id,
        outcome.status.name.lower(),
    )
    if outcome.status is Delivery.FAILED:
        # Delivery is best-effort and never raises, so a failure arrives as an
        # outcome.  Say so rather than hand back a link to nothing.
        return JSONResponse(
            {"error": "the conversation could not be delivered to a topic"},
            status_code=502,
        )
    # A duplicate is the same thread re-tapped with nothing new on it.  Its
    # card is already in the topic, so this is a success with nothing new to
    # open, and the phone can say so instead of reporting a failure.
    return JSONResponse(
        {
            "duplicate": outcome.status is Delivery.DUPLICATE,
            "event_id": outcome.event_id,
            "thread_id": outcome.thread_id,
            "deep_link": outcome.deep_link,
        }
    )


def create_linkedin_routes() -> list[Route]:
    return [
        Route(
            "/api/linkedin/handovers",
            upload_linkedin_handover_endpoint,
            methods=["POST"],
        ),
    ]
