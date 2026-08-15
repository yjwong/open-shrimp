"""WhatsApp adapter: message rows pushed from the Android companion.

The host holds no WhatsApp data and never talks to WhatsApp.  The companion
app reads ``msgstore.db`` on the phone behind root and POSTs to the upload
endpoints; this adapter is the passive receiving end.  ``start`` only records
``emit`` — nothing polls and nothing connects, and the endpoints find the
adapter through ``EventManager.get_adapter_of_type`` rather than a registry of
its own.

Two paths arrive here and they answer different questions.  ``ingest`` is the
feed: rows from the chats selected in the companion UI, one event each, inert
in the inbox until someone picks them up.  ``handover`` is one chat the user
pointed at, rendered as a single transcript and taken straight to a working
topic.  Neither knows about the other — a handover moves no watermark and
retires no id, so a chat that is both watched and handed over keeps
delivering through the feed unchanged.

Restart-durable dedup for the feed lives on the phone, which advances its
``message._id`` watermark only after the host accepts a batch (the sink's own
dedup is an in-memory LRU that a restart wipes).  That watermark is one-way,
so ``ingest`` returns the highest id it is *done with* — which is neither the
highest it emitted nor the highest it saw.  A row the allowlist declines is
done with and must be acknowledged or the phone re-sends it forever; a row
that failed to reach a topic is not, and acknowledging it loses the message.
"""

import logging
from datetime import datetime
from typing import Any

from open_shrimp.config import EventSourceConfig
from open_shrimp.events.base import Delivery, DeliveryOutcome, EmitFn
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


def _jid_server(jid: str | None) -> str | None:
    """The server half of a JID: ``g.us``, ``s.whatsapp.net``, ``lid``."""
    if jid is None or "@" not in jid:
        return None
    return jid.rsplit("@", 1)[-1]


def chat_server(row: dict) -> str | None:
    """The JID server of the row's chat.

    Derived from ``chat_jid`` rather than carried as its own field, so the two
    cannot disagree — a row claiming a direct server for a group JID would
    otherwise hand a group id back as a trusted sender.
    """
    return _jid_server(_text(row.get("chat_jid")))


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


# ── Handover: one chat, rendered whole ────────────────────────────────────
#
# The phone sends rows and nothing else.  The placeholder table, the type
# allowlist and the text cap all live above, and duplicating them on the
# phone would let the two drift; so the transcript is drawn here and the
# companion stays a transport.

_STAMP_FORMAT = "%Y-%m-%d %H:%M"
_DATE_CHARS = len("2026-08-14")


def is_group_chat(chat: dict) -> bool:
    return _jid_server(_text(chat.get("jid"))) == "g.us"


def chat_label(chat: dict) -> str:
    """The chat's display string, group-qualified.  Display only, untrusted.

    Names come from the phone, which merges ``wa.db`` contact rows into the
    picker — so a one-to-one chat reads as a person rather than as the bare
    JID a feed event falls back to.  They gate nothing.
    """
    jid = _text(chat.get("jid"))
    name = _text(chat.get("subject")) or _text(chat.get("name")) or jid or "unknown"
    return f"group {name}" if is_group_chat(chat) else name


def _stamp(value: object) -> str | None:
    """A row's timestamp as local wall-clock text, or None if it has none."""
    try:
        millis = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000).strftime(_STAMP_FORMAT)
    except (OverflowError, OSError, ValueError):
        return None


def _author(row: dict, chat: dict) -> str:
    """Who a transcript line is attributed to.

    Outbound rows read ``me``: a transcript with one side stripped out cannot
    be read, which is why the handover query keeps them.  A one-to-one row
    carries no per-row sender — the counterparty is the chat — so it falls
    back to the chat's own label, while a group row that somehow lacks one is
    left unattributed rather than credited to the group.
    """
    if row.get("from_me"):
        return "me"
    who = _text(row.get("sender_name")) or _text(row.get("sender_jid"))
    if who:
        return who
    return "unknown" if is_group_chat(chat) else chat_label(chat)


def _drawn_rows(chat: dict, rows: list[dict]) -> list[tuple[str | None, str]]:
    """``(timestamp, line)`` for every row the host knows how to draw.

    Rows of a type outside ``ACCEPTED_TYPES`` are dropped for the same reason
    the feed drops them — the allowlist fails closed on the dozen rare types
    that were never identified — and a row whose body renders empty carries
    nothing to read.
    """
    drawn: list[tuple[str | None, str]] = []
    for row in rows:
        if _message_type(row) not in ACCEPTED_TYPES:
            continue
        body = message_text(row)
        if body is None:
            continue
        stamp = _stamp(row.get("timestamp"))
        author = _author(row, chat)
        drawn.append(
            (stamp, f"[{stamp}] {author}: {body}" if stamp else f"{author}: {body}")
        )
    return drawn


def _plural(count: int) -> str:
    return f"{count} message" if count == 1 else f"{count} messages"


def _header_line(
    chat: dict, drawn: list[tuple[str | None, str]], truncated: bool
) -> str:
    """The transcript's first line: what this is, how much of it, and how bounded.

    Truncation is reported rather than counted.  An exact "N older messages
    omitted" needs a COUNT(*) over the whole chat, which costs seconds on a
    real store; the boundary tells the agent the one thing it needs — that
    this is a window and not the chat.

    Being the first line of ``Event.text`` also gives the spawned topic a
    usable name, which the oldest message's opening words would not.
    """
    jid = _text(chat.get("jid")) or "unknown"
    stamps = [stamp for stamp, _ in drawn if stamp]
    line = f"WhatsApp chat with {chat_label(chat)} ({jid}) — {_plural(len(drawn))}"
    if stamps:
        line += f", {stamps[0]} to {stamps[-1]}"
    line += "."
    if truncated:
        line += (
            f" Older messages exist; nothing before {stamps[0]} is included."
            if stamps
            else " Older messages exist and are not included."
        )
    return line


def render_transcript(chat: dict, rows: list[dict], truncated: bool) -> str:
    """*rows* as a readable conversation, oldest first, under a header line.

    This is what the agent reads through ``read_inbound_event``; *rows* must
    already be in oldest-first order.
    """
    drawn = _drawn_rows(chat, rows)
    return "\n".join(
        [_header_line(chat, drawn, truncated), "", *(line for _, line in drawn)]
    )


def render_summary(chat: dict, rows: list[dict], truncated: bool) -> str:
    """The one-line inbox card standing in for the transcript.

    Without it the sink would chunk a hundred-thousand-character transcript
    into a couple of dozen Telegram messages in the inbox topic.
    """
    drawn = _drawn_rows(chat, rows)
    stamps = [stamp for stamp, _ in drawn if stamp]
    line = f"Handed over — {_plural(len(drawn))}"
    if stamps:
        first, last = stamps[0][:_DATE_CHARS], stamps[-1][:_DATE_CHARS]
        line += f", {first}" + (f" → {last}" if last != first else "")
    if truncated:
        line += ", older messages not included"
    return line + "."


def build_handover(source_name: str, payload: dict) -> Event:
    """One chat as one event: the unit the user pointed at is the chat.

    ``dedup_key`` is None on purpose.  Replay is already impossible a layer
    down — every signed request carries a nonce the host rejects on reuse —
    and a content-derived key would additionally block handing the same chat
    over twice, which is an ordinary thing to want.

    ``sender_id`` is None on purpose too, and it is load-bearing.  Trust here
    comes from the device signature rather than from anything in the payload,
    and leaving the field empty is also what makes it impossible for a
    ``/context:`` string inside the transcript to route the event: the sink
    only reads a directive after matching ``sender_id`` against the source's
    trusted senders, and there is nothing here to match.  A handover reaches
    a context the same way every other event does — a person taps Pick up and
    chooses one.

    The caller must have validated that every row carries an integer ``id``.
    """
    chat = payload.get("chat") or {}
    rows = sorted(payload.get("messages") or [], key=lambda row: int(row["id"]))
    truncated = bool(payload.get("truncated"))
    return Event(
        source=source_name,
        sender=chat_label(chat),
        text=render_transcript(chat, rows, truncated),
        summary=render_summary(chat, rows, truncated),
        raw={
            "chat": chat,
            "messages": [_bounded_row(row) for row in rows],
            "truncated": truncated,
        },
        dedup_key=None,
        sender_id=None,
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

        The returned id is the phone's licence to forget, so it may only cover
        rows that are genuinely finished with.  Three cases, and they are not
        the same:

        * a row the allowlist rejects never reaches ``emit`` and is done —
          withholding it would stall the phone on that row permanently;
        * a duplicate is already in a topic and is likewise done, which is
          what lets a re-sent batch drain instead of deadlocking;
        * a row that failed to deliver reached no topic, has no discoverable
          event id and will never be offered again, so the batch stops at the
          last id before it and the phone re-sends from there.
        """
        emit = self._emit
        if emit is None:
            raise RuntimeError("WhatsApp adapter is not started")
        cursor: int | None = None
        for row in sorted(rows, key=lambda r: int(r["id"])):
            row_id = int(row["id"])
            if should_ingest(row):
                try:
                    outcome = await emit(build_event(self.name, row))
                except Exception:
                    logger.exception(
                        "events[%s]: failed to emit message %d; batch stops here",
                        self.name,
                        row_id,
                    )
                    return cursor
                if outcome.status is Delivery.FAILED:
                    logger.warning(
                        "events[%s]: message %d was not delivered; batch stops here",
                        self.name,
                        row_id,
                    )
                    return cursor
            cursor = row_id
        return cursor

    async def handover(self, payload: dict[str, Any]) -> DeliveryOutcome:
        """Deliver one chat as a single event; report where it landed.

        The signature on the request is authority to *deliver* the chat, not
        to act on it: the event lands in the inbox as an inert card like any
        other, and a person picks it up into a context of their choosing.
        Nothing in *payload* can change that, because there is nothing in the
        payload the sink consults when deciding.

        Nothing here touches a cursor either.  A handover reads from the head
        of a chat and retires no id, so the feed is unaffected by it.
        """
        emit = self._emit
        if emit is None:
            raise RuntimeError("WhatsApp adapter is not started")
        return await emit(build_handover(self.name, payload))
