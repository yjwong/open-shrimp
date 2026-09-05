"""A live draft has to be refreshed often enough, and sent rarely enough.

Telegram's clients delete a live draft ``message_typing_draft_ttl`` seconds
after the last one they received, and the server stops accepting them past 20
calls in 5 seconds or 40 in 30.  Both ends of that window make the draft
disappear mid-turn, so both are pinned here.
"""

from __future__ import annotations

import datetime as dtm
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

from open_shrimp import rich_message
from open_shrimp.rich_message import (
    DRAFT_TTL_SECONDS,
    _DRAFT_MIN_INTERVAL,
    _draft_budgets,
    draft_budget_spent,
    send_rich_draft,
)
from open_shrimp.stream import (
    DRAFT_INTERVAL_SECONDS,
    DRAFT_KEEPALIVE_SECONDS,
    _draft_is_stale,
    _DraftState,
    _send_draft,
)


@pytest.fixture(autouse=True)
def _clean_budgets() -> Any:
    """The budgets are module state, shared across tests without this."""
    _draft_budgets.clear()
    yield
    _draft_budgets.clear()


def _spend(chat_id: int, times: list[float]) -> None:
    """Book sends at ``times``, which must be in order."""
    for sent in times:
        _draft_budgets[chat_id].charge(sent)


# --- Rate limiting ----------------------------------------------------------

def test_a_fresh_peer_may_send_immediately() -> None:
    assert _draft_budgets[1].spent(100.0) is False


def test_twenty_calls_in_five_seconds_defers_the_twenty_first() -> None:
    # Twenty calls spread over the last four seconds: under the 30-second
    # tier, at the 5-second one.
    _spend(1, [100.0 + i * 0.2 for i in range(20)])
    assert _draft_budgets[1].spent(104.0) is True
    # The oldest of the twenty leaves the 5-second window at 105.0.
    assert _draft_budgets[1].spent(105.0) is False


def test_the_thirty_second_tier_binds_a_half_second_cadence() -> None:
    """A 0.5s flush is 60 calls in 30 seconds, which the 40-call tier stops."""
    _spend(1, [100.0 + i * 0.5 for i in range(40)])
    # Asked far enough past the last send that pacing is not what refuses it.
    assert _draft_budgets[1].spent(120.5) is True


def test_pacing_never_opens_a_gap_the_ttl_can_close() -> None:
    """A paced sender is never stalled, so no gap approaches the TTL.

    Spending the allowance as fast as the tiers alone permit — 20 calls, then
    20 more five seconds later — blocks the peer until 30 seconds after the
    first of them, a 25-second silence that deletes the draft.  Driving the
    budget at the flush loop's own cadence for two minutes shows the worst gap
    pacing leaves instead: one tick past the interval.
    """
    budget = _draft_budgets[1]
    sent: list[float] = []
    now = 100.0
    while now < 220.0:
        if not budget.spent(now):
            budget.charge(now)
            sent.append(now)
        now += DRAFT_INTERVAL_SECONDS

    gaps = [later - earlier for earlier, later in zip(sent, sent[1:])]
    assert max(gaps) <= _DRAFT_MIN_INTERVAL + DRAFT_INTERVAL_SECONDS
    assert max(gaps) < DRAFT_KEEPALIVE_SECONDS < DRAFT_TTL_SECONDS


def test_calls_older_than_the_widest_window_stop_counting() -> None:
    _spend(1, [100.0 + i * 0.5 for i in range(40)])
    assert _draft_budgets[1].spent(150.0) is False
    # And a later send drops the ones that can no longer constrain anything.
    _draft_budgets[1].charge(150.0)
    assert len(_draft_budgets[1]._calls) == 1


def test_peers_hold_separate_budgets() -> None:
    _spend(1, [100.0 + i * 0.2 for i in range(20)])
    assert _draft_budgets[1].spent(104.0) is True
    assert _draft_budgets[2].spent(104.0) is False


@pytest.mark.asyncio
async def test_a_spent_budget_defers_the_send_without_calling_telegram() -> None:
    bot = AsyncMock()
    now = time.monotonic()
    _spend(7, [now - 20.0 + i * 0.5 for i in range(40)])

    assert draft_budget_spent(7) is True
    assert await send_rich_draft(bot, 7, 1, "hi") is False
    assert bot.do_api_request.await_count == 0


@pytest.mark.asyncio
async def test_an_unspent_budget_sends() -> None:
    bot = AsyncMock()
    assert await send_rich_draft(bot, 7, 1, "hi") is True
    assert bot.do_api_request.await_count == 1


@pytest.mark.asyncio
async def test_flood_wait_holds_the_peer_for_the_named_delay() -> None:
    bot = AsyncMock()
    bot.do_api_request.side_effect = RetryAfter(dtm.timedelta(seconds=3))

    assert await send_rich_draft(bot, 7, 1, "hi") is False
    # Still inside the cool-down, so nothing else reaches the API.
    assert await send_rich_draft(bot, 7, 1, "hi") is False
    assert bot.do_api_request.await_count == 1


# --- Keepalive --------------------------------------------------------------

def _state_with_content() -> _DraftState:
    state = _DraftState(chat_id=1)
    state.append_gfm("half an answer")
    return state


def test_a_draft_older_than_the_keepalive_is_stale() -> None:
    state = _state_with_content()
    state.last_draft_sent = time.monotonic() - DRAFT_KEEPALIVE_SECONDS - 1
    assert _draft_is_stale(state) is True


def test_a_draft_just_sent_is_not_stale() -> None:
    state = _state_with_content()
    state.last_draft_sent = time.monotonic()
    assert _draft_is_stale(state) is False


def test_nothing_to_refresh_before_the_first_draft_lands() -> None:
    state = _state_with_content()
    assert state.last_draft_sent == 0.0
    assert _draft_is_stale(state) is False


def test_an_empty_buffer_is_not_kept_alive() -> None:
    state = _DraftState(chat_id=1)
    state.last_draft_sent = time.monotonic() - 1000
    assert _draft_is_stale(state) is False


def test_reasoning_alone_is_worth_keeping_alive() -> None:
    """Thinking rides in the draft and nowhere else, so it has to survive."""
    state = _DraftState(chat_id=1)
    state.thinking = "weighing two approaches"
    state.last_draft_sent = time.monotonic() - 1000
    assert _draft_is_stale(state) is True


def test_the_live_edit_fallback_needs_no_keepalive() -> None:
    state = _state_with_content()
    state.drafts_disabled = True
    state.last_draft_sent = time.monotonic() - 1000
    assert _draft_is_stale(state) is False


def test_a_new_message_forgets_the_previous_draft_send() -> None:
    state = _state_with_content()
    state.last_draft_sent = time.monotonic()
    state.begin_new_message()
    assert state.last_draft_sent == 0.0


# --- The two together -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_deferred_send_stays_dirty_and_unstamped() -> None:
    bot = AsyncMock()
    state = _state_with_content()
    state.dirty = True
    now = time.monotonic()
    _spend(state.chat_id, [now - 20.0 + i * 0.5 for i in range(40)])

    await _send_draft(bot, state)

    assert bot.do_api_request.await_count == 0
    assert state.dirty is True
    assert state.last_draft_sent == 0.0


@pytest.mark.asyncio
async def test_a_sent_draft_clears_dirty_and_stamps_the_time() -> None:
    bot = AsyncMock()
    state = _state_with_content()
    state.dirty = True

    await _send_draft(bot, state)

    assert bot.do_api_request.await_count == 1
    assert state.dirty is False
    assert state.last_draft_sent > 0.0
