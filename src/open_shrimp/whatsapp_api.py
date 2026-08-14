"""Signed HTTP endpoints for WhatsApp content pushed from the companion app.

The phone reads ``msgstore.db`` behind root and pushes what it finds; both
routes here are authenticated with the per-request Android device signature
and hand their payload to the live :class:`WhatsAppAdapter`.

They are separate routes rather than two modes of one, because their
contracts disagree.  ``/messages`` is a cursor contract — a drainable batch of
whatever the selected chats delivered, answered with the watermark the phone
may retire.  ``/handovers`` carries exactly one chat the user pointed at, is
atomic rather than drainable, retires nothing, and is answered with the topic
its card landed in.  Overloading one endpoint would put two incompatible
meanings on the same response field.

Neither route decides that anything is acted on.  A handover is authority to
deliver one chat, not to run it: the card it produces waits behind the same
Pick up button as every other event, and the context is the operator's choice.

This lives beside the other companion-facing surfaces rather than in
``events/``, which owns adapters and holds no transport dependency.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from open_shrimp.android_companion import authenticate_android_request
from open_shrimp.events.base import SupportsHandover, SupportsIngest
from open_shrimp.events.manager import get_active_adapter_of_type
from open_shrimp.review.auth import AuthError, read_json_body

logger = logging.getLogger(__name__)

# Every accepted row costs a Telegram round trip inside this request, so the
# batch has to be small enough to drain before the phone's client gives up and
# re-sends — which would stall the watermark instead of advancing it.
MAX_BATCH_ROWS = 50
# A handover costs one Telegram round trip regardless of its row count, so the
# ceiling is about what a transcript may claim rather than about draining.  It
# sits above the phone's own limit deliberately: a bound changed on one side
# then surfaces as a rejection rather than as a silently shortened chat.
MAX_HANDOVER_ROWS = 200
# The signature covers the raw body, so the body is buffered and hashed before
# anything can reject it.  Checking the declared length first turns an
# oversized push into a header read rather than a buffer, a hash, and a parse.
MAX_BATCH_BYTES = 4_000_000


def _message_rows(body: dict, limit: int) -> list[dict]:
    """The ``messages`` list from *body*, or raise ``AuthError`` saying why not.

    Every row is required to carry an integer ``id``: the adapter orders and
    acknowledges rows by it and may assume it is there.
    """
    rows = body.get("messages")
    if not isinstance(rows, list):
        raise AuthError(400, "messages must be a list")
    if len(rows) > limit:
        raise AuthError(413, f"batch exceeds {limit} messages")
    for row in rows:
        # A non-mapping raises TypeError here, so this covers the shape too.
        try:
            int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError(
                400, "each message must be a mapping with an integer 'id'"
            ) from exc
    return rows


def _validated_handover(body: dict) -> dict:
    """The handover payload from *body*, or raise ``AuthError`` saying why not.

    Only the shape is checked.  Nothing in here decides whether the chat is
    acted on — that is settled by the signature the request arrived under —
    so there is no field to validate for authority, and none is looked for.
    """
    chat = body.get("chat")
    jid = chat.get("jid") if isinstance(chat, dict) else None
    if not isinstance(jid, str) or "@" not in jid:
        raise AuthError(400, "chat.jid must be a JID string")
    return {
        "chat": chat,
        "messages": _message_rows(body, MAX_HANDOVER_ROWS),
        "truncated": bool(body.get("truncated")),
    }


async def upload_whatsapp_messages_endpoint(request: Request) -> JSONResponse:
    """POST /api/whatsapp/messages — ingest a batch, return the new cursor."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BATCH_BYTES:
        return JSONResponse({"error": "batch is too large"}, status_code=413)

    try:
        device = await authenticate_android_request(request)
        body = await read_json_body(request)
        adapter = get_active_adapter_of_type("whatsapp")
        if not isinstance(adapter, SupportsIngest):
            # No WhatsApp source is configured, or the bot is still starting.
            raise AuthError(503, "WhatsApp intake is not enabled on this server")
        rows = _message_rows(body, MAX_BATCH_ROWS)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    cursor = await adapter.ingest(rows)
    logger.info(
        "Ingested a WhatsApp batch of %d from device %s; cursor now %s",
        len(rows),
        device["device_id"],
        cursor,
    )
    return JSONResponse({"cursor": cursor})


async def upload_whatsapp_handover_endpoint(request: Request) -> JSONResponse:
    """POST /api/whatsapp/handovers — take one chat straight to a topic."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BATCH_BYTES:
        return JSONResponse({"error": "handover is too large"}, status_code=413)

    try:
        device = await authenticate_android_request(request)
        body = await read_json_body(request)
        adapter = get_active_adapter_of_type("whatsapp")
        if not isinstance(adapter, SupportsHandover):
            # No WhatsApp source is configured, or the bot is still starting.
            raise AuthError(503, "WhatsApp intake is not enabled on this server")
        payload = _validated_handover(body)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    outcome = await adapter.handover(payload)
    logger.info(
        "Handed over a WhatsApp chat of %d rows from device %s; event %s, topic %s",
        len(payload["messages"]),
        device["device_id"],
        outcome.event_id,
        outcome.thread_id,
    )
    if outcome.thread_id is None:
        # Delivery is best-effort and never raises, so a failure arrives as an
        # empty outcome.  Say so rather than hand back a link to nothing.
        return JSONResponse(
            {
                "error": "the chat could not be delivered to a topic",
                "event_id": outcome.event_id,
            },
            status_code=502,
        )
    return JSONResponse(
        {
            "event_id": outcome.event_id,
            "thread_id": outcome.thread_id,
            "deep_link": outcome.deep_link,
        }
    )


def create_whatsapp_routes() -> list[Route]:
    return [
        Route(
            "/api/whatsapp/messages",
            upload_whatsapp_messages_endpoint,
            methods=["POST"],
        ),
        Route(
            "/api/whatsapp/handovers",
            upload_whatsapp_handover_endpoint,
            methods=["POST"],
        ),
    ]
