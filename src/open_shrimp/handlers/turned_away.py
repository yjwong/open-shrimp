"""What happens when a non-allowlisted user messages the bot.

The bot says nothing back.  A reply naming the sender's own id would be
self-diagnosing for a mistyped allowlist, but it is also an oracle: it
confirms to anyone sweeping the username namespace that a live instance with a
real machine behind it answers here, and it hands over a copy-pasteable id
alongside the exact config key to paste it into — the bot authoring its own
social-engineering script.  The enrollment handshake makes a wrong allowlist
structurally impossible on the wizard path anyway, so what is left to diagnose
is the hand-edited config, and that is diagnosed on the operator's side.

So: a log line always, and an aggregated note to the people who are already
allowed.  Never a word to the sender.

The invariant this file protects, stated once: **the bot speaks to a
non-allowlisted user in exactly one circumstance — an open enrollment window
in the setup wizard, capped at three candidates, six digits and one sentence,
naming no product.**  Every other unauthorized message is silence plus a log
line.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from open_shrimp.config import Config
from open_shrimp.handlers.utils import _is_authorized

logger = logging.getLogger(__name__)

# One note per hour at most.  The point is a signal the operator can act on,
# not a running commentary that trains them to ignore it.
NOTICE_INTERVAL_SECONDS = 3600.0


@dataclass
class _Tally:
    """Attempts seen since the last note went out."""

    attempts: int = 0
    senders: set[int] = field(default_factory=set)
    # None rather than 0.0: on a platform whose monotonic clock counts uptime,
    # zero is a real timestamp and the first note of the boot would be
    # swallowed for an hour.
    last_sent: float | None = None


_tally = _Tally()


def reset_state() -> None:
    """Forget everything seen so far."""
    global _tally
    _tally = _Tally()


def _describe(senders: set[int]) -> str:
    ids = sorted(senders)
    shown = ", ".join(str(i) for i in ids[:5])
    if len(ids) > 5:
        shown += f", and {len(ids) - 5} more"
    return shown


async def note_unauthorized(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log a turned-away sender and, at most hourly, tell the allowed users.

    Registered ahead of every other handler and deliberately does not stop
    propagation: the drop itself still happens where it always did, in each
    handler's own authorization check.  This only makes it visible.
    """
    # Only somebody actually addressing the bot.  Polling asks for every update
    # type, so without this a reaction or a membership change in any group the
    # bot sits in would be logged as a turned-away message and counted into a
    # notice that says N people messaged it — false, and at group scale it
    # trains the operator to ignore the one alert that matters.
    if (
        update.message is None
        and update.edited_message is None
        and update.callback_query is None
    ):
        return

    config: Config = context.bot_data["config"]
    user = update.effective_user
    if (
        user is None
        or user.id == getattr(context.bot, "id", None)
        or _is_authorized(user.id, config)
    ):
        return

    chat = update.effective_chat
    chat_type = chat.type if chat is not None else "unknown"
    # WARNING the first time this interval, DEBUG for their repeats.  Logging
    # goes to a rotating file, so one stranger sitting in a busy group would
    # otherwise cost a disk write for every message they send — and drown the
    # senders the operator has not seen before.
    _tally.attempts += 1
    first_sighting = user.id not in _tally.senders
    _tally.senders.add(user.id)
    logger.log(
        logging.WARNING if first_sighting else logging.DEBUG,
        "Turned away user %s (@%s) in a %s chat",
        user.id,
        user.username or "-",
        chat_type,
    )

    # Nobody to tell, and the attempt to tell is the disclosure.
    if not config.allowed_users:
        return

    now = time.monotonic()
    if _tally.last_sent is not None and now - _tally.last_sent < NOTICE_INTERVAL_SECONDS:
        return
    _tally.last_sent = now

    attempts, senders = _tally.attempts, _tally.senders
    _tally.attempts = 0
    _tally.senders = set()

    people = "someone" if len(senders) == 1 else f"{len(senders)} people"
    text = (
        f"Heads up: {people} contacted this bot and were turned away "
        f"({attempts} attempt(s)).\n"
        f"User ID(s): {_describe(senders)}\n"
        "Nothing was sent back to them. If that was you on another account, "
        "you can add it in the config."
    )

    for allowed in config.allowed_users:
        try:
            await context.bot.send_message(chat_id=allowed, text=text)
        except TelegramError:
            logger.debug("Could not deliver the turned-away note to %s", allowed)
