"""Framing for the control channel between the agent core and a local UI.

Newline-delimited JSON in both directions.  A client sends request frames
carrying an ``id``; the server answers with a frame carrying the same ``id``
and either ``result`` or ``error``.  The server also emits unsolicited frames
carrying ``event`` and no ``id``, so a UI learns about state changes it did
not initiate — a ``/restart`` issued from Telegram, for instance.

The channel carries no untrusted content: it exposes process control only,
never conversation or event text.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

PROTOCOL_VERSION = 1

ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_METHOD = "unknown_method"
ERR_INTERNAL = "internal"

# A frame longer than this is a malfunctioning or hostile client, not a
# request: every legitimate frame is a short JSON object.
MAX_FRAME_BYTES = 64 * 1024


def encode(message: Mapping[str, Any]) -> bytes:
    """Serialise one frame, newline-terminated."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    """Parse one frame.

    Raises :class:`ValueError` when the line is not a JSON object.
    """
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("frame must be a JSON object")
    return message


def response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"id": request_id, "result": result}


def error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "error": {"code": code, "message": message}}


def event(name: str, data: Any = None) -> dict[str, Any]:
    return {"event": name, "data": data}
