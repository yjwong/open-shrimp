"""The first thing the bot says, on the first boot of a config.

Telegram shows the START affordance only on a fresh chat with a bot, and the
setup wizard's enrollment handshake spends that single press: it arrives at the
wizard, not at the core, which is not even running.  Nobody types ``/start`` at
a bot they have already started, so the greeting that used to carry the
product's only explanation of itself would otherwise never reach the person it
was written for.

Boot is also the honest moment for it.  The wizard cannot send orientation
because at that point it does not know whether the bot will work, and
instructions that arrive before the bot can act on them are worse than none.
This card arrives only when the core is alive, which is the same signal the
user needs.
"""

from __future__ import annotations

import logging

import aiosqlite
from telegram import Bot
from telegram.error import TelegramError

from open_shrimp.config import Config
from open_shrimp.db import claim_once, release_once

logger = logging.getLogger(__name__)

# Claimed per person, not per install, because that is who the message is for:
# re-running the wizard against a different Telegram account should orient the
# new account, and must not orient the old one twice.
ORIENTATION = "orientation"


def orientation_text(config: Config) -> str:
    """What this is, how to talk to it, and where to look things up.

    Names the project by its description rather than its directory: an
    absolute filesystem path is not an orientation, and it is the second thing
    a new user would otherwise ever see.
    """
    context = config.contexts.get(config.default_context)
    project = (context.description if context else None) or config.default_context

    return "\n".join(
        [
            "OpenShrimp is running.",
            "",
            f"I'm working on {project}.",
            "",
            "Just send me a message — or a voice note. There's no command to "
            "learn: describe what you want and I'll do it, asking first "
            "before I change any file or run anything.",
            "",
            "Worth knowing:",
            "  /context — switch to another project",
            "  /clear — start a fresh conversation",
            "  /status — see what I'm working on",
        ]
    )


async def send_orientation(bot: Bot, db: aiosqlite.Connection, config: Config) -> int:
    """DM each allowed user their orientation, exactly once, and count the sends.

    The flag is claimed before the send and released if the send fails, so a
    blocked bot or a dropped connection costs a retry on the next boot rather
    than the product's only explanation of itself.
    """
    text = orientation_text(config)
    sent = 0
    for user_id in config.allowed_users:
        if not await claim_once(db, ORIENTATION, str(user_id)):
            continue
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except TelegramError:
            logger.warning("Could not send the orientation to %s", user_id, exc_info=True)
            await release_once(db, ORIENTATION, str(user_id))
        else:
            sent += 1
    return sent
