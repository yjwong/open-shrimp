"""Adapter protocol for inbound event sources."""

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from open_shrimp.events.types import Event

EmitFn = Callable[[Event], Awaitable[None]]


class EventSourceAdapter(Protocol):
    """An outbound connection to an event platform.

    Adapters own their connection lifecycle and reconnect with exponential
    backoff — log each failure, never crash the bot. ``emit`` is the sink's
    entry point; call it once per received event.
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
