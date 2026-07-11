"""Telegram intake adapter: a second bot token, long-polled as a pure listener.

Builds a second python-telegram-bot ``Application`` with a manual lifecycle
(startup: initialize -> start -> updater.start_polling; shutdown in
reverse). Messages from allowlisted chats are
converted to :class:`~open_shrimp.events.types.Event` and handed to the sink's
``emit``. The intake bot never posts to its source chats on its own; the only
outbound path is :meth:`TelegramIntakeAdapter.reply`, invoked explicitly via
the ``reply_inbound_event`` tool.
"""

import asyncio
import contextlib
import logging
from typing import Any

from telegram import Message, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from open_shrimp.config import EventSourceConfig
from open_shrimp.events.base import EmitFn
from open_shrimp.events.types import Event

logger = logging.getLogger(__name__)

_BACKOFF_INITIAL_S = 5.0
_BACKOFF_CAP_S = 300.0


def media_placeholder(message: Message) -> str | None:
    """Placeholder text for media-only messages, None if unrecognized."""
    if getattr(message, "photo", None):
        return "[photo]"
    if getattr(message, "video", None):
        return "[video]"
    if getattr(message, "video_note", None):
        return "[video note]"
    if getattr(message, "voice", None):
        return "[voice]"
    if getattr(message, "audio", None):
        return "[audio]"
    if getattr(message, "sticker", None):
        emoji = getattr(message.sticker, "emoji", None)
        return f"[sticker {emoji}]" if emoji else "[sticker]"
    # Animation messages also carry a document field; check animation first.
    if getattr(message, "animation", None):
        return "[animation]"
    if getattr(message, "document", None):
        name = getattr(message.document, "file_name", None)
        return f"[document: {name}]" if name else "[document]"
    if getattr(message, "contact", None):
        return "[contact]"
    if getattr(message, "location", None):
        return "[location]"
    if getattr(message, "poll", None):
        question = getattr(message.poll, "question", None)
        return f"[poll: {question}]" if question else "[poll]"
    return None


def format_sender(message: Message) -> str | None:
    """Human-readable sender: full name + @username, group title prefix."""
    who: str | None = None
    user = message.from_user
    if user is not None:
        who = user.full_name
        if user.username:
            who = f"{who} @{user.username}"
    elif getattr(message, "sender_chat", None) is not None:
        who = getattr(message.sender_chat, "title", None)

    chat = message.chat
    if chat.type in ("group", "supergroup") and chat.title:
        prefix = f"group {chat.title}"
        return f"{prefix} / {who}" if who else prefix
    if chat.type == "channel" and chat.title:
        prefix = f"channel {chat.title}"
        return f"{prefix} / {who}" if who else prefix
    return who


def addresses_bot(
    message: Message, bot_username: str | None, bot_id: int | None
) -> bool:
    """True if *message* explicitly addresses the bot.

    Covers an ``@username`` mention, a ``/command@username`` addressed to the
    bot, and a ``text_mention`` of the bot's user id.  Works on both text and
    caption entities.  Entity text is sliced by offset/length so it works
    without PTB helper methods (and thus with test doubles).
    """
    target = f"@{bot_username}".lower() if bot_username else None
    for body, entities in (
        (getattr(message, "text", None), getattr(message, "entities", None)),
        (getattr(message, "caption", None), getattr(message, "caption_entities", None)),
    ):
        for entity in entities or ():
            etype = getattr(entity, "type", None)
            if etype == "text_mention":
                user = getattr(entity, "user", None)
                if user is not None and getattr(user, "id", None) == bot_id:
                    return True
                continue
            if target is None or body is None:
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if offset is None or length is None:
                continue
            token = body[offset : offset + length].lower()
            if etype == "mention" and token == target:
                return True
            if etype == "bot_command" and token.endswith(target):
                return True
    return False


def build_event(source_name: str, message: Message) -> Event:
    """Convert an intake Message to a backend-neutral Event."""
    text = message.text or message.caption or media_placeholder(message)
    return Event(
        source=source_name,
        sender=format_sender(message),
        text=text,
        raw=message.to_dict(),
        dedup_key=f"{message.chat.id}:{message.message_id}",
        reply_ref={"chat_id": message.chat.id, "message_id": message.message_id},
    )


async def handle_intake_update(
    source_name: str,
    allowed_chats: set[int],
    emit: EmitFn,
    update: Update,
    *,
    require_mention: bool = False,
    bot_username: str | None = None,
    bot_id: int | None = None,
) -> None:
    """Core handler logic, separated from PTB wiring for testability."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if chat.id not in allowed_chats:
        logger.info(
            "events[%s]: dropping message %s from disallowed chat %s",
            source_name,
            message.message_id,
            chat.id,
        )
        return
    # A DM to the bot is implicitly addressed to it; only group/channel
    # messages need an explicit mention when require_mention is on.
    if (
        require_mention
        and getattr(chat, "type", None) != "private"
        and not addresses_bot(message, bot_username, bot_id)
    ):
        logger.debug(
            "events[%s]: dropping message %s (bot not addressed)",
            source_name,
            message.message_id,
        )
        return
    await emit(build_event(source_name, message))


class TelegramIntakeAdapter:
    """EventSourceAdapter for a second Telegram bot token (pure listener)."""

    def __init__(self, source: EventSourceConfig) -> None:
        self.name = source.name
        self._token = source.token or ""
        self._allowed_chats = set(source.allowed_chats)
        self._require_mention = source.require_mention
        self._bot_username: str | None = None
        self._bot_id: int | None = None
        self._app: Application | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self, emit: EmitFn) -> None:
        """Launch startup (with retry) in the background; returns immediately."""
        self._stopped = False
        self._startup_task = asyncio.create_task(
            self._start_with_retry(emit), name=f"telegram-intake-start-{self.name}"
        )

    async def reply(self, reply_ref: dict, text: str) -> None:
        """Send *text* as a Telegram reply to the originating message."""
        chat_id = reply_ref.get("chat_id")
        message_id = reply_ref.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            raise ValueError("event carries no Telegram reply routing")
        app = self._app
        if app is None:
            raise RuntimeError("Telegram intake adapter is not started")
        await app.bot.send_message(
            chat_id, text, reply_to_message_id=message_id
        )

    async def stop(self) -> None:
        """Stop polling and tear down; tolerant of partial startup."""
        self._stopped = True
        task = self._startup_task
        self._startup_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._teardown_app()

    async def _start_with_retry(self, emit: EmitFn) -> None:
        delay = _BACKOFF_INITIAL_S
        while not self._stopped:
            try:
                await self._start_once(emit)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "events[%s]: telegram intake startup failed; retrying in %.0fs",
                    self.name,
                    delay,
                )
                await self._teardown_app()
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_CAP_S)
            else:
                logger.info("events[%s]: telegram intake polling started", self.name)
                return

    async def _start_once(self, emit: EmitFn) -> None:
        app = self._build_application()
        self._app = app
        app.add_handler(MessageHandler(filters.ALL, self._make_handler(emit)))
        await app.initialize()
        # initialize() populates bot identity (getMe); needed to match mentions.
        self._bot_username = app.bot.username
        self._bot_id = app.bot.id
        await app.start()
        await app.updater.start_polling()

    def _build_application(self) -> Application:
        return Application.builder().token(self._token).build()

    def _make_handler(self, emit: EmitFn) -> Any:
        async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                await handle_intake_update(
                    self.name,
                    self._allowed_chats,
                    emit,
                    update,
                    require_mention=self._require_mention,
                    bot_username=self._bot_username,
                    bot_id=self._bot_id,
                )
            except Exception:
                logger.exception("events[%s]: failed to process intake update", self.name)

        return on_message

    async def _teardown_app(self) -> None:
        app = self._app
        self._app = None
        if app is None:
            return
        updater = getattr(app, "updater", None)
        if updater is not None:
            try:
                if updater.running:
                    await updater.stop()
            except Exception:
                logger.warning(
                    "events[%s]: intake updater.stop() failed", self.name, exc_info=True
                )
        try:
            if app.running:
                await app.stop()
        except Exception:
            logger.warning("events[%s]: intake app.stop() failed", self.name, exc_info=True)
        try:
            await app.shutdown()
        except Exception:
            logger.warning("events[%s]: intake app.shutdown() failed", self.name, exc_info=True)
