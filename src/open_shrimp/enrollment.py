"""Authenticated enrollment handshake for the setup wizard.

The wizard writes the first entry in ``allowed_users``, and that list is the
product's only authentication boundary in front of a bot that runs shell
commands and edits files.  Enrollment is therefore an authentication step:
whoever is written in has to *prove* they are the operator sitting at the
wizard, not merely be the first message the bot happened to receive.

The proof travels phone -> desktop.  The bot sends a six-digit code to the
chat that messaged it; the operator types that code into the wizard.  Nothing
has to reach the phone except a message the bot can already send, and the only
typing happens on a desktop keyboard — which is what makes this work over ssh
with no rendering at all.  A deep-link nonce is kept as an accelerator for the
case where Telegram Desktop is on the wizard's own machine.

Two invariants this module exists to hold:

* Nothing that predates the window can ever enroll.  The backlog is drained to
  the highest queued ``update_id`` before a single code is issued, so a message
  from yesterday is neither a candidate nor a recipient.
* The bot speaks to a non-allowlisted user in exactly one circumstance: an open
  enrollment window, capped at three candidates, six digits and one sentence,
  naming no product.  Anything that widens that is a fingerprint for whoever is
  sweeping the username namespace.

The Bot API is spoken to over raw ``httpx`` here, and not through
python-telegram-bot as everything else in the core does.  Two reasons, both
load-bearing.  :meth:`EnrollmentWindow.offer` decides who may enroll from the
update exactly as Telegram serialised it, which is what lets the macOS and
Windows wizards run character-for-character the same filter against the same
JSON — a library's parsed objects would make three surfaces agree only by
inspection.  And ``httpx``'s transport is one seam, so a test drives the whole
handshake — drain, poll, code delivery, offset confirmation — with real
``getUpdates`` offset semantics rather than a mocked client.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# A wizard left open on a desk overnight must not still be enrollable in the
# morning.
WINDOW_SECONDS = 300.0

# A flood is a signal worth surfacing rather than an error to swallow, so the
# cap is low enough that the operator notices it.
MAX_CANDIDATES = 3

# Wrong entries close the window rather than looping forever.
MAX_WRONG_CODES = 5

# Long-poll duration.  Telegram holds the request open this long when the queue
# is empty, so the loop costs one request per interval rather than one per
# second.
POLL_TIMEOUT = 25.0

# The code is retyped by a human reading it off a phone, so it is six digits
# grouped for legibility rather than the base64url a paste-only flow would use.
_CODE_DIGITS = 6

_CODE_MESSAGE = "Setup code: {code}\nType this into the setup window on your computer."

# The wizard's last word in Telegram.  Orientation belongs on first boot, when
# the bot can actually be acted on.
ALL_SET_MESSAGE = "You're all set. I'll message you here when the bot starts."


class EnrollmentError(Exception):
    """Any failure of the enrollment handshake."""


class TokenRejected(EnrollmentError):
    """Telegram refused the token — it is wrong, revoked, or mistyped."""


class PollConflict(EnrollmentError):
    """Another client already owns ``getUpdates`` for this token.

    Two clients polling one token get HTTP 409, so this means a core is
    running and has to be stopped before the wizard can hold the poll.
    """


class TelegramUnreachable(EnrollmentError):
    """The network, not Telegram, said no."""


@dataclass(frozen=True)
class BotIdentity:
    """What ``getMe`` tells us about the token's bot."""

    user_id: int
    username: str | None

    @property
    def link(self) -> str:
        return f"https://t.me/{self.username}" if self.username else ""


@dataclass(frozen=True)
class Candidate:
    """Somebody who messaged the bot inside the enrollment window.

    ``code`` is ``None`` for a candidate that arrived carrying the deep-link
    nonce: it has already proven it came from the wizard's own screen, so
    there is nothing left for a code to prove.

    ``thread_id`` is carried because a chat with Threaded Mode on is many
    conversations, and a reply that omits it lands in none of them — the
    operator is left hunting for a code in a chat they are not looking at.
    """

    user_id: int
    chat_id: int
    thread_id: int | None
    username: str | None
    first_name: str | None
    code: str | None

    @property
    def authenticated(self) -> bool:
        return self.code is None

    @property
    def label(self) -> str:
        """How the confirmation screen names this person."""
        name = self.first_name or ""
        if self.username and name:
            return f"@{self.username} ({name}, id {self.user_id})"
        if self.username:
            return f"@{self.username} (id {self.user_id})"
        if name:
            return f"{name} (id {self.user_id})"
        return f"id {self.user_id}"


def grouped_code(code: str) -> str:
    """Group a code for reading aloud off a screen: ``431902`` -> ``431 902``."""
    half = len(code) // 2
    return f"{code[:half]} {code[half:]}"


def code_message(code: str) -> str:
    """The one sentence the bot sends a candidate.

    Names no product: a stranger who pokes the bot during a window learns only
    that something asked them for a code, not that a machine with an OpenShrimp
    on it is behind the username.
    """
    return _CODE_MESSAGE.format(code=grouped_code(code))


def _start_payload(text: str) -> str | None:
    """The deep-link payload from a ``/start`` message, if it carries one."""
    parts = text.split(maxsplit=1)
    if not parts or not parts[0].startswith("/start"):
        return None
    return parts[1].strip() if len(parts) > 1 else None


class EnrollmentWindow:
    """The bounded window in which a candidate may be enrolled.

    Decides who is a candidate, allocates codes, and validates entries.  It
    sends nothing itself, so it is the same object on every surface and is
    testable without a network.  Its mutations take a lock because the poll
    runs on one thread while the operator types a code on another.
    """

    def __init__(
        self,
        *,
        nonce: str | None = None,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # 22 characters, inside Telegram's 1-64 character [A-Za-z0-9_-]
        # deep-link payload budget.
        self.nonce = nonce or secrets.token_urlsafe(16)
        self.wrong_attempts = 0
        self.flooded = False
        self._clock = clock
        self._deadline = clock() + window_seconds
        self._pending: list[Candidate] = []
        # Everyone this window has ever spoken to.  The cap is on codes issued,
        # not on codes outstanding: without this a declined stranger could ask
        # again and again and the "capped at three" invariant would only ever
        # have bounded how many were in flight at once.
        self._spoken_to: set[int] = set()
        self._closed = False
        self._lock = threading.RLock()

    @property
    def expired(self) -> bool:
        return self._clock() >= self._deadline

    @property
    def closed(self) -> bool:
        """Expiry invalidates every outstanding code and the nonce alike."""
        return self._closed or self.expired

    @property
    def seconds_left(self) -> float:
        return max(0.0, self._deadline - self._clock())

    @property
    def candidates(self) -> list[Candidate]:
        with self._lock:
            return list(self._pending)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def deep_link(self, username: str) -> str:
        return f"https://t.me/{username}?start={self.nonce}"

    def offer(self, update: dict[str, Any]) -> Candidate | None:
        """Consider one raw update.

        Returns the new candidate when the update produced one, and ``None``
        for everything else — a filtered update, a repeat from somebody already
        pending, a flood past the cap, or a closed window.  A caller replies
        with the code only when a candidate with a code comes back, which is
        what keeps the bot silent toward everyone else.
        """
        with self._lock:
            if self.closed:
                return None

            message = update.get("message")
            if not isinstance(message, dict):
                # Deliberately not edited_message or channel_post: only a fresh
                # private message from a human is a candidate.
                return None

            chat = message.get("chat") or {}
            if chat.get("type") != "private":
                return None

            sender = message.get("from") or {}
            user_id = sender.get("id")
            if not isinstance(user_id, int) or sender.get("is_bot"):
                return None

            # Somebody already holding a live code gets nothing for messaging
            # twice; a second code would only be a second thing to mistype.
            if any(c.user_id == user_id for c in self._pending):
                return None

            # But somebody the operator *declined* may ask again.  What the cap
            # bounds is how many distinct strangers the bot ever speaks to, and
            # re-issuing to one already in that set widens it by nobody — while
            # refusing them makes "No, try again" a dead end for the operator
            # who mis-tapped it.
            returning = user_id in self._spoken_to
            if not returning and len(self._spoken_to) >= MAX_CANDIDATES:
                self.flooded = True
                return None

            payload = _start_payload(message.get("text") or "")
            authenticated = payload is not None and secrets.compare_digest(
                payload, self.nonce
            )
            thread_id = message.get("message_thread_id")
            candidate = Candidate(
                user_id=user_id,
                chat_id=chat.get("id", user_id),
                thread_id=thread_id if isinstance(thread_id, int) else None,
                username=sender.get("username"),
                first_name=sender.get("first_name"),
                code=None if authenticated else self._allocate_code(),
            )
            self._pending.append(candidate)
            self._spoken_to.add(user_id)
            return candidate

    def authenticated_candidate(self) -> Candidate | None:
        """The candidate that arrived by deep link, if one has.

        The most recent one, because that is the one the surface just named:
        picking the oldest would confirm a different person from the one the
        operator was told about.
        """
        with self._lock:
            if self.closed:
                return None
            for candidate in reversed(self._pending):
                if candidate.authenticated:
                    return candidate
            return None

    def submit(self, entered: str) -> Candidate | None:
        """Redeem a typed code.

        A code is single-use: the candidate leaves the pending list whether or
        not the operator goes on to confirm, so replaying it enrolls nobody.
        """
        with self._lock:
            if self.closed:
                return None

            # ASCII digits only.  ``str.isdigit`` is true for Arabic-Indic and
            # superscript digits, which ``compare_digest`` then refuses to
            # compare at all — a TypeError out of the code prompt.
            digits = "".join(ch for ch in entered if ch in "0123456789")
            for candidate in self._pending:
                if candidate.code and secrets.compare_digest(candidate.code, digits):
                    self._pending.remove(candidate)
                    return candidate

            self.wrong_attempts += 1
            if self.wrong_attempts >= MAX_WRONG_CODES:
                self.close()
            return None

    def take(self, candidate: Candidate) -> None:
        """Spend a deep-link candidate, so it too cannot be redeemed twice."""
        with self._lock:
            if candidate in self._pending:
                self._pending.remove(candidate)

    def _allocate_code(self) -> str:
        taken = {c.code for c in self._pending}
        while True:
            code = f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"
            if code not in taken:
                return code


async def _call(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    http_timeout: float = 15.0,
) -> Any:
    """Call one Bot API method, mapping failures onto this module's errors.

    The HTTP timeout is a separate argument from the request body because
    ``getUpdates`` takes a ``timeout`` of its own, and a long poll needs the
    transport to outlive it.
    """
    url = f"{API_BASE}/bot{token}/{method}"
    try:
        response = await client.post(url, json=params or {}, timeout=http_timeout)
    except httpx.HTTPError as exc:
        raise TelegramUnreachable(str(exc)) from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code == 409:
        raise PollConflict(body.get("description", "Conflict"))
    if response.status_code in (401, 404):
        raise TokenRejected(body.get("description", "Unauthorized"))
    if not body.get("ok"):
        raise EnrollmentError(body.get("description") or f"HTTP {response.status_code}")
    return body.get("result")


async def get_me(client: httpx.AsyncClient, token: str) -> BotIdentity:
    """Verify the token and learn the bot's username."""
    result = await _call(client, token, "getMe")
    return BotIdentity(user_id=result["id"], username=result.get("username"))


async def drain_backlog(client: httpx.AsyncClient, token: str) -> int:
    """Return the offset the window should start from.

    Telegram queues updates for up to 24 hours, so without this the "first
    message that arrives" may have arrived yesterday from somebody else.
    ``offset=-1`` reads the highest queued update without confirming anything;
    polling from one past it skips the whole backlog.
    """
    result = await _call(client, token, "getUpdates", {"offset": -1, "limit": 1})
    if not result:
        return 0
    return int(result[-1]["update_id"]) + 1


async def poll_updates(
    client: httpx.AsyncClient,
    token: str,
    offset: int,
    *,
    timeout: float = POLL_TIMEOUT,
) -> tuple[list[dict[str, Any]], int]:
    """Long-poll one batch, returning the updates and the next offset."""
    result = await _call(
        client,
        token,
        "getUpdates",
        {"offset": offset, "timeout": int(timeout)},
        http_timeout=timeout + 10.0,
    )
    updates = list(result or [])
    if updates:
        offset = int(updates[-1]["update_id"]) + 1
    return updates, offset


async def send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
) -> None:
    """Reply in the conversation the message came from.

    A chat with Threaded Mode on is many conversations, and Telegram puts a
    reply with no ``message_thread_id`` in none of them.  Sent only when the
    incoming message carried one, so a chat without threads is unaffected.
    """
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        params["message_thread_id"] = thread_id
    await _call(client, token, "sendMessage", params)


async def confirm_offset(client: httpx.AsyncClient, token: str, offset: int) -> None:
    """Confirm every update the wizard consumed.

    Without this the core's first ``getUpdates`` replays them: the enrolled
    user's ``/start`` fires the greeting a second time and every other
    candidate's message is re-delivered to a bot that now has an allowlist.
    """
    if offset <= 0:
        return
    try:
        await _call(client, token, "getUpdates", {"offset": offset, "limit": 1})
    except EnrollmentError:
        logger.warning("Could not confirm the enrollment offset", exc_info=True)


async def run_window(
    client: httpx.AsyncClient,
    token: str,
    window: EnrollmentWindow,
    offset: int,
    *,
    on_candidate: Callable[[Candidate], None],
    on_flood: Callable[[], None],
    should_stop: Callable[[], bool],
    on_close: Callable[[], None] = lambda: None,
    on_offset: Callable[[int], None] = lambda _: None,
    poll_timeout: float = POLL_TIMEOUT,
) -> int:
    """Poll for candidates until the window closes, returning the next offset.

    Sends a code to each candidate that earns one and to nobody else.  The
    callbacks are the surface's: they draw, they do not decide.

    ``on_offset`` fires after every batch rather than only on return, because a
    caller that never learns the offset confirms nothing, and the updates the
    wizard consumed then replay into the core on its first poll.

    ``on_close`` fires when the window runs out, and only then.  Expiry is the
    one thing that happens to a surface rather than because of it: a caller
    blocked on a code it is never going to get would otherwise sit in front of
    a dead prompt, which is the silence this whole flow exists to remove.

    Transient failures do not end the window.  A candidate who has blocked the
    bot, or one 502 from the API, must not spend the operator's five minutes —
    only a refused token or a conflicting poller is fatal, because neither
    resolves by waiting.
    """
    while not window.closed and not should_stop():
        # Never park past the deadline, and never spin: a poll shorter than a
        # second would busy-loop through the last fraction of the window.
        wait = max(1.0, min(poll_timeout, window.seconds_left))
        try:
            updates, offset = await poll_updates(client, token, offset, timeout=wait)
        except (TokenRejected, PollConflict):
            raise
        except EnrollmentError:
            logger.warning("Enrollment poll failed; retrying", exc_info=True)
            await asyncio.sleep(1.0)
            continue
        on_offset(offset)

        for update in updates:
            was_flooded = window.flooded
            candidate = window.offer(update)
            if candidate is None:
                if window.flooded and not was_flooded:
                    on_flood()
                continue
            if candidate.code:
                try:
                    await send_message(
                        client,
                        token,
                        candidate.chat_id,
                        code_message(candidate.code),
                        candidate.thread_id,
                    )
                except EnrollmentError:
                    # Blocked the bot, deleted the chat, or a transient 5xx.
                    # Their problem, not the window's.
                    logger.warning("Could not send a setup code", exc_info=True)
                    continue
            on_candidate(candidate)

    # Not when the caller asked to stop, which also closes the window: a
    # successful enrollment must not report itself as one that ran out.
    if window.closed and not should_stop():
        on_close()
    return offset


class EnrollmentPoller:
    """Runs :func:`run_window` on a background thread.

    The CLI wizard blocks on ``input()`` waiting for the code, and the poll has
    to keep running underneath it, so the loop gets a thread of its own rather
    than the whole wizard turning async around a blocking read.

    It owns the run's lifetime and nothing else: who may enroll is asked of the
    window directly, which locks its own mutations, so forwarding methods here
    would only move those rules away from the one class that states them.
    """

    def __init__(
        self,
        token: str,
        window: EnrollmentWindow,
        *,
        on_candidate: Callable[[Candidate], None],
        on_flood: Callable[[], None],
        on_close: Callable[[], None] = lambda: None,
        offset: int = 0,
        client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._token = token
        self._window = window
        self._on_candidate = on_candidate
        self._on_flood = on_flood
        self._on_close = on_close
        self._client_factory = client_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self.offset = offset
        self.error: EnrollmentError | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="enrollment-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # Closed before the join, not merely signalled: until the thread is
        # actually gone a batch could still land, and a window still open when
        # it does would issue a code to somebody the wizard has stopped
        # listening for.
        self._window.close()
        self._stop.set()
        if self._thread is None:
            return

        # The stop flag is only read between batches, so a poll parked on an
        # empty queue would otherwise hold the wizard for the rest of its
        # timeout — a blank screen at the moment enrollment succeeds.
        # Cancelling aborts the request instead; the offset is already recorded
        # per batch, so nothing consumed is lost with it.
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The poll had already finished and closed its loop, which is
                # the outcome the cancel was asking for.
                pass
        self._thread.join(timeout=POLL_TIMEOUT + 15.0)
        self._thread = None

    def _run(self) -> None:
        try:
            asyncio.run(self._poll())
        except asyncio.CancelledError:
            pass  # stop() asked, which is not a failure.
        except EnrollmentError as exc:
            self.error = exc
        except Exception:
            logger.exception("The enrollment poll failed")
            self.error = EnrollmentError("The enrollment poll stopped unexpectedly.")

    async def _poll(self) -> None:
        # Published so ``stop`` can reach into this thread's loop from the one
        # the wizard prompts on.
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        async with self._client_factory() as client:
            self.offset = await run_window(
                client,
                self._token,
                self._window,
                self.offset,
                on_candidate=self._on_candidate,
                on_flood=self._on_flood,
                should_stop=self._stop.is_set,
                on_close=self._on_close,
                on_offset=self._record_offset,
            )

    def _record_offset(self, offset: int) -> None:
        # Written as each batch lands, so a poll that dies later still leaves
        # behind an offset the wizard can confirm.
        self.offset = offset
