"""Send, edit and stream rich messages.

``sendRichMessage``, ``sendRichMessageDraft`` and ``editMessageText``'s
``rich_message`` field are not in python-telegram-bot, so every call here goes
through ``bot.do_api_request``.  PTB's request layer JSON-encodes nested dicts
and ``TelegramObject`` values in ``api_kwargs``, so ``rich_message`` and
``reply_markup`` ride along unchanged.

Interactive controls stay on ``reply_markup``.  A ``<tg-button>`` in the body
makes web.telegram.org withhold the whole message — "This message is not
supported on the web version of Telegram" — and the server returns a normal
``Message``, so there is nothing to catch and no way to detect the client.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from telegram import Bot, InlineKeyboardMarkup, Message


logger = logging.getLogger(__name__)

# Rich bodies of the cards this process sent, keyed by (chat_id, message_id).
# A callback that appends an outcome line to a card — "Approved", "Session
# ended" — needs the Markdown the card was built from, and Telegram hands back
# entities instead: a table or a <details> has no entity form to reconstruct.
#
# Only messages carrying a keyboard are kept.  They are the only ones a
# callback can arrive for, and a streamed answer is both far larger and never
# read back, so remembering everything would spend the budget on the traffic
# that cannot use it.
_MAX_REMEMBERED_BODIES = 512
_bodies: OrderedDict[tuple[int, int], str] = OrderedDict()


def _remember(chat_id: int, message_id: int, text: str) -> None:
    key = (chat_id, message_id)
    _bodies.pop(key, None)
    _bodies[key] = text
    while len(_bodies) > _MAX_REMEMBERED_BODIES:
        _bodies.popitem(last=False)


def body_of(message: Message) -> str:
    """The Markdown *message* was sent with, or its flattened text."""
    remembered = _bodies.get((message.chat_id, message.message_id))
    if remembered is not None:
        return remembered
    return message.text or ""


def _body(text: str) -> dict[str, Any]:
    """Build an ``InputRichMessage`` around a Markdown body."""
    return {"markdown": text}


def _thread_kwargs(thread_id: int | None) -> dict[str, Any]:
    return {"message_thread_id": thread_id} if thread_id is not None else {}


async def send_rich(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool = False,
    reply_to_message_id: int | None = None,
    **extra: Any,
) -> Message:
    """Send *text* (already rich Markdown) as a rich message."""
    api_kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _body(text),
        **_thread_kwargs(thread_id),
        **extra,
    }
    if reply_markup is not None:
        api_kwargs["reply_markup"] = reply_markup
    if disable_notification:
        api_kwargs["disable_notification"] = True
    if reply_to_message_id is not None:
        api_kwargs["reply_parameters"] = {"message_id": reply_to_message_id}

    result = await bot.do_api_request(
        "sendRichMessage", api_kwargs=api_kwargs, return_type=Message,
    )
    message_id = getattr(result, "message_id", None)
    if message_id is not None and reply_markup is not None:
        _remember(chat_id, message_id, text)
    return result


async def reply_rich(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool = False,
    quote: bool = False,
    **extra: Any,
) -> Message:
    """Send a rich message into the topic *message* arrived in.

    Mirrors ``Message.reply_text``: the thread id only carries over for a
    forum topic, where it identifies the topic rather than a reply chain.
    """
    thread_id = message.message_thread_id if message.is_topic_message else None
    return await send_rich(
        message.get_bot(),
        message.chat_id,
        text,
        thread_id=thread_id,
        reply_markup=reply_markup,
        disable_notification=disable_notification,
        reply_to_message_id=message.message_id if quote else None,
        **extra,
    )


async def edit_rich(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Rewrite a sent rich message in place.

    Flipping an open ``<details>`` to a collapsed one is an edit like any
    other, which is what lets a Bash card open when the command starts and
    fold shut when it finishes.
    """
    api_kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": _body(text),
    }
    if reply_markup is not None:
        api_kwargs["reply_markup"] = reply_markup
    await bot.do_api_request("editMessageText", api_kwargs=api_kwargs)
    # An edit that takes the keyboard away still updates a tracked card: the
    # decision may be recorded in two steps.
    if reply_markup is not None or (chat_id, message_id) in _bodies:
        _remember(chat_id, message_id, text)


async def edit_message_rich(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """``edit_rich`` addressed by the ``Message`` a callback handed you."""
    await edit_rich(
        message.get_bot(),
        message.chat_id,
        message.message_id,
        text,
        reply_markup=reply_markup,
    )


async def send_rich_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str,
    *,
    thread_id: int | None = None,
) -> None:
    """Animate a partial answer while the turn runs.

    The draft lives about 30 seconds and is never persisted, so the turn still
    ends with a real ``sendRichMessage``.  ``chat_id`` is Integer-only, so a
    group chat gets ``draft_peer_invalid`` back and has to fall back to editing
    a real message.

    ``can_stop`` stays off.  It would render a second stop control feeding
    ``stopped_message_generation`` into the same cancellation state ``/stop``
    already drives, and two producers for one piece of state is how a turn
    ends up half-cancelled.
    """
    await bot.do_api_request(
        "sendRichMessageDraft",
        api_kwargs={
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": _body(text),
            **_thread_kwargs(thread_id),
        },
    )
