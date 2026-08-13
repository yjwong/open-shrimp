"""Signed HTTP endpoint for WhatsApp message batches from the companion app.

The phone POSTs rows it read out of ``msgstore.db`` behind root, already
narrowed to the chats selected in the companion UI — messages from every other
chat never leave the device.  Each batch is authenticated with the per-request
Android device signature, handed to the live :class:`WhatsAppAdapter`, and
answered with the watermark the phone may advance its cursor to.

This lives beside the other companion-facing surfaces rather than in
``events/``, which owns adapters and holds no transport dependency.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from open_shrimp.android_companion import authenticate_android_request
from open_shrimp.events.base import SupportsIngest
from open_shrimp.events.manager import get_active_adapter_of_type
from open_shrimp.review.auth import AuthError, read_json_body

logger = logging.getLogger(__name__)

# Every accepted row costs a Telegram round trip inside this request, so the
# batch has to be small enough to drain before the phone's client gives up and
# re-sends — which would stall the watermark instead of advancing it.
MAX_BATCH_ROWS = 50
# The signature covers the raw body, so the body is buffered and hashed before
# anything can reject it.  Checking the declared length first turns an
# oversized push into a header read rather than a buffer, a hash, and a parse.
MAX_BATCH_BYTES = 4_000_000


def _validated_rows(body: dict) -> list[dict]:
    """The message rows from *body*, or raise ``AuthError`` describing why not."""
    rows = body.get("messages")
    if not isinstance(rows, list):
        raise AuthError(400, "messages must be a list")
    if len(rows) > MAX_BATCH_ROWS:
        raise AuthError(413, f"batch exceeds {MAX_BATCH_ROWS} messages")
    for row in rows:
        # A non-mapping raises TypeError here, so this covers the shape too.
        try:
            int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError(
                400, "each message must be a mapping with an integer 'id'"
            ) from exc
    return rows


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
        rows = _validated_rows(body)
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


def create_whatsapp_routes() -> list[Route]:
    return [
        Route(
            "/api/whatsapp/messages",
            upload_whatsapp_messages_endpoint,
            methods=["POST"],
        ),
    ]
