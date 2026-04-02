"""Helper for creating Mini App buttons that work in both private and group chats.

Telegram ``web_app`` (Mini App) buttons only work in private chats.  In group
chats and forum topics, we fall back to regular ``url`` buttons with a short-
lived HMAC auth token appended as a query parameter.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from telegram import InlineKeyboardButton, WebAppInfo

from open_shrimp.review.auth import generate_auth_token


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
    """
    if is_private_chat:
        return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))

    token = generate_auth_token(user_id, chat_id, bot_token)
    url_with_token = _append_query_param(url, "token", token)
    return InlineKeyboardButton(text, url=url_with_token)


def _append_query_param(url: str, key: str, value: str) -> str:
    """Append a query parameter to a URL, preserving existing params."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params[key] = [value]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))
