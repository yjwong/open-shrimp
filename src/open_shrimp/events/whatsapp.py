"""WhatsApp adapter: message rows pushed from the Android companion.

The host holds no WhatsApp data and never talks to WhatsApp.  The companion
app reads ``msgstore.db`` on the phone behind root, keeps only the chats
selected there, and POSTs batches to the upload endpoint; this adapter is the
passive receiving end.  ``start`` only records ``emit`` — nothing polls and
nothing connects, and the endpoint finds the adapter through
``EventManager.get_adapter_of_type`` rather than a registry of its own.

Restart-durable dedup lives on the phone, which advances its ``message._id``
watermark only after the host accepts a batch (the sink's own dedup is an
in-memory LRU that a restart wipes).  So every row this module refuses must
still be reported as accepted, or the phone re-sends it forever; ``ingest``
returns the highest id it is done with, not the highest it emitted.
"""

import logging
from typing import Any

from open_shrimp.config import EventSourceConfig
from open_shrimp.events.base import EmitFn
from open_shrimp.events.types import Event

logger = logging.getLogger(__name__)

# Rows the phone is expected to send, per the verified message_type census.
# Anything else is dropped rather than guessed at: a dozen rare types remain
# unidentified, and an allowlist that fails closed keeps them out of the feed.
MEDIA_PLACEHOLDERS: dict[int, str] = {
    1: "[image]",
    2: "[voice note]",
    3: "[video]",
    4: "[contact]",
    5: "[location]",
    9: "[document]",
    13: "[gif]",
    20: "[sticker]",
}
TEXT_TYPE = 0
DOCUMENT_TYPE = 9
ACCEPTED_TYPES = frozenset({TEXT_TYPE, *MEDIA_PLACEHOLDERS})

# JID servers that identify a single person rather than a group or a feed.
_DIRECT_SERVERS = frozenset({"s.whatsapp.net", "lid"})

# A guard against an oversized database blob, not a display policy — the sink
# owns chunking to Telegram's limit.  Applied to what is stored as well as to
# what is rendered, so the two cannot diverge.
MAX_TEXT_CHARS = 16_000
_BOUNDED_FIELDS = ("text", "caption")


def _text(value: object) -> str | None:
    """A non-empty, stripped string, or None for anything else."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _message_type(row: dict) -> int | None:
    """The row's message type, or None when it is absent or malformed."""
    try:
        return int(row["message_type"])
    except (KeyError, TypeError, ValueError):
        return None


def chat_server(row: dict) -> str | None:
    """The JID server of the row's chat: ``g.us``, ``s.whatsapp.net``, ``lid``.

    Derived from ``chat_jid`` rather than carried as its own field, so the two
    cannot disagree — a row claiming a direct server for a group JID would
    otherwise hand a group id back as a trusted sender.
    """
    chat_jid = _text(row.get("chat_jid"))
    if chat_jid is None or "@" not in chat_jid:
        return None
    return chat_jid.rsplit("@", 1)[-1]


def _placeholder(mtype: int | None, row: dict) -> str:
    """Bracketed stand-in for a media row, with the filename when useful."""
    if mtype == DOCUMENT_TYPE:
        path = _text(row.get("file_path"))
        if path:
            return f"[document: {path.rsplit('/', 1)[-1]}]"
    return MEDIA_PLACEHOLDERS.get(mtype, "[media]")


def message_text(row: dict) -> str | None:
    """Body for a row: its text, or a placeholder plus any media caption."""
    mtype = _message_type(row)
    if mtype == TEXT_TYPE:
        body = _text(row.get("text"))
    else:
        placeholder = _placeholder(mtype, row)
        caption = _text(row.get("caption"))
        body = f"{placeholder} {caption}" if caption else placeholder
    return body[:MAX_TEXT_CHARS] if body else None


def resolve_sender_id(row: dict) -> str | None:
    """The stable identity that may gate trust, or None to fail closed.

    The phone has already resolved LIDs to phone JIDs through ``jid_map``; it
    holds that table and the host never sees it.  Resolution succeeds for
    almost every sender, but a LID that does not resolve is still a stable id,
    so ``trusted_senders`` accepts either form.  A display name is never a
    fallback — it is attacker-controlled.

    Rows in one-to-one chats carry no per-row sender: the counterparty is the
    chat itself.  That substitution is only sound when the chat JID names a
    person, so a group row missing its sender resolves to None — inbound group
    messages always carry a real sender, so this fails closed on malformed
    input rather than on anything WhatsApp legitimately produces.
    """
    sender_jid = _text(row.get("sender_jid"))
    if sender_jid:
        return sender_jid
    chat_jid = _text(row.get("chat_jid"))
    if chat_jid and chat_server(row) in _DIRECT_SERVERS:
        return chat_jid
    return None


def format_sender(row: dict) -> str | None:
    """Human-readable sender, group-qualified.  Display only, untrusted.

    Deliberately independent of :func:`resolve_sender_id`: that function
    encodes a trust policy, and tightening it must not silently change what
    the topic header reads.
    """
    who = (
        _text(row.get("sender_name"))
        or _text(row.get("sender_jid"))
        or _text(row.get("chat_jid"))
    )
    subject = _text(row.get("chat_subject"))
    if chat_server(row) == "g.us" and subject:
        return f"group {subject} / {who}" if who else f"group {subject}"
    return who


def _bounded_row(row: dict) -> dict:
    """*row* with its free-text fields capped, or *row* itself if already small.

    The sink persists ``Event.raw`` verbatim, so without this the cap would
    bound what is rendered while leaving what is stored unbounded.
    """
    oversized = {
        key: value[:MAX_TEXT_CHARS]
        for key in _BOUNDED_FIELDS
        if isinstance(value := row.get(key), str) and len(value) > MAX_TEXT_CHARS
    }
    return {**row, **oversized} if oversized else row


def build_event(source_name: str, row: dict) -> Event:
    """Convert one pushed message row to a backend-neutral Event.

    ``reply_ref`` and ``context_ref`` are both None: writing rows into
    ``msgstore.db`` sends nothing, so there is no reply route, and the host
    cannot fetch surrounding chat lines without a round trip to the phone.
    Neither may be faked — ``routing_summary`` surfaces the keys of both to the
    agent as *trusted* text, outside the untrusted envelope.
    """
    return Event(
        source=source_name,
        sender=format_sender(row),
        text=message_text(row),
        raw=_bounded_row(row),
        dedup_key=f"{row.get('chat_jid')}:{row.get('key_id')}",
        sender_id=resolve_sender_id(row),
    )


def should_ingest(row: dict) -> bool:
    """True if the row is a genuine inbound message we know how to render."""
    if _message_type(row) not in ACCEPTED_TYPES:
        return False
    # Outgoing messages are the user's own words echoed back; they carry no
    # sender to gate on and nothing to act upon.
    if row.get("from_me"):
        return False
    return bool(_text(row.get("key_id"))) and bool(_text(row.get("chat_jid")))


class WhatsAppAdapter:
    """EventSourceAdapter fed by the companion's signed uploads."""

    def __init__(self, source: EventSourceConfig) -> None:
        self.name = source.name
        self._emit: EmitFn | None = None

    async def start(self, emit: EmitFn) -> None:
        self._emit = emit

    async def stop(self) -> None:
        self._emit = None

    async def ingest(self, rows: list[dict[str, Any]]) -> int | None:
        """Emit *rows* in id order; return the highest id the phone may retire.

        Every row must carry an integer ``id``; the upload endpoint enforces
        that before calling.

        Rows the allowlist rejects still advance the watermark — refusing to
        acknowledge one would stall the phone on it permanently.  The guard
        around ``emit`` is defensive only: :meth:`EventSink.emit` handles its
        own failures and does not raise, so a batch drains in full in practice.
        """
        emit = self._emit
        if emit is None:
            raise RuntimeError("WhatsApp adapter is not started")
        cursor: int | None = None
        for row in sorted(rows, key=lambda r: int(r["id"])):
            row_id = int(row["id"])
            if should_ingest(row):
                try:
                    await emit(build_event(self.name, row))
                except Exception:
                    logger.exception(
                        "events[%s]: failed to emit message %d; batch stops here",
                        self.name,
                        row_id,
                    )
                    return cursor
            cursor = row_id
        return cursor
