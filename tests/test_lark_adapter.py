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
    chat_type: str = "p2p",
    mentions: list[Any] | None = None,
) -> dict[str, Any]:
    sender_id: dict[str, Any] = {}
    if open_id is not None:
        sender_id["open_id"] = open_id
    message: dict[str, Any] = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": chat_type,
        "message_type": message_type,
    }
    if content is not None:
        message["content"] = content
    if mentions is not None:
        message["mentions"] = mentions
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


def _source(
    domain: str | None = None, require_mention: bool = False
) -> EventSourceConfig:
    return EventSourceConfig(
        name="lark",
        type="lark",
        app_id="cli_x",
        app_secret="sec_y",
        domain=domain,
        require_mention=require_mention,
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


def test_context_ref_thread_id_none_when_not_threaded() -> None:
    event = map_message_event("lark", _payload())
    assert event.context_ref == {
        "chat_id": "oc_1",
        "thread_id": None,
        "anchor_message_id": "om_1",
    }


def test_context_ref_uses_thread_id_field_not_root_id() -> None:
    # Lark carries the real thread container as thread_id (omt_…); root_id is
    # an om_… message id and must not be used as a thread container.
    payload = _payload()
    payload["event"]["message"]["root_id"] = "om_root"
    payload["event"]["message"]["thread_id"] = "omt_193518774b4f9983"
    event = map_message_event("lark", payload)
    assert event.context_ref == {
        "chat_id": "oc_1",
        "thread_id": "omt_193518774b4f9983",
        "anchor_message_id": "om_1",
    }


def test_context_ref_none_without_chat_id() -> None:
    payload = _payload()
    del payload["event"]["message"]["chat_id"]
    event = map_message_event("lark", payload)
    assert event.context_ref is None


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


def _listed_msg(
    text: str | None, open_id: str | None, message_id: str, msg_type: str = "text"
) -> Any:
    from types import SimpleNamespace

    content = f'{{"text":{__import__("json").dumps(text)}}}' if text is not None else None
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        body=SimpleNamespace(content=content),
        sender=SimpleNamespace(id=open_id),
    )


def _fake_list_client(list_fn: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=SimpleNamespace(list=list_fn))
        )
    )


def _list_response(items: list[Any], success: bool = True) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        success=lambda: success,
        code=0,
        msg="ok",
        data=SimpleNamespace(items=items),
    )


@pytest.mark.asyncio
async def test_fetch_context_renders_thread_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    monkeypatch.setattr(adapter, "_fetch_user_name", lambda oid: {"ou_a": "Alice", "ou_b": "Bob"}.get(oid))
    calls: list[Any] = []

    # API returns newest-first; the adapter reverses to oldest-first.
    def fake_list(request: Any) -> Any:
        calls.append(request)
        return _list_response([
            _listed_msg("second", "ou_b", "om_2"),
            _listed_msg("first", "ou_a", "om_1"),
        ])

    adapter._api_client = _fake_list_client(fake_list)

    out = await adapter.fetch_context(
        {"chat_id": "oc_1", "thread_id": "omt_thread", "anchor_message_id": "om_2"}
    )

    assert out == "Alice: first\nBob (event message): second"
    # A real thread id routes to the thread container.
    assert calls[0].container_id_type == "thread"
    assert calls[0].container_id == "omt_thread"


@pytest.mark.asyncio
async def test_fetch_context_falls_back_to_chat_when_no_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    monkeypatch.setattr(adapter, "_fetch_user_name", lambda oid: None)
    calls: list[Any] = []

    def fake_list(request: Any) -> Any:
        calls.append(request)
        return _list_response([_listed_msg("hi", "ou_x", "om_9")])

    adapter._api_client = _fake_list_client(fake_list)

    # thread_id None means the message is not in a thread.
    out = await adapter.fetch_context(
        {"chat_id": "oc_1", "thread_id": None, "anchor_message_id": "om_9"}
    )

    assert out == "ou_x (event message): hi"
    assert calls[0].container_id_type == "chat"
    assert calls[0].container_id == "oc_1"


@pytest.mark.asyncio
async def test_fetch_context_no_chat_fallback_when_thread_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A threaded message whose thread lookup returns nothing must degrade to
    # None, never spill into unrelated whole-chat history.
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    calls: list[Any] = []

    def fake_list(request: Any) -> Any:
        calls.append(request)
        return _list_response([])

    adapter._api_client = _fake_list_client(fake_list)

    out = await adapter.fetch_context(
        {"chat_id": "oc_1", "thread_id": "omt_thread", "anchor_message_id": "om_2"}
    )

    assert out is None
    assert [c.container_id_type for c in calls] == ["thread"]


@pytest.mark.asyncio
async def test_fetch_context_none_when_no_text_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    adapter._api_client = _fake_list_client(
        lambda r: _list_response([_listed_msg(None, "ou_x", "om_1", msg_type="image")])
    )

    out = await adapter.fetch_context(
        {"chat_id": "oc_1", "thread_id": "omt_1", "anchor_message_id": "om_1"}
    )
    assert out is None


@pytest.mark.asyncio
async def test_fetch_context_none_when_list_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lark_oapi")
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    adapter._api_client = _fake_list_client(
        lambda r: _list_response([], success=False)
    )

    out = await adapter.fetch_context(
        {"chat_id": "oc_1", "thread_id": "omt_1", "anchor_message_id": "om_1"}
    )
    assert out is None


@pytest.mark.asyncio
async def test_fetch_context_none_without_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", _FakeSDK())
    adapter = LarkAdapter(_source())
    assert await adapter.fetch_context({"thread_id": "om_1"}) is None


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


def _mention_adapter(
    monkeypatch: pytest.MonkeyPatch, bot_open_id: str | None = "ou_bot"
) -> LarkAdapter:
    monkeypatch.setattr(lark_mod, "lark_oapi", object())
    adapter = LarkAdapter(_source(require_mention=True))
    adapter._loop = asyncio.get_running_loop()
    monkeypatch.setattr(adapter, "_fetch_bot_open_id", lambda: bot_open_id)
    monkeypatch.setattr(adapter, "_fetch_user_name", lambda open_id: None)
    return adapter


async def _emit_count(adapter: LarkAdapter, payload: dict[str, Any]) -> int:
    received: list[Any] = []

    async def emit(event: Any) -> None:
        received.append(event)

    adapter._emit = emit
    await adapter._deliver(payload)
    return len(received)


@pytest.mark.asyncio
async def test_require_mention_off_ingests_group_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_mod, "lark_oapi", object())
    adapter = LarkAdapter(_source())  # require_mention defaults off
    adapter._loop = asyncio.get_running_loop()
    monkeypatch.setattr(adapter, "_fetch_user_name", lambda open_id: None)
    assert await _emit_count(adapter, _payload(chat_type="group")) == 1


@pytest.mark.asyncio
async def test_require_mention_passes_p2p_without_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _mention_adapter(monkeypatch)
    assert await _emit_count(adapter, _payload(chat_type="p2p")) == 1


@pytest.mark.asyncio
async def test_require_mention_drops_group_without_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _mention_adapter(monkeypatch)
    assert await _emit_count(adapter, _payload(chat_type="group")) == 0


@pytest.mark.asyncio
async def test_require_mention_drops_group_mentioning_someone_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _mention_adapter(monkeypatch)
    payload = _payload(
        chat_type="group",
        mentions=[{"key": "@_user_1", "id": {"open_id": "ou_alice"}}],
    )
    assert await _emit_count(adapter, payload) == 0


@pytest.mark.asyncio
async def test_require_mention_passes_group_mentioning_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _mention_adapter(monkeypatch)
    payload = _payload(
        chat_type="group",
        mentions=[
            {"key": "@_user_1", "id": {"open_id": "ou_alice"}},
            {"key": "@_user_2", "id": {"open_id": "ou_bot"}},
        ],
    )
    assert await _emit_count(adapter, payload) == 1


@pytest.mark.asyncio
async def test_require_mention_fails_open_when_bot_id_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A group message that mentions *someone* but the bot's own open_id
    # can't be resolved: emit rather than silently dropping.
    adapter = _mention_adapter(monkeypatch, bot_open_id=None)
    payload = _payload(
        chat_type="group",
        mentions=[{"key": "@_user_1", "id": {"open_id": "ou_alice"}}],
    )
    assert await _emit_count(adapter, payload) == 1


@pytest.mark.asyncio
async def test_bot_open_id_fetched_once_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _mention_adapter(monkeypatch)
    calls = {"n": 0}

    def fetch() -> str:
        calls["n"] += 1
        return "ou_bot"

    monkeypatch.setattr(adapter, "_fetch_bot_open_id", fetch)
    payload = _payload(
        chat_type="group",
        mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
    )
    await _emit_count(adapter, payload)
    await _emit_count(adapter, payload)
    assert calls["n"] == 1
