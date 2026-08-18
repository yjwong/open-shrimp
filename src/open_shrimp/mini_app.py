"""Handing out Mini App links, and never handing out a dead one.

Telegram ``web_app`` (Mini App) buttons only work in private chats.  In group
chats and forum topics we fall back to regular ``url`` buttons with a short-
lived HMAC auth token appended as a query parameter.

Both kinds are loaded by Telegram's own servers rather than by the phone
holding the chat, so a base URL that only this machine can reach renders as a
perfectly ordinary button that silently does nothing when tapped — the state a
quick tunnel that failed to start leaves behind.

The invariant that keeps that from happening lives one level up:
:func:`open_shrimp.web_url.mini_app_base` is the only function that yields a
base for a Mini App URL, and it answers ``None`` when Telegram could not open
one.  :func:`mini_app_keyboard` and :func:`reply_mini_app` ask it themselves
and never let a caller supply a base; :func:`make_web_app_button`, which the
incidental buttons elsewhere in the tree still call with a whole URL, cannot
check it and says so.  Either way the failure mode of forgetting is *no
button* — safe, if unhelpful — rather than a broken one.

Commands that exist *to* open a Mini App should use :func:`reply_mini_app`,
which turns the unhelpful half into an explanation.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from open_shrimp.config import Config
from open_shrimp.review.auth import generate_auth_token
from open_shrimp.web_url import mini_app_base

# A row of ``(button label, path below the Mini App base)`` pairs — callers
# never see the base itself, so they cannot build a URL that skips the guard.
Row = Sequence[tuple[str, str]]


def make_web_app_button(
    text: str,
    url: str,
    *,
    chat_id: int,
    user_id: int,
    bot_token: str,
    is_private_chat: bool,
) -> InlineKeyboardButton:
    """Create an InlineKeyboardButton for a web app.

    In private chats, returns a ``web_app`` button (opens inside Telegram's
    Mini App WebView with ``initData`` for auth).

    In group/forum chats, generates an HMAC auth token, appends it to the
    URL, and returns a regular ``url`` button (opens in external browser).

    ``url`` must extend a base that came from
    :func:`open_shrimp.web_url.mini_app_base`; this function cannot check that
    for itself, which is why callers pass a base of ``str | None`` around and
    drop the button when it is ``None``.
    """
    if is_private_chat:
        return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))

    token = generate_auth_token(user_id, chat_id, bot_token)
    url_with_token = _append_query_param(url, "token", token)
    return InlineKeyboardButton(text, url=url_with_token)


def mini_app_keyboard(
    rows: Sequence[Row],
    *,
    config: Config,
    chat_id: int,
    user_id: int,
    is_private_chat: bool,
) -> InlineKeyboardMarkup | None:
    """Build a keyboard of Mini App buttons, or ``None`` if there can be none.

    ``rows`` holds ``(label, path)`` pairs — ``path`` is everything after the
    base, e.g. ``"/terminal/?mode=login"``.
    """
    base = mini_app_base(config)
    if base is None:
        return None
    return InlineKeyboardMarkup([
        [
            make_web_app_button(
                label,
                f"{base}{path}",
                chat_id=chat_id,
                user_id=user_id,
                bot_token=config.telegram.token,
                is_private_chat=is_private_chat,
            )
            for label, path in row
        ]
        for row in rows
    ])


def mini_app_unavailable_text(opens: str, still_works: str) -> str:
    """Why the button is missing, for someone who has never seen a terminal.

    It names the failure, its one consequence, and the two ways out.  Refusing
    without explaining is the failure this whole path exists to remove.
    """
    return (
        f"I can't open {opens} at the moment.\n\n"
        "It's a Mini App — one of the little windows that open inside "
        "Telegram — and Telegram loads those from its own servers rather than "
        "from your phone. So it needs a web address that reaches this machine "
        "from the internet, and right now there isn't one. The tunnel that "
        "normally provides that address either didn't start or has stopped.\n\n"
        f"{still_works}\n\n"
        "To fix it: restart OpenShrimp on the machine it runs on — it opens a "
        "fresh tunnel every time it starts, and that is usually all it takes. "
        "If it keeps failing, something on that machine's network is blocking "
        "the tunnel, and the way round it is to give OpenShrimp an https "
        "address of its own under review.public_url in the settings file."
    )


async def reply_mini_app(
    message: Message,
    *,
    text: str,
    rows: Sequence[Row],
    config: Config,
    user_id: int,
    is_private_chat: bool,
    opens: str,
    still_works: str,
    parse_mode: str | None = "MarkdownV2",
) -> Message:
    """Reply with a Mini App keyboard, or with the reason there isn't one.

    This is the shape every command that hands out a Mini App should use: the
    guard is not something the handler is trusted to remember, because the
    handler never gets a base URL it could have built a dead button from.

    ``opens`` names the thing in the user's words ("the sign-in page"), and
    ``still_works`` says what they can do meanwhile, so the reply is an
    explanation rather than a refusal.
    """
    keyboard = mini_app_keyboard(
        rows,
        config=config,
        chat_id=message.chat_id,
        user_id=user_id,
        is_private_chat=is_private_chat,
    )
    if keyboard is None:
        return await message.reply_text(
            mini_app_unavailable_text(opens, still_works)
        )
    return await message.reply_text(
        text, parse_mode=parse_mode, reply_markup=keyboard
    )


def _append_query_param(url: str, key: str, value: str) -> str:
    """Append a query parameter to a URL, preserving existing params."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params[key] = [value]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))
