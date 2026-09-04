from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from tests.android_signing import android_headers, b64url
from tests.rich_stub import unwrap

from open_shrimp.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_shrimp.db import init_db
from open_shrimp.review.api import create_review_app

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ALLOWED_USER_ID = 111222333


def _make_config() -> Config:
    return Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory="/tmp/test-repo",
                description="Test context",
                model="claude-sonnet-4-6",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )


def _build_init_data() -> str:
    user_obj = json.dumps(
        {"id": ALLOWED_USER_ID, "first_name": "Test"}, separators=(",", ":")
    )
    params = {
        "auth_date": str(int(time.time())),
        "user": user_obj,
        "query_id": "AAHQ",
    }
    data_check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    params["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urlencode(params)


def _auth_header() -> dict[str, str]:
    return {"authorization": f"tg-init-data {_build_init_data()}"}


def _pair(client: TestClient, private_key: ec.EllipticCurvePrivateKey, device_id: str) -> None:
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    code = client.post(
        "/api/android-companion/pairing-codes", headers=_auth_header()
    ).json()["code"]
    client.post(
        "/api/android-companion/pair",
        json={
            "code": code,
            "device_id": device_id,
            "display_name": "Pixel",
            "public_key": b64url(public_key),
        },
    )


def _make_client(tmp_path: Path) -> tuple[TestClient, object]:
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    app = create_review_app(_make_config(), db)
    return TestClient(app), db


class _FakeFuture:
    """Minimal stand-in for asyncio.Future.

    The endpoint only touches ``done()`` and ``set_result()``; using a fake
    sidesteps the cross-event-loop issue where TestClient runs the app in its
    own loop (in production the HTTP server and bot share one loop).
    """

    def __init__(self) -> None:
        self.result_value: bool | None = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value: bool) -> None:
        self.result_value = value
        self._done = True


def test_android_approval_resolves_pending_future(tmp_path: Path) -> None:
    from open_shrimp.handlers.state import _approval_futures, _approval_resolved_via

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-approve-device"
    tool_use_id = "tool-abc"
    future = _FakeFuture()
    _approval_futures[f"approve:{tool_use_id}"] = future  # type: ignore[assignment]
    _approval_futures[f"deny:{tool_use_id}"] = future  # type: ignore[assignment]
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"approve"}'
        resp = client.post(
            path,
            content=body,
            headers={
                "content-type": "application/json",
                **android_headers(
                    private_key,
                    device_id=device_id,
                    method="POST",
                    path=path,
                    body=body,
                    nonce="nonce-approve",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "resolved", "decision": "approve"}
        assert future.result_value is True
        assert _approval_resolved_via.get(tool_use_id) == "android"
    finally:
        _approval_futures.pop(f"approve:{tool_use_id}", None)
        _approval_futures.pop(f"deny:{tool_use_id}", None)
        _approval_resolved_via.pop(tool_use_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_approval_resolves_host_escape_future(tmp_path: Path) -> None:
    from open_shrimp.handlers.state import _approval_futures, _approval_resolved_via

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-hostbash-device"
    tool_use_id = "tool-hb"
    future = _FakeFuture()
    # Host-escape prompts register under the ``hb_approve:``/``hb_deny:`` keys.
    _approval_futures[f"hb_approve:{tool_use_id}"] = future  # type: ignore[assignment]
    _approval_futures[f"hb_deny:{tool_use_id}"] = future  # type: ignore[assignment]
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"approve"}'
        resp = client.post(
            path,
            content=body,
            headers={
                "content-type": "application/json",
                **android_headers(
                    private_key,
                    device_id=device_id,
                    method="POST",
                    path=path,
                    body=body,
                    nonce="nonce-hostbash",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "resolved", "decision": "approve"}
        assert future.result_value is True
        # The host-escape flow edits its own message, so no phone marker is set.
        assert tool_use_id not in _approval_resolved_via
    finally:
        _approval_futures.pop(f"hb_approve:{tool_use_id}", None)
        _approval_futures.pop(f"hb_deny:{tool_use_id}", None)
        _approval_resolved_via.pop(tool_use_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_approval_resolves_config_write_future(tmp_path: Path) -> None:
    """A config-write card the phone can show, it must also be able to answer.

    The card's awaiting overlay is pushed exactly like every other
    approval's, and the phone knows only the ``tool_use_id`` — so a
    sender whose prefix this endpoint does not probe produces a tap that
    silently resolves nothing, in front of a card that by design has no
    deadline.
    """
    from open_shrimp.handlers.state import _approval_futures, _approval_resolved_via

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-configwrite-device"
    tool_use_id = "tool-cw"
    future = _FakeFuture()
    _approval_futures[f"cw_approve:{tool_use_id}"] = future  # type: ignore[assignment]
    _approval_futures[f"cw_deny:{tool_use_id}"] = future  # type: ignore[assignment]
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"approve"}'
        resp = client.post(
            path,
            content=body,
            headers={
                "content-type": "application/json",
                **android_headers(
                    private_key,
                    device_id=device_id,
                    method="POST",
                    path=path,
                    body=body,
                    nonce="nonce-configwrite",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "resolved", "decision": "approve"}
        assert future.result_value is True
        # Like host escape, this flow edits its own card, so no phone marker.
        assert tool_use_id not in _approval_resolved_via
    finally:
        _approval_futures.pop(f"cw_approve:{tool_use_id}", None)
        _approval_futures.pop(f"cw_deny:{tool_use_id}", None)
        _approval_resolved_via.pop(tool_use_id, None)
        client.close()
        asyncio.run(db.close())

def test_android_approval_noops_when_future_missing(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-expired-device"
    tool_use_id = "tool-gone"
    try:
        _pair(client, private_key, device_id)
        path = f"/api/agent/approvals/{tool_use_id}"
        body = b'{"decision":"deny"}'
        resp = client.post(
            path,
            content=body,
            headers={
                "content-type": "application/json",
                **android_headers(
                    private_key,
                    device_id=device_id,
                    method="POST",
                    path=path,
                    body=body,
                    nonce="nonce-expired",
                ),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "expired"}
    finally:
        client.close()
        asyncio.run(db.close())


def test_android_approval_rejects_unsigned_request(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        resp = client.post(
            "/api/agent/approvals/tool-x",
            json={"decision": "approve"},
        )
        assert resp.status_code == 401
    finally:
        client.close()
        asyncio.run(db.close())


# ---------------------------------------------------------------------------
# AskUserQuestion: the same Live Update answers a choice, by option position
# ---------------------------------------------------------------------------


class _RecordingBot:
    """Captures the card edit the phone's answer triggers."""

    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def do_api_request(self, endpoint, api_kwargs=None, **_: object) -> None:
        if endpoint == "editMessageText":
            self.edits.append(unwrap(api_kwargs or {}).__dict__)


def _register_question(
    question_id: str,
    *,
    options: list[dict[str, str]],
    multi_select: bool = False,
    bot: object = None,
    message_id: int | None = None,
) -> "_FakeFuture":
    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.state import _QuestionState, _question_states

    future = _FakeFuture()
    _question_states[question_id] = _QuestionState(
        question_id=question_id,
        scope=ChatScope(chat_id=-1001234, thread_id=77),
        options=options,  # type: ignore[arg-type]
        multi_select=multi_select,
        future=future,  # type: ignore[arg-type]
        bot=bot,
        message_id=message_id,
        original_text_md="Which one\\?",
    )
    return future


def _post_answer(
    client: TestClient,
    private_key: ec.EllipticCurvePrivateKey,
    device_id: str,
    question_id: str,
    payload: bytes,
    nonce: str,
) -> object:
    path = f"/api/agent/questions/{question_id}"
    return client.post(
        path,
        content=payload,
        headers={
            "content-type": "application/json",
            **android_headers(
                private_key,
                device_id=device_id,
                method="POST",
                path=path,
                body=payload,
                nonce=nonce,
            ),
        },
    )


def test_android_question_answers_by_option_index(tmp_path: Path) -> None:
    """The phone answers with a position, and the agent hears the label.

    Indexes are the contract precisely so the two surfaces cannot disagree:
    the label the phone rendered came out of the same list the host resolves
    against, however the push truncated it for the notification.
    """
    from open_shrimp.handlers.state import _question_states

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-question-device"
    question_id = "q-single"
    future = _register_question(
        question_id,
        options=[{"label": "Merge"}, {"label": "Rebase"}],
    )
    try:
        _pair(client, private_key, device_id)
        resp = _post_answer(
            client, private_key, device_id, question_id,
            b'{"option_indexes":[1],"other_texts":[]}', "nonce-q-single",
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "resolved", "answer": "Rebase"}
        assert future.result_value == "Rebase"
    finally:
        _question_states.pop(question_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_question_multi_select_joins_options_and_free_text(
    tmp_path: Path,
) -> None:
    from open_shrimp.handlers.state import _question_states

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-question-multi"
    question_id = "q-multi"
    future = _register_question(
        question_id,
        options=[{"label": "Tests"}, {"label": "Docs"}, {"label": "Types"}],
        multi_select=True,
    )
    try:
        _pair(client, private_key, device_id)
        resp = _post_answer(
            client, private_key, device_id, question_id,
            b'{"option_indexes":[2,0],"other_texts":["and a changelog"," "]}',
            "nonce-q-multi",
        )
        assert resp.status_code == 200
        # Sorted by position, then whatever was typed; blank entries dropped.
        assert future.result_value == "Tests, Types, and a changelog"
    finally:
        _question_states.pop(question_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_question_closes_the_telegram_card(tmp_path: Path) -> None:
    """An answer over HTTP has no CallbackQuery, so it must retire the card.

    Left alone, the Telegram card keeps offering buttons for a future that
    is already resolved, and the next tap reports the question expired.
    """
    from open_shrimp.handlers.state import _question_states

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-question-card"
    question_id = "q-card"
    bot = _RecordingBot()
    _register_question(
        question_id,
        options=[{"label": "Merge"}],
        bot=bot,
        message_id=4242,
    )
    try:
        _pair(client, private_key, device_id)
        resp = _post_answer(
            client, private_key, device_id, question_id,
            b'{"option_indexes":[0],"other_texts":[]}', "nonce-q-card",
        )
        assert resp.status_code == 200
        assert len(bot.edits) == 1
        edit = bot.edits[0]
        assert edit["chat_id"] == -1001234
        assert edit["message_id"] == 4242
        assert edit["reply_markup"] is None
        assert "Merge" in str(edit["text"])
    finally:
        _question_states.pop(question_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_question_expired_when_answered_in_telegram_first(
    tmp_path: Path,
) -> None:
    from open_shrimp.handlers.state import _question_states

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-question-late"
    question_id = "q-late"
    future = _register_question(question_id, options=[{"label": "Merge"}])
    future.set_result("Merge")
    try:
        _pair(client, private_key, device_id)
        resp = _post_answer(
            client, private_key, device_id, question_id,
            b'{"option_indexes":[0],"other_texts":[]}', "nonce-q-late",
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "expired"}
    finally:
        _question_states.pop(question_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_question_rejects_boolean_option_index(tmp_path: Path) -> None:
    """JSON ``true`` is an int in Python and would silently select option 1."""
    from open_shrimp.handlers.state import _question_states

    client, db = _make_client(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    device_id = "android-question-bool"
    question_id = "q-bool"
    future = _register_question(
        question_id, options=[{"label": "Merge"}, {"label": "Rebase"}],
    )
    try:
        _pair(client, private_key, device_id)
        resp = _post_answer(
            client, private_key, device_id, question_id,
            b'{"option_indexes":[true],"other_texts":[]}', "nonce-q-bool",
        )
        assert resp.status_code == 400
        assert future.result_value is None
    finally:
        _question_states.pop(question_id, None)
        client.close()
        asyncio.run(db.close())


def test_android_question_rejects_unsigned_request(tmp_path: Path) -> None:
    client, db = _make_client(tmp_path)
    try:
        resp = client.post(
            "/api/agent/questions/q-x",
            json={"option_indexes": [0], "other_texts": []},
        )
        assert resp.status_code == 401
    finally:
        client.close()
        asyncio.run(db.close())
