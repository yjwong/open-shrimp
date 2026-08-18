"""Handing out Mini App links, and never handing out a dead one.

Telegram ``web_app`` (Mini App) buttons only work in private chats.  In group
chats and forum topics we fall back to regular ``url`` buttons with a short-
lived HMAC auth token appended as a query parameter.

Both kinds are loaded by Telegram's own servers rather than by the phone
holding the chat, so a base URL only this machine can reach renders as an
ordinary-looking button that does nothing at all when tapped — see
:func:`open_shrimp.web_url.is_public_base` for why, and
:func:`open_shrimp.web_url.mini_app_base` for the one function allowed to
answer with a base.

The invariant is carried by the type rather than by convention: every entry
point here takes ``base: str | None`` and yields ``None`` when it is ``None``,
so a caller who forgets renders no button.  Commands that exist *to* open a
Mini App use :func:`reply_mini_app`, which turns that silence into an
explanation.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from open_shrimp.config import Config
from open_shrimp.review.auth import generate_auth_token
from open_shrimp.web_url import mini_app_base


def make_web_app_button(
    text: str,
    base: str | None,
    path: str,
    *,
    chat_id: int,
    user_id: int,
    bot_token: str,
    is_private_chat: bool,
) -> InlineKeyboardButton | None:
    """Create a Mini App button, or ``None`` if Telegram could not open it.

    ``base`` must have come from :func:`open_shrimp.web_url.mini_app_base`,
    which is the only producer of one; ``path`` is everything below it, e.g.
    ``"/terminal/?mode=login"``.  Splitting the URL in two is what lets the
    ``None`` propagate: a caller cannot concatenate its way past the guard
    without first having a base that does not exist.

    In private chats this is a ``web_app`` button (opens inside Telegram's
    Mini App WebView with ``initData`` for auth).  In group and forum chats it
    is a regular ``url`` button carrying an HMAC auth token, which opens in an
    external browser.
    """
    if base is None:
        return None
    url = f"{base}{path}"
    if is_private_chat:
        return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))

    token = generate_auth_token(user_id, chat_id, bot_token)
    return InlineKeyboardButton(text, url=_append_query_param(url, "token", token))


def mini_app_keyboard(
    buttons: Sequence[tuple[str, str]],
    *,
    config: Config,
    chat_id: int,
    user_id: int,
    is_private_chat: bool,
) -> InlineKeyboardMarkup | None:
    """A keyboard of ``(label, path)`` buttons, one per row, or ``None``.

    For callers holding a ``Config`` rather than a base — they never see one,
    so there is nothing to get wrong.
    """
    base = mini_app_base(config)
    if base is None:
        return None
    return InlineKeyboardMarkup([
        [
            make_web_app_button(
                label,
                base,
                path,
                chat_id=chat_id,
                user_id=user_id,
                bot_token=config.telegram.token,
                is_private_chat=is_private_chat,
            )
        ]
        for label, path in buttons
    ])


def _unavailable_text(opens: str, still_works: str) -> str:
    """Why the button is missing, for someone who has never seen a terminal.

    It names the failure, its one consequence, and the two ways out — the same
    two the readiness card offers, in a beginner's register rather than an
    operator's.  Refusing without explaining is the failure this replaces.
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
    buttons: Sequence[tuple[str, str]],
    config: Config,
    user_id: int,
    is_private_chat: bool,
    opens: str,
    still_works: str,
) -> None:
    """Reply with a Mini App keyboard, or with the reason there isn't one.

    The shape every command handing out a Mini App should use.  ``text`` is
    MarkdownV2 and is sent only on the button path; the explanation is plain,
    because it is prose full of characters MarkdownV2 would reject.

    ``opens`` names the thing in the user's words ("the sign-in page") and
    ``still_works`` says what they can do meanwhile, so the reply explains
    rather than refuses.
    """
    keyboard = mini_app_keyboard(
        buttons,
        config=config,
        chat_id=message.chat_id,
        user_id=user_id,
        is_private_chat=is_private_chat,
    )
    if keyboard is None:
        await message.reply_text(_unavailable_text(opens, still_works))
        return
    await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


def _append_query_param(url: str, key: str, value: str) -> str:
    """Append a query parameter to a URL, preserving existing params."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params[key] = [value]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))
