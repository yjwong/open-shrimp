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

The same card carries the readiness checklist from :mod:`open_shrimp.readiness`,
because the questions it answers — am I signed in, can my buttons open, is my
project still there — are the ones whose answers are otherwise silence.  The
orientation is said once ever; the checklist is said again whenever something
that used to work stops working, and never merely because it is still broken.
"""

from __future__ import annotations

import logging

import aiosqlite
from telegram import Bot

from open_shrimp.config import Config
from open_shrimp.db import claim_once, release_once
from open_shrimp.handlers.utils import NO_CONTEXT_TEXT
from open_shrimp.markdown import TELEGRAM_MAX_LENGTH
from open_shrimp.readiness import KEYS, Row, State, check_readiness, readiness_text

logger = logging.getLogger(__name__)

# Claimed per person, not per install, because that is who the message is for:
# re-running the wizard against a different Telegram account should orient the
# new account, and must not orient the old one twice.
ORIENTATION = "orientation"

# Claimed per person *and* per row, which is the granularity of the thing being
# said: a second problem deserves a second message, and the same problem on the
# tenth restart does not.
READINESS = "readiness"


def orientation_text(config: Config) -> str:
    """What this is, how to talk to it, and where to look things up.

    Names the project by its description rather than its directory: an
    absolute filesystem path is not an orientation, and it is the second thing
    a new user would otherwise ever see.
    """
    # Keyed on whether any project exists, not on whether a default is set:
    # an install with projects but no default still has plenty to work on, and
    # is asked to choose rather than told it has nothing.
    if not config.contexts:
        return "\n".join(
            [
                "OpenShrimp is running.",
                "",
                NO_CONTEXT_TEXT,
                "",
                "Once a project exists, just send me a message — or a voice "
                "note. There's no command to learn: describe what you want "
                "and I'll do it, asking first before I change any file or "
                "run anything.",
            ]
        )

    default = config.default_context
    context = config.contexts.get(default) if default is not None else None
    project = (context.description if context else None) or default

    if project is None:
        return "\n".join(
            [
                "OpenShrimp is running.",
                "",
                "Pick a project with /context and I'll get started.",
                "",
                "Just send me a message — or a voice note. There's no command "
                "to learn: describe what you want and I'll do it, asking "
                "first before I change any file or run anything.",
                "",
                "Worth knowing:",
                "  /context — choose or switch project",
                "  /clear — start a fresh conversation",
                "  /status — see what I'm working on",
            ]
        )

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


def _row_claim(row_key: str) -> str:
    return f"{READINESS}:{row_key}"


async def _new_failures(
    db: aiosqlite.Connection, rows: list[Row], user_id: int
) -> list[str]:
    """Claim every failing row for *user_id* and return the ones that are new.

    A row that passes gives its claim back, so the same problem returning
    later is news again.  A row that could not be checked changes nothing: the
    claim records what this person has been *told*, and a probe that failed to
    answer is not evidence that the problem went away — treating it as such
    would re-announce a standing failure every time a flaky check flapped,
    which is the nag the whole design exists to avoid.

    The whole key set is walked rather than today's rows, because a row can
    stop being produced entirely — a sandbox left the config, a backend this
    platform has no checks for — and a claim nobody will ever release is a
    problem that can only ever be reported once.
    """
    failing = {row.key for row in rows if row.state is State.PROBLEM}
    unknown = {row.key for row in rows if row.state is State.UNKNOWN}
    news: list[str] = []
    for key in KEYS:
        if key in unknown:
            continue
        if key not in failing:
            await release_once(db, _row_claim(key), str(user_id))
        elif await claim_once(db, _row_claim(key), str(user_id)):
            news.append(key)
    return news


def _card(config: Config, rows: list[Row], *, orienting: bool) -> str:
    """The message body — the whole checklist either way.

    A regression is reported with every row and not only the one that broke,
    because someone reading "the tunnel is down" wants to know in the same
    breath whether anything else is.

    Always one message, and always short enough to send.  Telegram refuses an
    over-long one outright, and the install most likely to produce one — many
    contexts, every prerequisite missing — is the install that needs the card
    most, so the length is spent on the first rows and the rest is delegated.
    """
    checklist = readiness_text(rows) if rows else ""
    head = orientation_text(config) if orienting else "Something needs your attention."
    text = f"{head}\n\n{checklist}" if checklist else head
    if len(text) <= TELEGRAM_MAX_LENGTH:
        return text
    tail = "\n\n…and more. Run `openshrimp doctor` to see the rest."
    return text[: TELEGRAM_MAX_LENGTH - len(tail)].rstrip() + tail


async def send_first_boot(bot: Bot, db: aiosqlite.Connection, config: Config) -> int:
    """DM each allowed user what they have not been told yet, and count the sends.

    Claims are taken before the send and given back if it fails, so a blocked
    bot or a dropped connection costs a retry on the next boot rather than the
    product's only explanation of itself.
    """
    rows = await check_readiness(config)
    sent = 0
    for user_id in config.allowed_users:
        orienting = await claim_once(db, ORIENTATION, str(user_id))
        news = await _new_failures(db, rows, user_id)
        if not orienting and not news:
            continue
        try:
            await bot.send_message(
                chat_id=user_id, text=_card(config, rows, orienting=orienting)
            )
        except Exception:
            # Every failure gives the claims back, not just the Telegram ones:
            # what is being protected is that nothing is recorded as said
            # unless it was, and an unexpected exception said it least of all.
            logger.warning("Could not send the first-boot card to %s", user_id, exc_info=True)
            if orienting:
                await release_once(db, ORIENTATION, str(user_id))
            for key in news:
                await release_once(db, _row_claim(key), str(user_id))
        else:
            sent += 1
    return sent
