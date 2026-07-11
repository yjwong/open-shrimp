"""Start and stop inbound event source adapters with the bot lifecycle."""

import logging

import aiosqlite
from telegram import Bot

from open_shrimp.config import EventsConfig, EventSourceConfig
from open_shrimp.events.base import EventSourceAdapter
from open_shrimp.events.sink import EventSink

logger = logging.getLogger(__name__)

# The manager currently running with the bot, if any.  Set on start() and
# cleared on stop() so tool handlers (reply_inbound_event) can reach the
# live adapters without threading the manager through the tool wiring.
_active_manager: "EventManager | None" = None


def get_active_manager() -> "EventManager | None":
    return _active_manager


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

    def get_adapter(self, name: str) -> EventSourceAdapter | None:
        """The running adapter for source *name*, or None."""
        for adapter in self._adapters:
            if adapter.name == name:
                return adapter
        return None

    async def start(self) -> None:
        global _active_manager
        _active_manager = self
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
        global _active_manager
        if _active_manager is self:
            _active_manager = None
        for adapter in reversed(self._adapters):
            try:
                await adapter.stop()
            except Exception:
                logger.warning(
                    "Error stopping event source %r",
                    adapter.name, exc_info=True,
                )
        self._adapters.clear()
