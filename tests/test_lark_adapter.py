"""Tests for the Lark inbound event adapter.

These must pass without lark-oapi installed: the mapping logic is pure
(dict-based) and the adapter defers all SDK object construction to start().
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

import open_shrimp.events.lark as lark_mod
from open_shrimp.config import EventSourceConfig
from open_shrimp.events.lark import (
    LarkAdapter,
    extract_text,
    map_message_event,
    sender_open_id,
)


def _payload(
    message_type: str = "text",
    content: str | None = '{"text":"hello world"}',
    event_id: str = "evt-abc123",
    open_id: str | None = "ou_deadbeef",
) -> dict[str, Any]:
    sender_id: dict[str, Any] = {}
    if open_id is not None:
        sender_id["open_id"] = open_id
    message: dict[str, Any] = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "message_type": message_type,
    }
    if content is not None:
        message["content"] = content
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "tk",
        },
        "event": {
            "sender": {"sender_id": sender_id, "sender_type": "user"},
            "message": message,
        },
    }


def _source(domain: str | None = None) -> EventSourceConfig:
    return EventSourceConfig(
        name="lark", type="lark", app_id="cli_x", app_secret="sec_y", domain=domain
    )


class _FakeSDK:
    LARK_DOMAIN = "https://open.larksuite.com"
    FEISHU_DOMAIN = "https://open.feishu.cn"


def test_domain_defaults_to_feishu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    assert adapter._resolve_domain() == _FakeSDK.FEISHU_DOMAIN


def test_domain_lark_resolves_to_international(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source(domain="lark"))
    assert adapter._resolve_domain() == _FakeSDK.LARK_DOMAIN


def test_domain_feishu_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source(domain="feishu"))
    assert adapter._resolve_domain() == _FakeSDK.FEISHU_DOMAIN


def test_construction_does_not_touch_sdk_domain_attrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # object() has no *_DOMAIN attributes; construction must not access them.
    monkeypatch.setattr(lark_mod, "lark_oapi", object())
    LarkAdapter(_source(domain="lark"))  # must not raise


def test_text_message_maps_to_event_text() -> None:
    event = map_message_event("lark", _payload())
    assert event.source == "lark"
    assert event.text == "hello world"
    assert event.raw is None
    assert event.dedup_key == "evt-abc123"


def test_mention_placeholders_replaced_with_names() -> None:
    payload = _payload(content='{"text":"@_user_1 ping @_user_2"}')
    payload["event"]["message"]["mentions"] = [
        {"key": "@_user_1", "name": "Alice", "id": {"open_id": "ou_a"}},
        {"key": "@_user_2", "name": "Bob", "id": {"open_id": "ou_b"}},
    ]
    event = map_message_event("lark", payload)
    assert event.text == "@Alice ping @Bob"


def test_mention_without_name_leaves_placeholder() -> None:
    payload = _payload(content='{"text":"@_user_1 hi"}')
    payload["event"]["message"]["mentions"] = [
        {"key": "@_user_1", "id": {"open_id": "ou_a"}},
        "garbage",
    ]
    event = map_message_event("lark", payload)
    assert event.text == "@_user_1 hi"


def test_mentions_absent_or_malformed_is_noop() -> None:
    assert map_message_event("lark", _payload()).text == "hello world"
    payload = _payload(content='{"text":"@_user_1 hi"}')
    payload["event"]["message"]["mentions"] = "not-a-list"
    assert map_message_event("lark", payload).text == "@_user_1 hi"


def test_dedup_key_is_event_id() -> None:
    event = map_message_event("lark", _payload(event_id="evt-42"))
    assert event.dedup_key == "evt-42"


def test_sender_falls_back_to_open_id() -> None:
    event = map_message_event("lark", _payload(open_id="ou_alice"))
    assert event.sender == "ou_alice"


def test_resolved_sender_name_wins() -> None:
    event = map_message_event("lark", _payload(), sender_name="Alice")
    assert event.sender == "Alice"


def test_non_text_message_uses_raw_fallback_with_type_tag() -> None:
    payload = _payload(message_type="post", content='{"title":"t"}')
    event = map_message_event("lark", payload, sender_name="Alice")
    assert event.text is None
    assert event.raw == payload
    assert event.sender == "Alice · [post]"


def test_non_text_message_without_sender_still_tags_type() -> None:
    payload = _payload(message_type="image", content='{"image_key":"k"}', open_id=None)
    event = map_message_event("lark", payload)
    assert event.text is None
    assert event.raw == payload
    assert event.sender == "[image]"


def test_malformed_text_content_falls_back_to_raw() -> None:
    payload = _payload(content="not-json")
    event = map_message_event("lark", payload)
    assert event.text is None
    assert event.raw == payload


def test_reply_ref_carries_message_id() -> None:
    event = map_message_event("lark", _payload())
    assert event.reply_ref == {"message_id": "om_1"}


def test_reply_ref_none_without_message_id() -> None:
    payload = _payload()
    del payload["event"]["message"]["message_id"]
    event = map_message_event("lark", payload)
    assert event.reply_ref is None


@pytest.mark.asyncio
async def test_reply_without_message_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    with pytest.raises(ValueError):
        await adapter.reply({}, "hi")


def test_send_reply_before_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    with pytest.raises(RuntimeError):
        adapter._send_reply("om_1", "hi")


def _fake_api_client(reply_fn: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=SimpleNamespace(reply=reply_fn))
        )
    )


def test_send_reply_builds_in_thread_text_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    sent: list[Any] = []

    def fake_reply(request: Any) -> Any:
        sent.append(request)

        class _Resp:
            @staticmethod
            def success() -> bool:
                return True

        return _Resp()

    adapter._api_client = _fake_api_client(fake_reply)

    adapter._send_reply("om_1", "hello 你好")

    [request] = sent
    assert request.message_id == "om_1"
    body = request.request_body
    assert body.msg_type == "text"
    assert body.reply_in_thread is True
    assert body.content == '{"text": "hello 你好"}'


def test_send_reply_failure_raises_with_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())

    class _Resp:
        code = 99991663
        msg = "token invalid"

        @staticmethod
        def success() -> bool:
            return False

    adapter._api_client = _fake_api_client(lambda r: _Resp())

    with pytest.raises(RuntimeError) as excinfo:
        adapter._send_reply("om_1", "hi")
    assert "99991663" in str(excinfo.value)


def test_extract_text_edge_cases() -> None:
    assert extract_text("text", '{"text":"hi"}') == "hi"
    assert extract_text("text", None) is None
    assert extract_text("text", '"just a string"') is None
    assert extract_text("text", '{"other":"x"}') is None
    assert extract_text("post", '{"text":"hi"}') is None
    assert extract_text(None, '{"text":"hi"}') is None


def test_sender_open_id_missing() -> None:
    assert sender_open_id(_payload(open_id=None)) is None
    assert sender_open_id({}) is None


def test_instantiation_without_dep_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", None)
    with pytest.raises(RuntimeError) as excinfo:
        LarkAdapter(_source())
    assert "uv sync --extra lark" in str(excinfo.value)
    assert "lark" in str(excinfo.value)


@pytest.mark.asyncio
async def test_on_message_hops_into_loop_and_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", object())  # dep sentinel
    adapter = LarkAdapter(_source())

    received: list[Any] = []
    done = asyncio.Event()

    async def emit(event: Any) -> None:
        received.append(event)
        done.set()

    adapter._emit = emit
    adapter._loop = asyncio.get_running_loop()
    monkeypatch.setattr(adapter, "_fetch_user_name", lambda open_id: "Bob")

    thread = threading.Thread(target=adapter._on_message, args=(_payload(),))
    thread.start()
    await asyncio.wait_for(done.wait(), timeout=5.0)
    thread.join(timeout=5.0)

    assert len(received) == 1
    event = received[0]
    assert event.source == "lark"
    assert event.text == "hello world"
    assert event.sender == "Bob"
    assert event.dedup_key == "evt-abc123"


@pytest.mark.asyncio
async def test_sender_resolution_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", object())
    adapter = LarkAdapter(_source())

    received: list[Any] = []

    async def emit(event: Any) -> None:
        received.append(event)

    adapter._emit = emit
    adapter._loop = asyncio.get_running_loop()

    def boom(open_id: str) -> str | None:
        raise ConnectionError("tenant scope denied")

    monkeypatch.setattr(adapter, "_fetch_user_name", boom)

    await adapter._deliver(_payload(open_id="ou_carol"))

    assert len(received) == 1
    assert received[0].sender == "ou_carol"


@pytest.mark.asyncio
async def test_emit_exception_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", object())
    adapter = LarkAdapter(_source())

    async def emit(event: Any) -> None:
        raise RuntimeError("sink exploded")

    adapter._emit = emit
    adapter._loop = asyncio.get_running_loop()
    monkeypatch.setattr(adapter, "_fetch_user_name", lambda open_id: None)

    await adapter._deliver(_payload())  # must not raise
