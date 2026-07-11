"""Signed HTTP endpoint for meeting-transcript uploads from the companion app.

The phone POSTs the diarized transcript text (audio stays on the phone),
authenticated with the per-request Android device signature.  The row is
upserted (re-uploads after a redone diarization replace the transcript and
re-run notes) and handed to the live :class:`MeetingProcessor`.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from open_shrimp.android_companion import authenticate_android_request
from open_shrimp.db import upsert_meeting_job
from open_shrimp.meetings.processor import get_active_processor
from open_shrimp.review.auth import AuthError, read_json_body

logger = logging.getLogger(__name__)

_MAX_TRANSCRIPT_CHARS = 2_000_000


def _opt_int(body: dict, key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AuthError(400, f"{key} must be an integer") from exc


async def upload_meeting_transcript_endpoint(request: Request) -> JSONResponse:
    """POST /api/meetings/transcripts — accept a transcript, start processing."""
    try:
        device = await authenticate_android_request(request)
        body = await read_json_body(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    meeting_id = body.get("meeting_id")
    transcript = body.get("transcript")
    if not isinstance(meeting_id, str) or not meeting_id:
        return JSONResponse({"error": "meeting_id is required"}, status_code=400)
    if not isinstance(transcript, str) or not transcript.strip():
        return JSONResponse({"error": "transcript is required"}, status_code=400)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        return JSONResponse({"error": "transcript is too large"}, status_code=413)

    processor = get_active_processor()
    if processor is None:
        # Meetings are not configured, or the bot is still starting up.
        return JSONResponse(
            {"error": "Meetings are not enabled on this server"}, status_code=503
        )

    title = body.get("title")
    if not isinstance(title, str) or not title:
        title = f"Meeting {meeting_id}"

    try:
        started_at_ms = _opt_int(body, "started_at_ms")
        duration_ms = _opt_int(body, "duration_ms")
        speaker_count = _opt_int(body, "speaker_count") or 0
        word_count = _opt_int(body, "word_count") or 0
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    db = request.app.state.db
    job_id = await upsert_meeting_job(
        db,
        device_id=device["device_id"],
        meeting_id=meeting_id,
        title=title,
        started_at_ms=started_at_ms,
        duration_ms=duration_ms,
        speaker_count=speaker_count,
        word_count=word_count,
        transcript=transcript,
    )
    processor.enqueue(job_id)
    logger.info(
        "Accepted meeting transcript %s from device %s (job %d, %d chars)",
        meeting_id,
        device["device_id"],
        job_id,
        len(transcript),
    )
    return JSONResponse({"status": "processing"}, status_code=202)


def create_meetings_routes() -> list[Route]:
    return [
        Route(
            "/api/meetings/transcripts",
            upload_meeting_transcript_endpoint,
            methods=["POST"],
        ),
    ]
