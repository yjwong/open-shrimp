"""Adapter protocol for inbound event sources."""

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from open_shrimp.events.types import Event

EmitFn = Callable[[Event], Awaitable[None]]


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
    """

    async def ingest(self, rows: list[dict]) -> int | None: ...


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
