"""LinkedIn adapter: one conversation, handed over from the phone.

The host holds no LinkedIn data and never talks to LinkedIn.  A bubble
floating over the LinkedIn app captures the thread on screen when the user
taps it, and the companion POSTs that capture to ``/api/linkedin/handovers``;
this adapter is the passive receiving end.  ``start`` only records ``emit`` —
nothing polls and nothing connects, and the endpoint finds the adapter through
``EventManager.get_adapter_of_type`` rather than a registry of its own.

There is no feed half.  ``SupportsIngest`` would need a watermark over a store
that holds one message for most conversations until the user opens them, so
the only thing delivered here is a conversation a person pointed at.

The capture arrives at one of two fidelities and the transcript says which.
With the on-device store read, every message carries an id and every sender a
profile URL; without it the capture is the viewport, and the header line says
so rather than letting a partial thread read as a whole one.

Two human gates stand between a tap and a turn: the bubble on the phone, and
Pick up in Telegram.  The device signature is authority to deliver the
conversation, not to act on it, and nothing in the payload can change that.
"""

import logging
from dataclasses import dataclass
from typing import Any

from open_shrimp.config import EventSourceConfig
from open_shrimp.events.base import DeliveryOutcome, EmitFn
from open_shrimp.events.format import DATE_CHARS, plural, stamp_millis
from open_shrimp.events.types import Event

logger = logging.getLogger(__name__)

# A guard against an oversized blob, not a display policy — the sink owns
# chunking to Telegram's limit.  Applied to what is stored as well as to what
# is rendered, so the two cannot diverge.
MAX_TEXT_CHARS = 16_000
# Names, headlines and URLs go on a single card line each, and every one of
# them is written by the person messaging you.
MAX_FIELD_CHARS = 300

# The one category worth a line of its own: an InMail is someone who is not a
# connection paying to reach the user, which is most of what gets handed over.
_INMAIL = "INMAIL"


def _field(value: object) -> str | None:
    """A short display field — a name, a headline, a URL — or None."""
    if not isinstance(value, str):
        return None
    return value.strip()[:MAX_FIELD_CHARS] or None


def _body(value: object) -> str | None:
    """A message body, capped, or None if it carries nothing to read."""
    if not isinstance(value, str):
        return None
    return value.strip()[:MAX_TEXT_CHARS] or None


def _when(message: dict) -> str | None:
    """When a message was sent, at whatever fidelity the capture had.

    ``timestamp`` is epoch milliseconds off ``MessagesData.deliveredAt`` and is
    the only form that can be ordered or ranged.  ``time_text`` is what the
    screen showed, which is a localised clock time under a separate date
    header, and is carried through verbatim rather than guessed into an
    absolute instant.
    """
    return stamp_millis(message.get("timestamp")) or _field(message.get("time_text"))


def conversation_label(conversation: dict) -> str:
    """The thread's display string.  Display only, untrusted.

    ``messaging_toolbar_title`` for a screen capture, the store's own title
    otherwise.  It names the counterpart in a one-to-one thread, which is what
    the inbox card header and the spawned topic read.
    """
    return _field(conversation.get("title")) or "unknown"


def _author(message: dict) -> str:
    """Who a transcript line is attributed to.

    Outbound messages read ``me``: a transcript with one side stripped out
    cannot be read, so the capture keeps them.  A message whose sender was
    evicted from ``ParticipantsData`` before it was read is left unattributed
    rather than credited to the counterpart.
    """
    if message.get("from_me"):
        return "me"
    return _field(message.get("author")) or "unknown"


def linked_profiles(
    participants: list[dict], messages: list[dict], store_read: bool
) -> list[dict]:
    """The participants who sent one of the captured messages.

    Those are the people the agent has something to reason about, and a
    fifteen-name card with a profile URL each is unreadable.  The rest stay in
    ``raw`` for an agent that asks.

    Matched on ``sender_urn``, which both sides carry because both come out of
    the store.  A capture read from the screen alone has neither, so the match
    would drop every participant it was given, and what is left is whoever the
    screen named: the counterpart of a one-to-one thread and their headline.

    Which of the two it is comes from *store_read*, the same flag the header
    and the summary say it with, rather than from whether any urn turned up.
    Those two answers agree until a schema rename empties ``senderUrn`` on the
    store path, and then the guess reads a high-fidelity capture as a screen
    one and puts every participant it holds on the card — up to
    ``MAX_PARTICIPANTS`` of them, with a profile URL each.
    """
    if not store_read:
        return participants
    senders = {
        urn
        for message in messages
        if not message.get("from_me") and (urn := _field(message.get("sender_urn")))
    }
    return [
        participant
        for participant in participants
        if _field(participant.get("entity_urn")) in senders
    ]


def _profile_line(participant: dict) -> str | None:
    """One participant as ``name (pronouns), headline — url``, or None."""
    name = _field(participant.get("name"))
    pronouns = _field(participant.get("pronouns"))
    headline = _field(participant.get("headline"))
    url = _field(participant.get("profile_url"))
    if not (name or headline or url):
        return None
    who = f"{name} ({pronouns})" if name and pronouns else (name or "unknown")
    line = f"- {who}"
    if headline:
        line += f", {headline}"
    if url:
        line += f" — {url}"
    return line


@dataclass(frozen=True)
class Capture:
    """A handed-over conversation reduced to what a rendering reads.

    The transcript and the inbox card answer different questions about the
    same messages, so both used to walk the message list themselves and
    re-derive the same three facts from it.  Reading the payload once here
    leaves each renderer with only its own formatting to do.
    """

    title: str
    lines: list[str]  # one per message that carried something to read
    stamps: list[str]  # the times those lines carried, in the same order
    profiles: list[str]  # a line per participant who wrote one of the messages
    inmail: bool
    truncated: bool
    store_read: bool


def read_capture(
    conversation: dict,
    participants: list[dict],
    messages: list[dict],
    truncated: bool,
    store_read: bool,
) -> Capture:
    """Read a handover payload into the lines and facts a rendering needs.

    Messages arrive oldest first and stay in the order they were captured: a
    screen capture has no id to sort on, and re-sorting on the optional
    timestamp would put a store-backed thread and a tree-only one in different
    orders.  A message with no body carries nothing to read and is dropped.
    """
    lines: list[str] = []
    stamps: list[str] = []
    for message in messages:
        body = _body(message.get("text"))
        if body is None:
            continue
        when = _when(message)
        author = _author(message)
        lines.append(f"[{when}] {author}: {body}" if when else f"{author}: {body}")
        if when:
            stamps.append(when)
    return Capture(
        title=conversation_label(conversation),
        lines=lines,
        stamps=stamps,
        profiles=[
            line
            for participant in linked_profiles(participants, messages, store_read)
            if (line := _profile_line(participant))
        ],
        inmail=_field(conversation.get("category")) == _INMAIL,
        truncated=truncated,
        store_read=store_read,
    )


def _header_lines(capture: Capture) -> list[str]:
    """What this is, how much of it, how bounded, and at what fidelity.

    Truncation is reported rather than counted: the boundary tells the agent
    the one thing it needs, that this is a window onto the thread.

    Being the first line of ``Event.text`` also gives the spawned topic a
    usable name, which the oldest message's opening words would not.
    """
    first = f"LinkedIn conversation with {capture.title} — {plural(len(capture.lines))}"
    if capture.stamps:
        first += f", {capture.stamps[0]} to {capture.stamps[-1]}"
    lines = [first + "."]

    if capture.inmail:
        lines.append("Delivered as an InMail, so the sender is not a connection.")
    if capture.truncated:
        lines.append(
            f"Older messages exist; nothing before {capture.stamps[0]} is included."
            if capture.stamps
            else "Older messages exist and are not included."
        )
    if not capture.store_read:
        lines.append(
            "Captured from the screen alone: no profile links, no message ids, "
            "and nothing that was above the viewport."
        )
    return lines


def render_transcript(capture: Capture) -> str:
    """The conversation as readable text, oldest first, under its header.

    This is what the agent reads through ``read_inbound_event``, which wraps
    it in the untrusted envelope.  Every word of it was written by whoever is
    messaging the user, headlines and profile URLs included.
    """
    blocks = ["\n".join(_header_lines(capture))]
    if capture.profiles:
        blocks.append("\n".join(["Profiles:", *capture.profiles]))
    blocks.append("\n".join(capture.lines))
    return "\n\n".join(blocks)


def render_summary(capture: Capture) -> str:
    """The one-line inbox card standing in for the transcript.

    Without it the sink would chunk a long thread into a dozen Telegram
    messages in the inbox topic.
    """
    line = f"Handed over — {plural(len(capture.lines))}"
    if capture.stamps:
        first = capture.stamps[0][:DATE_CHARS]
        last = capture.stamps[-1][:DATE_CHARS]
        line += f", {first}" + (f" → {last}" if last != first else "")
    if capture.inmail:
        line += ", InMail"
    if capture.truncated:
        line += ", older messages not included"
    if not capture.store_read:
        line += ", screen capture only"
    return line + "."


def handover_dedup_key(conversation: dict, messages: list[dict]) -> str | None:
    """A key that a re-tap of an unchanged thread repeats, or None.

    The conversation urn plus the newest message's ``originToken`` and the
    number of messages captured: two taps on a thread nothing has happened to
    produce the same key, so the second is dropped instead of posting a second
    identical card.  Neither a reply arriving nor the user scrolling further
    back is a repeat, and both change the key.

    None for a capture without the store, which carries neither field —
    idempotency is one of the four things the store buys.  The sink's dedup is
    an in-memory LRU that a restart wipes, so this bounds double-taps rather
    than guaranteeing once-only delivery.
    """
    urn = _field(conversation.get("entity_urn"))
    tokens = [
        token
        for message in messages
        if (token := _field(message.get("origin_token")))
    ]
    if urn is None or not tokens:
        return None
    return f"{urn}:{tokens[-1]}:{len(messages)}"


def _bounded(message: dict) -> dict:
    """*message* with its body capped, or *message* itself if already small.

    The sink persists ``Event.raw`` verbatim, so without this the cap would
    bound what is rendered while leaving what is stored unbounded.
    """
    body = message.get("text")
    if isinstance(body, str) and len(body) > MAX_TEXT_CHARS:
        return {**message, "text": body[:MAX_TEXT_CHARS]}
    return message


def build_handover(source_name: str, payload: dict) -> Event:
    """One conversation as one event: the unit the user pointed at.

    ``sender_id`` is None on purpose, and it is load-bearing.  Trust here comes
    from the device signature rather than from anything in the payload, and
    leaving the field empty is what makes it impossible for a ``/context:``
    string inside a recruiter's message to route the event: the sink only
    reads a directive after matching ``sender_id`` against the source's
    trusted senders, and there is nothing here to match.  A handover reaches a
    context the same way every other event does — a person taps Pick up and
    chooses one.
    """
    conversation = payload.get("conversation") or {}
    participants = payload.get("participants") or []
    messages = payload.get("messages") or []
    truncated = bool(payload.get("truncated"))
    store_read = bool(payload.get("store_read"))
    capture = read_capture(
        conversation, participants, messages, truncated, store_read
    )
    return Event(
        source=source_name,
        sender=capture.title,
        text=render_transcript(capture),
        summary=render_summary(capture),
        raw={
            "conversation": conversation,
            "participants": participants,
            "messages": [_bounded(message) for message in messages],
            "truncated": truncated,
            "store_read": store_read,
        },
        dedup_key=handover_dedup_key(conversation, messages),
        sender_id=None,
    )


class LinkedInAdapter:
    """EventSourceAdapter fed by the companion's signed handovers."""

    def __init__(self, source: EventSourceConfig) -> None:
        self.name = source.name
        self._emit: EmitFn | None = None

    async def start(self, emit: EmitFn) -> None:
        self._emit = emit

    async def stop(self) -> None:
        self._emit = None

    async def handover(self, payload: dict[str, Any]) -> DeliveryOutcome:
        """Deliver one conversation as a single event; report where it landed.

        The signature on the request is authority to *deliver* the thread, not
        to act on it: the event lands in the inbox as an inert card like any
        other, and a person picks it up into a context of their choosing.
        Nothing in *payload* can change that, because there is nothing in the
        payload the sink consults when deciding.
        """
        emit = self._emit
        if emit is None:
            raise RuntimeError("LinkedIn adapter is not started")
        return await emit(build_handover(self.name, payload))
