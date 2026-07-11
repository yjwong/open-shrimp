"""Adapter protocol for inbound event sources."""

from collections.abc import Awaitable, Callable
from typing import Protocol

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
