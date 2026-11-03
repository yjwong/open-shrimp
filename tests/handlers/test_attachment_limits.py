"""Attachment downloads that cannot succeed are reported, never swallowed.

The Bot API refuses ``getFile`` above a size cap.  A file that trips it is
named back to the user with its size, and it never costs the user the other
attachments sent alongside it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.rich_stub import wire_rich
from telegram.error import BadRequest, NetworkError

from open_shrimp.db import ChatScope
from open_shrimp.handlers.messages import (
    _MAX_DOWNLOAD_BYTES,
    _download_all_attachments,
    _warn_skipped_attachments,
)

pytestmark = pytest.mark.asyncio


def _doc_message(name: str, size: int | None, file_id: str = "fid") -> SimpleNamespace:
    return SimpleNamespace(
        photo=None,
        document=SimpleNamespace(
            file_id=file_id, file_size=size, file_name=name, mime_type="application/zip"
        ),
        audio=None,
    )


def _photo_message(size: int, file_id: str = "pid") -> SimpleNamespace:
    return SimpleNamespace(
        photo=[SimpleNamespace(file_id=file_id, file_size=size)],
        document=None,
        audio=None,
    )


def _bot(payload: bytes = b"data", get_file=None) -> SimpleNamespace:
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(payload)))
    return SimpleNamespace(get_file=get_file or AsyncMock(return_value=file))


async def test_oversized_document_is_skipped_without_calling_get_file() -> None:
    bot = _bot()
    message = _doc_message("recording.zip", _MAX_DOWNLOAD_BYTES + 1)

    attachments, skipped = await _download_all_attachments([message], bot)

    assert attachments == []
    assert len(skipped) == 1
    assert "recording.zip" in skipped[0]
    assert "20 MB limit" in skipped[0]
    # The size is known up front, so the doomed round trip is never made.
    bot.get_file.assert_not_called()


async def test_file_at_the_limit_is_still_downloaded() -> None:
    bot = _bot()
    message = _doc_message("just-fits.zip", _MAX_DOWNLOAD_BYTES)

    attachments, skipped = await _download_all_attachments([message], bot)

    assert skipped == []
    assert len(attachments) == 1
    assert attachments[0].filename == "just-fits.zip"


async def test_oversized_file_does_not_cost_the_others() -> None:
    bot = _bot()
    messages = [
        _doc_message("huge.zip", _MAX_DOWNLOAD_BYTES * 2),
        _photo_message(1024),
    ]

    attachments, skipped = await _download_all_attachments(messages, bot)

    # The photo still arrives even though the document in the same batch did not.
    assert len(attachments) == 1
    assert attachments[0].mime_type == "image/jpeg"
    assert len(skipped) == 1
    assert "huge.zip" in skipped[0]


async def test_missing_file_size_falls_back_to_the_api_error() -> None:
    # Telegram omits file_size on some attachments, so the cap can only be
    # discovered by asking.
    bot = _bot(get_file=AsyncMock(side_effect=BadRequest("File is too big")))
    message = _doc_message("mystery.bin", None)

    attachments, skipped = await _download_all_attachments([message], bot)

    assert attachments == []
    assert "mystery.bin" in skipped[0]
    assert "20 MB limit" in skipped[0]


async def test_transport_failure_is_reported_not_raised() -> None:
    bot = _bot(get_file=AsyncMock(side_effect=NetworkError("connection reset")))
    message = _doc_message("notes.pdf", 4096)

    attachments, skipped = await _download_all_attachments([message], bot)

    assert attachments == []
    assert "notes.pdf" in skipped[0]
    assert "connection reset" in skipped[0]


async def test_healthy_download_reports_nothing_skipped() -> None:
    bot = _bot(payload=b"pdf-bytes")
    message = _doc_message("notes.pdf", 4096)

    attachments, skipped = await _download_all_attachments([message], bot)

    assert skipped == []
    assert attachments[0].data == b"pdf-bytes"


async def _warn(bot, scope: ChatScope, skipped: list[str], caption: str = "") -> bool:
    return await _warn_skipped_attachments(
        bot, scope, skipped, MagicMock(),
        user_id=1, is_private_chat=True, caption=caption,
    )


async def test_warning_names_each_skipped_file() -> None:
    bot = wire_rich(SimpleNamespace(send_message=AsyncMock()))
    scope = ChatScope(chat_id=7, thread_id=3)

    with patch("open_shrimp.file_upload.api.upload_keyboard", return_value=None):
        offered = await _warn(bot, scope, ["huge.zip — 41.0 MB, over the 20 MB limit"])

    assert offered is False

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 7
    assert kwargs["message_thread_id"] == 3
    # A filename is a name, not markup: an underscore in it must not open
    # an emphasis run, and a dot must not leave a backslash on screen.
    assert "huge.zip" in kwargs["text"]
    assert "41.0 MB" in kwargs["text"]
    assert "\\" not in kwargs["text"]


async def test_nothing_skipped_sends_no_message() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())

    assert await _warn(bot, ChatScope(chat_id=7, thread_id=None), []) is False

    bot.send_message.assert_not_called()


async def test_a_failing_report_never_escapes() -> None:
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=NetworkError("down")))

    # The user already lost the attachment; losing the turn as well is worse.
    assert await _warn(bot, ChatScope(chat_id=7, thread_id=None), ["x — nope"]) is False


async def test_oversized_file_offers_the_upload_page() -> None:
    bot = wire_rich(SimpleNamespace(send_message=AsyncMock()))
    scope = ChatScope(chat_id=7, thread_id=3)
    markup = MagicMock()

    with patch(
        "open_shrimp.file_upload.api.upload_keyboard", return_value=markup,
    ) as keyboard:
        offered = await _warn(
            bot, scope, ["huge.zip — 41.0 MB, over the 20 MB limit"],
            caption="summarise this",
        )

    assert offered is True
    assert keyboard.call_args.kwargs["caption"] == "summarise this"
    assert bot.send_message.await_args.kwargs["reply_markup"] is markup
    assert "upload page" in bot.send_message.await_args.kwargs["text"]


async def test_other_failures_offer_no_upload_page() -> None:
    bot = wire_rich(SimpleNamespace(send_message=AsyncMock()))

    with patch("open_shrimp.file_upload.api.upload_keyboard") as keyboard:
        offered = await _warn(
            bot, ChatScope(chat_id=7, thread_id=None),
            ["notes.pdf — download failed: connection reset"],
        )

    assert offered is False
    keyboard.assert_not_called()
