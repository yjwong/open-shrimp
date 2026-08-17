"""A Bot API stand-in with real ``getUpdates`` offset semantics.

Shared by the enrollment tests and the wizard tests, because both need the
part that matters: an update queued before the window is drained away, and
nothing the wizard consumed is left for the core to replay.
"""

from __future__ import annotations

import json
import re
import threading
import time

import httpx


def make_update(
    user_id: int,
    *,
    update_id: int = 1,
    text: str = "/start",
    chat_type: str = "private",
    is_bot: bool = False,
    thread_id: int | None = None,
    username: str | None = "ada_l",
    first_name: str | None = "Ada",
) -> dict:
    """One raw Bot API update, in the shape ``EnrollmentWindow.offer`` filters.

    One builder, because a window unit test asserting against a wire shape the
    stub no longer produces would go on passing against a stale envelope — and
    policing that envelope is the whole job of the filters it exercises.
    """
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            **({"message_thread_id": thread_id} if thread_id is not None else {}),
            "chat": {"id": user_id, "type": chat_type},
            "from": {
                "id": user_id,
                "is_bot": is_bot,
                "username": username,
                "first_name": first_name,
            },
        },
    }


def wait_for(predicate, timeout: float = 5.0) -> None:
    """Block until a background thread has done something, or give up."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)


class FakeTelegram:
    """A fake Bot API endpoint reachable through :meth:`client`."""

    def __init__(self, *, username: str = "my_bot", bot_id: int = 99) -> None:
        self.username = username
        self.bot_id = bot_id
        self.pending: list[dict] = []
        self.sent: list[tuple[int, str]] = []
        # The message_thread_id each send carried, positionally alongside `sent`.
        self.threads: list[int | None] = []
        self.conflict = False
        self.bad_token = False
        # Poll numbers that answer 502 instead, and chat ids sendMessage
        # refuses — the transients a five-minute window has to survive.
        self.poll_failures: set[int] = set()
        self.send_failures: set[int] = set()
        # The poll number from which a rival client owns the token.
        self.conflict_from_poll: int | None = None
        self.polls = 0
        self._next_id = 1
        self._lock = threading.Lock()
        # Updates to append the Nth time getUpdates long-polls, so a test can
        # deliver a message strictly after the backlog drain.
        self.inject_on_poll: dict[int, list[dict]] = {}

    # -- queueing --

    def queue_message(self, **kwargs) -> int:
        with self._lock:
            return self._queue_locked(kwargs)

    def deliver_on_poll(self, poll_number: int, **kwargs) -> None:
        self.inject_on_poll.setdefault(poll_number, []).append(kwargs)

    # -- transport --

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")

        if self.bad_token:
            return httpx.Response(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        if method == "getMe":
            return self._ok({"id": self.bot_id, "username": self.username})

        if method == "sendMessage":
            if params["chat_id"] in self.send_failures:
                return httpx.Response(
                    403,
                    json={
                        "ok": False,
                        "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user",
                    },
                )
            self.threads.append(params.get("message_thread_id"))
            self.sent.append((params["chat_id"], params["text"]))
            return self._ok({"message_id": 1})

        if method == "getUpdates":
            if self.conflict:
                return httpx.Response(
                    409,
                    json={
                        "ok": False,
                        "error_code": 409,
                        "description": "Conflict: terminated by other getUpdates request",
                    },
                )
            return self._get_updates(params)

        return self._ok(True)

    def _get_updates(self, params: dict) -> httpx.Response:
        offset = params.get("offset", 0)
        with self._lock:
            if offset == -1:
                return self._ok(self.pending[-1:])

            if offset > 0:
                self.pending = [u for u in self.pending if u["update_id"] >= offset]

            self.polls += 1
            if (
                self.conflict_from_poll is not None
                and self.polls >= self.conflict_from_poll
            ):
                return httpx.Response(
                    409,
                    json={
                        "ok": False,
                        "error_code": 409,
                        "description": "Conflict: terminated by other getUpdates request",
                    },
                )
            if self.polls in self.poll_failures:
                return httpx.Response(
                    502,
                    json={"ok": False, "error_code": 502, "description": "Bad Gateway"},
                )

            for spec in self.inject_on_poll.pop(self.polls, []):
                self._queue_locked(spec)

            return self._ok(list(self.pending))

    def _queue_locked(self, kwargs: dict) -> int:
        update = make_update(update_id=self._next_id, **kwargs)
        self._next_id += 1
        self.pending.append(update)
        return update["update_id"]

    @staticmethod
    def _ok(result) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    # -- assertions --

    def code_sent_to(self, chat_id: int) -> str:
        for target, text in self.sent:
            if target == chat_id and "Setup code" in text:
                return re.sub(r"\D", "", text.split("\n")[0])
        raise AssertionError(f"no setup code was sent to {chat_id}: {self.sent}")
