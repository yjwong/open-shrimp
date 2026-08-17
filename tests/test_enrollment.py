"""The enrollment handshake.

``allowed_users`` is the only auth boundary in front of a bot that runs shell
commands, so every case here is one where a naive "first message wins" wizard
would write the wrong person in.
"""

from __future__ import annotations

import httpx
import pytest

from open_shrimp import enrollment
from open_shrimp.enrollment import (
    Candidate,
    EnrollmentWindow,
    PollConflict,
    TokenRejected,
)
from tests.telegram_stub import FakeTelegram, make_update, wait_for

TOKEN = "111:AAA-bbb"


def _message(user_id: int, *, text: str = "hello", **kwargs) -> dict:
    """A plain message, which the window's filters treat as contact."""
    return make_update(user_id, text=text, **kwargs)


# ── The window ──


class TestCandidateFilters:
    def test_private_human_message_is_a_candidate(self) -> None:
        window = EnrollmentWindow()
        candidate = window.offer(_message(7))
        assert candidate is not None
        assert candidate.user_id == 7
        assert candidate.code and len(candidate.code) == 6

    def test_group_message_produces_no_candidate(self) -> None:
        window = EnrollmentWindow()
        assert window.offer(_message(7, chat_type="supergroup")) is None
        assert window.candidates == []

    def test_bot_sender_produces_no_candidate(self) -> None:
        window = EnrollmentWindow()
        assert window.offer(_message(7, is_bot=True)) is None

    def test_edited_message_produces_no_candidate(self) -> None:
        window = EnrollmentWindow()
        raw = _message(7)
        assert window.offer({"update_id": 1, "edited_message": raw["message"]}) is None

    def test_channel_post_produces_no_candidate(self) -> None:
        window = EnrollmentWindow()
        raw = _message(7, chat_type="channel")
        assert window.offer({"update_id": 1, "channel_post": raw["message"]}) is None

    def test_repeat_from_the_same_sender_does_not_take_a_second_slot(self) -> None:
        window = EnrollmentWindow()
        first = window.offer(_message(7, update_id=1))
        assert window.offer(_message(7, update_id=2)) is None
        assert window.candidates == [first]


class TestCodes:
    def test_a_code_identifies_exactly_one_candidate(self) -> None:
        window = EnrollmentWindow()
        ada = window.offer(_message(7, update_id=1))
        bob = window.offer(_message(8, update_id=2))
        assert ada and bob

        assert window.submit(ada.code) == ada

    def test_codes_are_distinct(self) -> None:
        window = EnrollmentWindow()
        codes = {
            c.code
            for c in (window.offer(_message(i, update_id=i)) for i in range(1, 4))
            if c
        }
        assert len(codes) == 3

    def test_grouping_is_ignored_on_entry(self) -> None:
        window = EnrollmentWindow()
        ada = window.offer(_message(7))
        assert ada
        assert window.submit(enrollment.grouped_code(ada.code)) == ada

    def test_a_wrong_code_enrolls_nobody_and_leaves_the_window_open(self) -> None:
        window = EnrollmentWindow()
        ada = window.offer(_message(7))
        assert ada

        assert window.submit("000000") is None
        assert not window.closed
        assert window.submit(ada.code) == ada

    def test_five_wrong_entries_close_the_window(self) -> None:
        window = EnrollmentWindow()
        window.offer(_message(7))
        for _ in range(enrollment.MAX_WRONG_CODES):
            assert window.submit("000000") is None
        assert window.closed
        assert window.offer(_message(8, update_id=2)) is None

    def test_a_code_is_single_use(self) -> None:
        window = EnrollmentWindow()
        ada = window.offer(_message(7))
        assert ada

        assert window.submit(ada.code) == ada
        assert window.submit(ada.code) is None

    def test_non_ascii_digits_do_not_crash_the_prompt(self) -> None:
        """``str.isdigit`` is true for Arabic-Indic digits, which
        ``compare_digest`` then refuses to compare at all."""
        window = EnrollmentWindow()
        ada = window.offer(_message(7))
        assert ada

        assert window.submit("٤٣١٩٠٢") is None
        assert window.submit(ada.code) == ada

    def test_a_declined_candidate_cannot_be_replayed(self) -> None:
        """Declining reopens the window for a fresh message, not a retype."""
        window = EnrollmentWindow()
        ada = window.offer(_message(7))
        assert ada
        window.submit(ada.code)  # operator then declines

        assert window.submit(ada.code) is None
        assert not window.closed


class TestFlood:
    def test_a_fourth_candidate_gets_no_code(self) -> None:
        window = EnrollmentWindow()
        for i in range(1, enrollment.MAX_CANDIDATES + 1):
            assert window.offer(_message(i, update_id=i)) is not None

        assert window.offer(_message(99, update_id=99)) is None
        assert window.flooded
        assert len(window.candidates) == enrollment.MAX_CANDIDATES

    def test_the_cap_counts_people_not_codes(self) -> None:
        """Three distinct strangers is the bound worth holding.  Re-issuing to
        one already inside it widens the bot's audience by nobody."""
        window = EnrollmentWindow()
        first = window.offer(_message(1, update_id=1))
        assert first
        window.submit(first.code)  # operator declines; the code is spent

        for i in range(2, enrollment.MAX_CANDIDATES + 1):
            assert window.offer(_message(i, update_id=i)) is not None

        assert window.offer(_message(99, update_id=99)) is None
        assert window.flooded

    def test_a_declined_person_may_ask_again(self) -> None:
        """"Not me" must not be a dead end for an operator who mis-tapped it:
        the decline tells them to message again, so messaging again has to work.
        """
        window = EnrollmentWindow()
        ada = window.offer(_message(7, update_id=1))
        assert ada
        window.submit(ada.code)  # operator declines

        again = window.offer(_message(7, update_id=2))
        assert again is not None
        assert again.code != ada.code
        assert window.submit(again.code) == again

    def test_asking_again_does_not_buy_a_slot(self) -> None:
        window = EnrollmentWindow()
        for i in range(1, enrollment.MAX_CANDIDATES + 1):
            assert window.offer(_message(i, update_id=i)) is not None
        first = window.candidates[0]
        window.submit(first.code)  # declined

        assert window.offer(_message(first.user_id, update_id=10)) is not None
        assert window.offer(_message(99, update_id=99)) is None
        assert window.flooded

    def test_a_repeat_while_holding_a_code_is_ignored(self) -> None:
        """A second code is only a second thing to mistype."""
        window = EnrollmentWindow()
        ada = window.offer(_message(7, update_id=1))
        assert ada
        assert window.offer(_message(7, update_id=2)) is None
        assert window.candidates == [ada]

    def test_a_deep_link_candidate_counts_against_the_cap_too(self) -> None:
        window = EnrollmentWindow(nonce="s3cret")
        for i in range(1, enrollment.MAX_CANDIDATES + 1):
            assert window.offer(_message(i, update_id=i)) is not None

        assert window.offer(_message(99, update_id=99, text="/start s3cret")) is None


class TestThreads:
    """A chat with Threaded Mode on is many conversations."""

    def test_the_thread_is_carried(self) -> None:
        window = EnrollmentWindow()
        candidate = window.offer(_message(7, thread_id=931))
        assert candidate is not None
        assert candidate.thread_id == 931

    def test_a_chat_without_threads_carries_none(self) -> None:
        window = EnrollmentWindow()
        candidate = window.offer(_message(7))
        assert candidate is not None
        assert candidate.thread_id is None


@pytest.mark.asyncio
async def test_the_code_lands_in_the_thread_it_was_asked_from() -> None:
    """A reply with no ``message_thread_id`` lands in none of a threaded chat's
    conversations, so the operator hunts for a code they never see."""
    fake = FakeTelegram()
    fake.deliver_on_poll(1, user_id=7, username="ada_l", thread_id=931)

    async with fake.client() as client:
        window = EnrollmentWindow()
        seen: list[Candidate] = []
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=seen.append,
            on_flood=lambda: None,
            should_stop=lambda: bool(seen),
            poll_timeout=1.0,
        )

    assert fake.threads == [931]


@pytest.mark.asyncio
async def test_a_chat_without_threads_is_sent_no_thread_id() -> None:
    fake = FakeTelegram()
    fake.deliver_on_poll(1, user_id=7, username="ada_l")

    async with fake.client() as client:
        window = EnrollmentWindow()
        seen: list[Candidate] = []
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=seen.append,
            on_flood=lambda: None,
            should_stop=lambda: bool(seen),
            poll_timeout=1.0,
        )

    assert fake.threads == [None]


class TestDeepLink:
    def test_the_matching_nonce_skips_the_code(self) -> None:
        window = EnrollmentWindow(nonce="s3cret")
        candidate = window.offer(_message(7, text="/start s3cret"))
        assert candidate is not None
        assert candidate.authenticated
        assert candidate.code is None
        assert window.authenticated_candidate() == candidate

    def test_a_wrong_nonce_falls_back_to_a_code(self) -> None:
        window = EnrollmentWindow(nonce="s3cret")
        candidate = window.offer(_message(7, text="/start guess"))
        assert candidate is not None
        assert not candidate.authenticated
        assert window.authenticated_candidate() is None

    def test_the_nonce_survives_a_botname_suffix(self) -> None:
        window = EnrollmentWindow(nonce="s3cret")
        candidate = window.offer(_message(7, text="/start@my_bot s3cret"))
        assert candidate is not None
        assert candidate.authenticated

    def test_the_link_carries_the_nonce(self) -> None:
        window = EnrollmentWindow(nonce="s3cret")
        assert window.deep_link("my_bot") == "https://t.me/my_bot?start=s3cret"

    def test_a_deep_link_candidate_is_spent_too(self) -> None:
        window = EnrollmentWindow(nonce="s3cret")
        candidate = window.offer(_message(7, text="/start s3cret"))
        assert candidate
        window.take(candidate)
        assert window.authenticated_candidate() is None


class TestExpiry:
    def test_expiry_invalidates_every_code_and_the_nonce(self) -> None:
        now = [0.0]
        window = EnrollmentWindow(
            nonce="s3cret", window_seconds=300.0, clock=lambda: now[0]
        )
        ada = window.offer(_message(7))
        linked = window.offer(_message(8, update_id=2, text="/start s3cret"))
        assert ada and linked

        now[0] = 301.0

        assert window.closed
        assert window.submit(ada.code) is None
        assert window.authenticated_candidate() is None
        assert window.offer(_message(9, update_id=9, text="/start s3cret")) is None


class TestLabel:
    def test_the_confirmation_names_the_person(self) -> None:
        candidate = Candidate(
            user_id=123456789,
            chat_id=123456789,
            thread_id=None,
            username="ada_l",
            first_name="Ada",
            code="431902",
        )
        assert candidate.label == "@ada_l (Ada, id 123456789)"

    def test_an_id_is_always_shown(self) -> None:
        candidate = Candidate(
            user_id=5, chat_id=5, thread_id=None, username=None, first_name=None, code="1"
        )
        assert candidate.label == "id 5"

    def test_the_code_message_names_no_product(self) -> None:
        text = enrollment.code_message("431902")
        assert "431 902" in text
        assert "shrimp" not in text.lower()


# ── The wire ──


@pytest.mark.asyncio
async def test_get_me_returns_the_username() -> None:
    fake = FakeTelegram(username="ada_bot")
    async with fake.client() as client:
        identity = await enrollment.get_me(client, TOKEN)
    assert identity.username == "ada_bot"


@pytest.mark.asyncio
async def test_a_bad_token_is_rejected_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TokenRejected):
            await enrollment.get_me(client, TOKEN)


@pytest.mark.asyncio
async def test_a_running_core_surfaces_as_a_conflict() -> None:
    fake = FakeTelegram()
    fake.conflict = True
    async with fake.client() as client:
        with pytest.raises(PollConflict):
            await enrollment.drain_backlog(client, TOKEN)


@pytest.mark.asyncio
async def test_the_backlog_is_drained_before_the_window_opens() -> None:
    """A message queued yesterday neither enrolls anyone nor receives a code."""
    fake = FakeTelegram()
    stale = fake.queue_message(user_id=666, username="stranger")

    async with fake.client() as client:
        offset = await enrollment.drain_backlog(client, TOKEN)
        assert offset == stale + 1

        window = EnrollmentWindow()
        fake.deliver_on_poll(1, user_id=7, username="ada_l")
        stop = [False]

        await enrollment.run_window(
            client,
            TOKEN,
            window,
            offset,
            on_candidate=lambda c: stop.__setitem__(0, True),
            on_flood=lambda: None,
            should_stop=lambda: stop[0],
            poll_timeout=1.0,
        )

    assert [c.user_id for c in window.candidates] == [7]
    assert [chat for chat, _ in fake.sent] == [7]
    assert fake.code_sent_to(7)


@pytest.mark.asyncio
async def test_a_fourth_candidate_is_never_written_to() -> None:
    fake = FakeTelegram()
    async with fake.client() as client:
        window = EnrollmentWindow()
        for i in range(1, 5):
            fake.deliver_on_poll(i, user_id=i, username=f"u{i}")

        seen = []
        floods = []
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=seen.append,
            on_flood=lambda: floods.append(True),
            should_stop=lambda: len(seen) + len(floods) >= 4,
            poll_timeout=1.0,
        )

    assert len(seen) == enrollment.MAX_CANDIDATES
    assert floods == [True]
    assert sorted(chat for chat, _ in fake.sent) == [1, 2, 3]


@pytest.mark.asyncio
async def test_confirming_the_offset_leaves_nothing_for_the_core_to_replay() -> None:
    fake = FakeTelegram()
    async with fake.client() as client:
        offset = await enrollment.drain_backlog(client, TOKEN)
        window = EnrollmentWindow()
        fake.deliver_on_poll(1, user_id=7)
        stop = [False]
        offset = await enrollment.run_window(
            client,
            TOKEN,
            window,
            offset,
            on_candidate=lambda c: stop.__setitem__(0, True),
            on_flood=lambda: None,
            should_stop=lambda: stop[0],
            poll_timeout=1.0,
        )
        await enrollment.confirm_offset(client, TOKEN, offset)

        # What the core's first poll would see.
        remaining, _ = await enrollment.poll_updates(client, TOKEN, offset, timeout=0)

    assert remaining == []
    assert fake.pending == []


# ── The poller ──


def test_the_poller_delivers_a_code_and_redeems_it() -> None:
    fake = FakeTelegram()
    window = EnrollmentWindow()
    seen: list[Candidate] = []

    poller = enrollment.EnrollmentPoller(
        TOKEN,
        window,
        on_candidate=seen.append,
        on_flood=lambda: None,
        client_factory=fake.client,
    )
    fake.deliver_on_poll(1, user_id=7, username="ada_l")
    poller.start()
    wait_for(lambda: bool(seen))

    assert len(seen) == 1
    # Redeemed before the poll is stopped: stopping ends the window with it.
    assert window.submit(fake.code_sent_to(7)) is not None
    poller.stop()


def test_a_stopped_poll_speaks_to_nobody() -> None:
    """Stopping ends the window with the thread.

    Otherwise a message landing in the gap between the wizard moving on and the
    thread noticing would earn a code from a window nobody is watching.
    """
    fake = FakeTelegram()
    window = EnrollmentWindow()
    poller = enrollment.EnrollmentPoller(
        TOKEN,
        window,
        on_candidate=lambda c: None,
        on_flood=lambda: None,
        client_factory=fake.client,
    )
    poller.start()
    wait_for(lambda: fake.polls >= 1)
    poller.stop()

    assert window.closed
    assert window.offer(make_update(7, update_id=99)) is None
    assert fake.sent == []


def test_a_dead_poll_still_leaves_a_confirmable_offset() -> None:
    """The offset the wizard confirms is what stops the core replaying the
    updates the wizard consumed, so it cannot be written only on a clean exit.
    """
    fake = FakeTelegram()
    fake.deliver_on_poll(1, user_id=7, username="ada_l")
    fake.conflict_from_poll = 2

    window = EnrollmentWindow()
    seen: list[Candidate] = []
    poller = enrollment.EnrollmentPoller(
        TOKEN,
        window,
        on_candidate=seen.append,
        on_flood=lambda: None,
        client_factory=fake.client,
    )
    poller.start()
    wait_for(lambda: poller.error is not None)
    poller.stop()

    assert seen
    assert poller.offset > 0


@pytest.mark.asyncio
async def test_a_window_that_runs_out_says_so_unprompted() -> None:
    """The one thing that happens to a surface rather than because of it.

    A caller blocked on ``input()`` cannot be interrupted, so an operator
    waiting for a code that will never arrive is left in front of a prompt that
    has quietly stopped meaning anything — the silence this flow exists to
    remove.
    """
    fake = FakeTelegram()
    now = [0.0]
    window = EnrollmentWindow(window_seconds=10.0, clock=lambda: now[0])
    closed: list[bool] = []

    async with fake.client() as client:
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=lambda c: None,
            on_flood=lambda: None,
            should_stop=lambda: False,
            on_close=lambda: closed.append(True),
            on_offset=lambda _: now.__setitem__(0, 11.0),
            poll_timeout=1.0,
        )

    assert closed == [True]


@pytest.mark.asyncio
async def test_a_window_closed_on_purpose_reports_nothing() -> None:
    """Stopping the poll closes the window too, and a successful enrollment
    must not announce itself as one that ran out."""
    fake = FakeTelegram()
    window = EnrollmentWindow()
    closed: list[bool] = []
    stopped = [False]

    def _stop(_offset: int) -> None:
        window.close()
        stopped[0] = True

    async with fake.client() as client:
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=lambda c: None,
            on_flood=lambda: None,
            should_stop=lambda: stopped[0],
            on_close=lambda: closed.append(True),
            on_offset=_stop,
            poll_timeout=1.0,
        )

    assert closed == []


@pytest.mark.asyncio
async def test_a_transient_failure_does_not_spend_the_window() -> None:
    """One 502, or one candidate who has blocked the bot, must not end the
    operator's five minutes."""
    fake = FakeTelegram()
    fake.poll_failures = {1}
    fake.send_failures = {8}
    fake.deliver_on_poll(2, user_id=8, username="blocked")
    fake.deliver_on_poll(3, user_id=7, username="ada_l")

    async with fake.client() as client:
        window = EnrollmentWindow()
        seen: list[Candidate] = []
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=seen.append,
            on_flood=lambda: None,
            should_stop=lambda: bool(seen),
            poll_timeout=1.0,
        )

    assert [c.user_id for c in seen] == [7]
    assert fake.code_sent_to(7)


def test_the_poller_reports_a_conflict_rather_than_raising() -> None:
    fake = FakeTelegram()
    fake.conflict = True
    poller = enrollment.EnrollmentPoller(
        TOKEN,
        EnrollmentWindow(),
        on_candidate=lambda c: None,
        on_flood=lambda: None,
        client_factory=fake.client,
    )
    poller.start()
    wait_for(lambda: poller.error is not None)
    poller.stop()

    assert isinstance(poller.error, PollConflict)


@pytest.mark.asyncio
async def test_nothing_is_sent_to_a_group_or_a_bot() -> None:
    fake = FakeTelegram()
    async with fake.client() as client:
        window = EnrollmentWindow()
        fake.deliver_on_poll(1, user_id=1, chat_type="supergroup")
        fake.deliver_on_poll(2, user_id=2, is_bot=True)
        fake.deliver_on_poll(3, user_id=3)

        seen = []
        await enrollment.run_window(
            client,
            TOKEN,
            window,
            0,
            on_candidate=seen.append,
            on_flood=lambda: None,
            should_stop=lambda: bool(seen),
            poll_timeout=1.0,
        )

    assert [c.user_id for c in seen] == [3]
    assert [chat for chat, _ in fake.sent] == [3]
