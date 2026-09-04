"""Stand-ins for the raw rich-message API.

``sendRichMessage``, ``sendRichMessageDraft`` and
``editMessageText(rich_message=...)`` have no python-telegram-bot method, so
every message the bot sends goes out through ``bot.do_api_request``.  These
helpers unwrap that envelope back into the (text, keyboard, thread) shape
assertions are written against.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock


def unwrap(api_kwargs: dict[str, Any]) -> SimpleNamespace:
    """The parts of an ``InputRichMessage`` call a test cares about."""
    return SimpleNamespace(
        chat_id=api_kwargs.get("chat_id"),
        message_id=api_kwargs.get("message_id"),
        text=api_kwargs.get("rich_message", {}).get("markdown", ""),
        thread_id=api_kwargs.get("message_thread_id"),
        reply_markup=api_kwargs.get("reply_markup"),
    )


class RichBot:
    """A bot that records rich sends and edits in the order they were made."""

    def __init__(self) -> None:
        self.sends: list[SimpleNamespace] = []
        self.edits: list[SimpleNamespace] = []
        self._next_id = 1000

    async def do_api_request(
        self, endpoint: str, api_kwargs: dict[str, Any] | None = None, **_: Any,
    ) -> Any:
        call = unwrap(api_kwargs or {})
        if endpoint == "sendRichMessage":
            self.sends.append(call)
            message_id = self._next_id
            self._next_id += 1
            return SimpleNamespace(message_id=message_id)
        if endpoint == "editMessageText":
            self.edits.append(call)
        return None

    @property
    def texts(self) -> list[str]:
        return [call.text for call in self.sends]


def wire_rich(bot: Any) -> Any:
    """Route a mock bot's rich API back onto its ``send_message`` mock.

    For tests already written against ``send_message`` / ``edit_message_text``:
    the assertions keep working while the code under test uses the real send
    path.
    """

    async def api(endpoint, api_kwargs=None, **_):
        kwargs = dict(api_kwargs or {})
        text = kwargs.pop("rich_message")["markdown"]
        if endpoint == "sendRichMessage":
            return await bot.send_message(
                chat_id=kwargs.pop("chat_id"), text=text, **kwargs,
            )
        if endpoint == "editMessageText":
            return await bot.edit_message_text(
                chat_id=kwargs.pop("chat_id"),
                message_id=kwargs.pop("message_id"),
                text=text,
                **kwargs,
            )
        return None

    bot.do_api_request = AsyncMock(side_effect=api)
    if not isinstance(getattr(bot, "edit_message_text", None), AsyncMock):
        bot.edit_message_text = AsyncMock()
    return bot


class RichMessage:
    """A ``Message`` stand-in that ``reply_rich`` can address."""

    def __init__(
        self,
        text: str = "",
        *,
        chat_id: int = 1,
        thread_id: int | None = None,
        message_id: int = 1,
        bot: RichBot | None = None,
    ) -> None:
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.message_thread_id = thread_id
        self.is_topic_message = thread_id is not None
        self.bot = bot or RichBot()

    def get_bot(self) -> RichBot:
        return self.bot

    @property
    def replies(self) -> list[str]:
        return self.bot.texts


_UNESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def rendered(text: str) -> str:
    """Roughly what Telegram displays for a rich body.

    Undoes the backslash escapes and the three HTML entities, and turns the
    explicit breaks back into newlines.  Markup characters are left in place;
    these tests care about the words, not the styling.
    """
    text = _UNESCAPE_RE.sub(r"\1", text.replace("<br>", "\n"))
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text


def rich_sends(bot: Any) -> list[SimpleNamespace]:
    """The ``sendRichMessage`` calls recorded on a mock bot."""
    return [
        unwrap(call.kwargs.get("api_kwargs") or {})
        for call in bot.do_api_request.call_args_list
        if call.args and call.args[0] == "sendRichMessage"
    ]
