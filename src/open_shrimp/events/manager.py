"""Start and stop inbound event source adapters with the bot lifecycle."""

import logging

import aiosqlite
from telegram import Bot

from open_shrimp.config import EventsConfig, EventSourceConfig
from open_shrimp.events.base import EventSourceAdapter
from open_shrimp.events.sink import EventSink

logger = logging.getLogger(__name__)


def _build_adapter(source: EventSourceConfig) -> EventSourceAdapter:
    if source.type == "telegram":
        from open_shrimp.events.telegram_intake import TelegramIntakeAdapter

        return TelegramIntakeAdapter(source)
    if source.type == "lark":
        from open_shrimp.events.lark import LarkAdapter

        return LarkAdapter(source)
    raise ValueError(f"Unknown event source type: {source.type!r}")


class EventManager:
    """Owns the sink and the configured adapters."""

    def __init__(
        self, config: EventsConfig, bot: Bot, db: aiosqlite.Connection,
    ) -> None:
        self._sink = EventSink(
            bot,
            db,
            config.chat_id,
            pickup_sources=frozenset(s.name for s in config.sources if s.pickup),
        )
        self._sources = config.sources
        self._adapters: list[EventSourceAdapter] = []

    async def start(self) -> None:
        # A source that fails to start must not take down the others.
        for source in self._sources:
            try:
                adapter = _build_adapter(source)
                await adapter.start(self._sink.emit)
            except Exception:
                logger.exception(
                    "Failed to start event source %r", source.name,
                )
                continue
            self._adapters.append(adapter)
            logger.info(
                "Started event source %r (type %s)", source.name, source.type,
            )

    async def stop(self) -> None:
        for adapter in reversed(self._adapters):
            try:
                await adapter.stop()
            except Exception:
                logger.warning(
                    "Error stopping event source %r",
                    adapter.name, exc_info=True,
                )
        self._adapters.clear()
