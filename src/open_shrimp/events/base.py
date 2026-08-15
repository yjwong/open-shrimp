"""Adapter protocol for inbound event sources."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol, runtime_checkable

from open_shrimp.events.types import Event


class Delivery(Enum):
    """What became of an emitted event.

    DUPLICATE and FAILED both leave every destination field None, but they are
    not interchangeable: a duplicate is already in a topic and is finished
    with, while a failure never reached one.  A caller holding a source-side
    watermark may retire the first and must not retire the second.
    """

    DELIVERED = auto()
    DUPLICATE = auto()
    FAILED = auto()


@dataclass
class DeliveryOutcome:
    """Where an emitted event landed.

    Every destination field is optional because delivery is best-effort: the
    sink never raises, and an event that was dropped reports Nones rather than
    an exception.  ``status`` is what distinguishes the reasons — read it,
    not the Nones.  Adapters that only push events ignore this; it exists for
    the ones whose caller is a request that must answer with a destination.
    """

    status: Delivery
    event_id: int | None  # persisted inbound_events row
    thread_id: int | None  # the source's inbox topic
    pickup_thread_id: int | None  # the spawned working topic, if any
    deep_link: str | None  # tg:// link to pickup_thread_id


EmitFn = Callable[[Event], Awaitable[DeliveryOutcome]]


class EventSourceAdapter(Protocol):
    """A live inbound event source.

    ``start(emit)`` makes the source live and must return promptly; ``stop()``
    makes it inert. ``emit`` is the sink's entry point; call it once per
    received event.

    An adapter that owns a connection also owns its lifecycle: reconnect with
    exponential backoff, log each failure, never crash the bot. A *passive*
    adapter — one fed by an authenticated push endpoint rather than by a
    connection of its own — has no lifecycle to own and may simply record
    ``emit``.
    """

    name: str

    async def start(self, emit: EmitFn) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class SupportsReply(Protocol):
    """Optional adapter capability: send a reply back to an event's origin.

    ``reply_ref`` is the adapter-specific routing dict the adapter itself
    put on the :class:`Event` at ingest time (e.g. the Lark ``message_id``
    to reply to, in-thread). Raise on failure — the caller surfaces the
    error to the agent as a tool error.
    """

    async def reply(self, reply_ref: dict, text: str) -> None: ...


@runtime_checkable
class SupportsIngest(Protocol):
    """Optional adapter capability: accept rows pushed in from outside.

    For a passive source the host never connects to, an authenticated upload
    endpoint hands rows here instead of the adapter fetching them. Returns the
    highest source-side watermark the pusher may retire — which is not the
    same as the highest row emitted, since a row the adapter declines is still
    finished with — or None when the batch advanced nothing.

    Never return an id whose row did not reach a topic. The pusher's watermark
    is the only restart-durable dedup a passive source has, so a row retired
    without being delivered is lost: there is no back-fill and it is never
    offered again.
    """

    async def ingest(self, rows: list[dict]) -> int | None: ...


@runtime_checkable
class SupportsHandover(Protocol):
    """Optional adapter capability: accept a first-party request to hand a
    conversation to the agent immediately.

    Distinct from :class:`SupportsIngest`: ingest is a feed the source drains
    against a watermark, and the host decides what to do with each row.  A
    handover is a single authenticated request that the conversation be worked
    on now, so it carries no cursor and its delivery outcome is the answer.

    The authentication is the caller's, not the payload's: an adapter marks
    the resulting event ``auto_pickup`` because of how the request arrived,
    and nothing inside *payload* may influence that.
    """

    async def handover(self, payload: dict) -> DeliveryOutcome: ...


@runtime_checkable
class SupportsContext(Protocol):
    """Optional adapter capability: fetch surrounding context for an event.

    ``context_ref`` is the adapter-specific dict the adapter put on the
    :class:`Event` at ingest (e.g. Lark ``chat_id`` + ``thread_id``).
    Returns a plain-text rendering of the extra context (e.g. recent thread
    messages) or None when nothing extra is available. The caller wraps the
    return value in the untrusted envelope, so the text must NOT embed
    instructions. Raise only on hard failure — the caller degrades to the
    base event content.
    """

    async def fetch_context(self, context_ref: dict) -> str | None: ...
