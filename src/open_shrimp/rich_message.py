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

import datetime as dtm
import logging
import time
import warnings
from collections import OrderedDict, defaultdict, deque
from typing import Any

from telegram import Bot, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, RetryAfter
from telegram.warnings import PTBUserWarning

logger = logging.getLogger(__name__)

# PTB nags whenever ``do_api_request`` names an endpoint it has a typed method
# for, and ``editMessageText`` is one.  The typed method cannot carry
# ``rich_message`` — it requires ``text``, which is the field being replaced —
# so the advice does not apply and the nag would otherwise print on every card
# that collapses.  Matched on the endpoint so the same warning about any other
# method still gets through.
warnings.filterwarnings(
    "ignore",
    message=r"Please use 'Bot\.editMessageText' instead",
    category=PTBUserWarning,
)

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


async def edit_rich_unchanged_ok(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Rewrite a card, treating "already says that" as success.

    Telegram rejects an edit whose body and keyboard both match what is
    already there, which a refresh button hits every time nothing has moved.
    Returns False only when the message could not be edited at all, so a
    caller that needs to fall back to sending a new one can tell the two
    apart.
    """
    try:
        await edit_rich(
            bot, chat_id, message_id, text, reply_markup=reply_markup,
        )
        return True
    except BadRequest as exc:
        return "message is not modified" in str(exc).lower()
    except Exception:
        return False


# Telegram's ceilings on live drafts, as (window seconds, calls allowed):
# 20 calls in 5 seconds and 40 in 30.  Both are enforced, because staying
# under the 5-second tier alone permits 4 calls/s, which exhausts the
# 30-second tier after 10 seconds of streaming.
_DRAFT_RATE_LIMITS: tuple[tuple[float, int], ...] = ((5.0, 20), (30.0, 40))
_DRAFT_WIDEST_WINDOW = max(width for width, _ in _DRAFT_RATE_LIMITS)

#: How long a client keeps a live draft it has stopped receiving updates for,
#: from ``message_typing_draft_ttl`` in the app config the server publishes.
#: A lapse is not a pause: the client deletes the draft outright, and the next
#: update builds a fresh one at the bottom of the list.
DRAFT_TTL_SECONDS = 10.0

#: Never send two drafts for one peer closer together than this.  The binding
#: tier is 40 calls per 30 seconds, one every 0.75s, so spending the allowance
#: as fast as the tiers permit — 20 calls, then 20 more five seconds later —
#: buys nothing and blocks the peer until 30 seconds after the first of them,
#: a 25-second silence that no keepalive inside the TTL can cover.  Spacing
#: every send holds the longest gap to one tick and leaves both tiers slack.
_DRAFT_MIN_INTERVAL = 0.8


class _DraftBudget:
    """What one peer may still spend on live drafts.

    Telegram counts the peer, so two forum topics of one chat streaming at
    once draw on the same instance.
    """

    __slots__ = ("_calls", "_flood_until")

    def __init__(self) -> None:
        #: Send times, newest last.
        self._calls: deque[float] = deque()
        #: Monotonic deadline a FLOOD_WAIT put this peer behind.
        self._flood_until = 0.0

    def spent(self, now: float) -> bool:
        """Whether another draft for this peer is refused or simply too early."""
        if now < self._flood_until:
            return True
        if self._calls and now - self._calls[-1] < _DRAFT_MIN_INTERVAL:
            return True
        # The tiers stay enforced behind the pacing above, which alone cannot
        # bring a peer back under a limit a FLOOD_WAIT or a clock jump left it
        # over.  Sends are appended in order, so the allowance-th entry from
        # the end is the oldest one a tier still counts.
        return any(
            len(self._calls) >= allowance
            and now - self._calls[-allowance] < width
            for width, allowance in _DRAFT_RATE_LIMITS
        )

    def charge(self, now: float) -> None:
        """Book a send, dropping the entries too old to constrain anything."""
        while self._calls and now - self._calls[0] >= _DRAFT_WIDEST_WINDOW:
            self._calls.popleft()
        self._calls.append(now)

    def flooded(self, seconds: float) -> None:
        """Hold the peer for the cool-down Telegram named."""
        self._flood_until = time.monotonic() + seconds


_draft_budgets: dict[int, _DraftBudget] = defaultdict(_DraftBudget)


def draft_budget_spent(chat_id: int) -> bool:
    """Whether a live draft for ``chat_id`` would be refused right now.

    A caller with expensive text to render can ask first and keep the work
    for its next attempt.  ``send_rich_draft`` checks again regardless, so
    skipping this changes nothing but the wasted render.
    """
    return _draft_budgets[chat_id].spent(time.monotonic())


async def send_rich_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str,
    *,
    thread_id: int | None = None,
) -> bool:
    """Animate a partial answer while the turn runs.

    The draft expires after ``DRAFT_TTL_SECONDS`` and is never persisted, so
    the turn still ends with a real ``sendRichMessage``.  ``chat_id`` is
    Integer-only, so a group chat gets ``draft_peer_invalid`` back and has to
    fall back to editing a real message.

    Returns whether the draft reached Telegram: False means the peer's rate
    budget is spent or a FLOOD_WAIT is still running, and the caller should
    keep its text pending and offer it again.  Skipping rather than sleeping
    keeps the next attempt carrying the newest text instead of a snapshot
    taken before the wait.

    ``can_stop`` stays off.  It would render a second stop control feeding
    ``stopped_message_generation`` into the same cancellation state ``/stop``
    already drives, and two producers for one piece of state is how a turn
    ends up half-cancelled.
    """
    now = time.monotonic()
    budget = _draft_budgets[chat_id]
    if budget.spent(now):
        return False

    # Charged before the request, so drafts in flight for other topics of the
    # same chat are visible to each other.
    budget.charge(now)
    try:
        await bot.do_api_request(
            "sendRichMessageDraft",
            api_kwargs={
                "chat_id": chat_id,
                "draft_id": draft_id,
                "rich_message": _body(text),
                **_thread_kwargs(thread_id),
            },
        )
    except RetryAfter as exc:
        # The server hands out cool-downs of up to 3 seconds even to callers
        # under the limit, so honour the value it names instead of retrying
        # on the caller's own cadence and extending the wait.  PTB reports it
        # as seconds or as a timedelta, per its PTB_TIMEDELTA setting.
        after = exc.retry_after
        seconds = (
            after.total_seconds() if isinstance(after, dtm.timedelta) else after
        )
        budget.flooded(seconds)
        logger.info(
            "Live draft flood wait of %.1fs for chat %s", seconds, chat_id,
        )
        return False
    return True
