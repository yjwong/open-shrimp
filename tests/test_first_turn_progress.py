"""Telling the chat about a first-turn download.

Tens of megabytes arriving with nothing in the chat is indistinguishable from
a hang.  The sandbox path has said so for a while; a host-local CLI fetch
reuses the same message and the same in-place edits, without a build log to
link to.  Which fetch, and what to call it, is the backend's answer — the
orchestrator asks every backend and names none of them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.rich_stub import unwrap

from open_shrimp import client_manager as CM
from open_shrimp.backend.protocol import HostPrefetch
from open_shrimp.db import ChatScope


def _backend(prefetch: HostPrefetch | None):
    """A backend that answers only the question this function asks it."""
    return SimpleNamespace(name="stub", host_prefetch=lambda: prefetch)


class _FakeBot:
    """Records the rich sends and edits the progress sink makes."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []

    async def do_api_request(self, endpoint, api_kwargs=None, **_):
        call = unwrap(api_kwargs or {}).__dict__
        if endpoint == "sendRichMessage":
            self.sent.append(call)
            return SimpleNamespace(chat_id=call["chat_id"], message_id=7)
        if endpoint == "editMessageText":
            self.edits.append(call)
        return None


def test_the_lead_sentence_follows_the_caller():
    sandbox = CM._first_turn_text(CM._SANDBOX_BOOT_LEAD, 5_000_000, None)
    cli = CM._first_turn_text("Downloading the opencode CLI", 5_000_000, 60_000_000)

    assert sandbox.startswith("Starting your project for the first time — ")
    assert cli.startswith("Downloading the opencode CLI — ")
    # The trailing promise is right for both.
    assert sandbox.endswith("This only happens once.")
    assert cli.endswith("This only happens once.")
    # A server that declined to declare a length leaves the percentage out.
    assert "%" not in sandbox
    assert "8% (5 of 60 MB)" in cli


def test_the_opening_message_counts_nothing_yet():
    """Sent before the transfer has anything to report, where "0%" would read
    as stuck rather than as starting."""
    opening = CM._first_turn_text(CM._SANDBOX_BOOT_LEAD)

    assert opening == "Starting your project for the first time. This only happens once."


@pytest.mark.asyncio
async def test_a_sink_with_no_keyboard_still_edits():
    """Outside a sandbox there is no build log to link, and the markup rides on
    every edit precisely so it can be absent without being dropped."""
    bot = _FakeBot()
    message = SimpleNamespace(chat_id=1, message_id=7)
    sink = CM._chat_progress_sink(bot, message, None, "Downloading the opencode CLI")

    sink(1_000_000, 60_000_000)
    # The sink hands each edit back with ``run_coroutine_threadsafe``, so the
    # loop has to come round before the edit exists.
    await asyncio.sleep(0.05)

    assert bot.edits[0]["reply_markup"] is None
    assert "Downloading the opencode CLI" in bot.edits[0]["text"]


@pytest.mark.asyncio
async def test_a_backend_with_nothing_to_fetch_says_nothing():
    """Covers both "this backend ships its CLI" and "it is already
    downloaded" — the orchestrator cannot tell them apart and need not."""
    bot = _FakeBot()

    await CM._run_host_prefetch(
        _backend(None), bot, ChatScope(chat_id=1, thread_id=None)
    )

    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_pending_fetch_is_reported_as_it_moves():
    bot = _FakeBot()
    pending = HostPrefetch(
        label="Downloading the opencode CLI",
        fetch=lambda progress: progress(30_000_000, 60_000_000),
    )

    await CM._run_host_prefetch(
        _backend(pending), bot, ChatScope(chat_id=1, thread_id=9)
    )
    await asyncio.sleep(0.05)

    assert bot.sent[0]["thread_id"] == 9
    assert "Downloading the opencode CLI" in bot.sent[0]["text"]
    assert "50%" in bot.edits[0]["text"]


@pytest.mark.asyncio
async def test_a_failed_fetch_leaves_the_error_to_the_spawn(caplog):
    """The backend's own error says more about a missing CLI than this could,
    so the turn continues to the client spawn that raises it."""

    def boom(progress):
        raise RuntimeError("github is down")

    pending = HostPrefetch(label="Downloading the opencode CLI", fetch=boom)

    with caplog.at_level("WARNING"):
        await CM._run_host_prefetch(
            _backend(pending), _FakeBot(), ChatScope(chat_id=1, thread_id=None)
        )
    assert "Downloading the opencode CLI failed" in caplog.text


@pytest.mark.asyncio
async def test_the_real_backends_answer_the_hook():
    """The two shipped backends satisfy the protocol: claude has nothing to
    fetch, opencode names its CLI."""
    from open_shrimp.backend import get_backend_by_name

    assert get_backend_by_name("claude_sdk").host_prefetch() is None
    pending = get_backend_by_name("opencode").host_prefetch()
    assert pending is None or pending.label == "Downloading the opencode CLI"
