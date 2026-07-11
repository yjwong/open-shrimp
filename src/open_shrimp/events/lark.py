"""Lark (Feishu) inbound event adapter over the WebSocket long connection.

Uses the ``lark-oapi`` SDK's WS client, which requires no public URL: it
connects out and authenticates with app_id/app_secret. The SDK client is
blocking (``ws.Client.start()`` never returns) and drives all its coroutines
on a module-level event loop captured at ``lark_oapi.ws.client`` import time,
so we give it a dedicated loop and run it in a daemon thread. Because that
loop is a module-level global in the SDK, at most one Lark adapter per
process is supported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any

from open_shrimp.events.base import EmitFn
from open_shrimp.events.types import Event

if TYPE_CHECKING:
    from open_shrimp.config import EventSourceConfig

try:
    import lark_oapi  # type: ignore[import-untyped]
except ImportError:
    lark_oapi = None

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "the 'lark-oapi' package is not installed — "
    "install with 'uv sync --extra lark'"
)
_JOIN_TIMEOUT = 10.0
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 300.0
_NAME_CACHE_SIZE = 512
_CONTEXT_MESSAGE_LIMIT = 20


def extract_text(message_type: str | None, content: str | None) -> str | None:
    """Extract plain text from a ``text`` message's JSON-encoded content."""
    if message_type != "text" or not content:
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    text = parsed.get("text")
    return text if isinstance(text, str) else None


def substitute_mentions(text: str, mentions: Any) -> str:
    """Replace ``@_user_N`` placeholders with ``@<display name>``.

    Lark message text carries opaque mention keys; the display names arrive
    alongside in the message's ``mentions`` array.
    """
    if not isinstance(mentions, list):
        return text
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = mention.get("key")
        name = mention.get("name")
        if isinstance(key, str) and key and isinstance(name, str) and name:
            text = text.replace(key, f"@{name}")
    return text


def sender_open_id(payload: dict[str, Any]) -> str | None:
    """Pull the sender's open_id out of a p2 im.message.receive_v1 payload."""
    event = payload.get("event") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    open_id = sender_id.get("open_id")
    return open_id if isinstance(open_id, str) and open_id else None


def chat_type(payload: dict[str, Any]) -> str | None:
    """Pull the message's chat_type (``p2p`` or ``group``) from a payload."""
    event = payload.get("event") or {}
    message = event.get("message") or {}
    value = message.get("chat_type")
    return value if isinstance(value, str) else None


def mention_open_ids(payload: dict[str, Any]) -> set[str]:
    """Collect the open_ids of everyone @-mentioned in the message."""
    event = payload.get("event") or {}
    message = event.get("message") or {}
    mentions = message.get("mentions")
    ids: set[str] = set()
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mid = mention.get("id")
            if isinstance(mid, dict):
                open_id = mid.get("open_id")
                if isinstance(open_id, str) and open_id:
                    ids.add(open_id)
    return ids


def map_message_event(
    source: str,
    payload: dict[str, Any],
    sender_name: str | None = None,
) -> Event:
    """Map a p2 im.message.receive_v1 payload dict to an Event.

    Text messages get ``Event.text``; anything else falls back to the full
    payload in ``Event.raw``. Since Event has no header field, the message
    type is appended to ``sender`` (e.g. ``Alice · [post]``) so the sink's
    JSON fallback stays identifiable.
    """
    header = payload.get("header") or {}
    event = payload.get("event") or {}
    message = event.get("message") or {}
    message_type = message.get("message_type")

    sender = sender_name or sender_open_id(payload)
    text = extract_text(message_type, message.get("content"))
    if text is not None:
        text = substitute_mentions(text, message.get("mentions"))
    raw: dict[str, Any] | None = None
    if text is None:
        raw = payload
        if isinstance(message_type, str) and message_type != "text":
            tag = f"[{message_type}]"
            sender = f"{sender} · {tag}" if sender else tag

    message_id = message.get("message_id")
    reply_ref: dict[str, Any] | None = None
    if isinstance(message_id, str) and message_id:
        reply_ref = {"message_id": message_id}

    # Thread/chat handle for fetching surrounding context at read time.
    # thread_id is Lark's real thread container id (omt_…), present only on
    # threaded messages; it is distinct from root_id (an om_… message id).
    # A missing thread_id means the message is not in a thread.
    context_ref: dict[str, Any] | None = None
    chat_id = message.get("chat_id")
    if isinstance(chat_id, str) and chat_id:
        thread_id = message.get("thread_id")
        context_ref = {
            "chat_id": chat_id,
            "thread_id": thread_id if isinstance(thread_id, str) and thread_id else None,
            "anchor_message_id": message_id,
        }

    dedup_key = header.get("event_id")
    return Event(
        source=source,
        sender=sender,
        text=text,
        raw=raw,
        dedup_key=dedup_key if isinstance(dedup_key, str) else None,
        reply_ref=reply_ref,
        context_ref=context_ref,
    )


def _payload_from(data: Any) -> dict[str, Any]:
    """Convert an SDK-typed event object (or a plain dict) to a dict payload."""
    if isinstance(data, dict):
        return data
    assert lark_oapi is not None
    marshalled = lark_oapi.JSON.marshal(data)
    parsed = json.loads(marshalled or "{}")
    return parsed if isinstance(parsed, dict) else {}


class LarkAdapter:
    """EventSourceAdapter for Lark via the WS long connection."""

    def __init__(self, source: EventSourceConfig) -> None:
        if lark_oapi is None:
            raise RuntimeError(
                f"Lark event source '{source.name}': {_INSTALL_HINT}"
            )
        self.name: str = source.name
        self._app_id: str = source.app_id or ""
        self._app_secret: str = source.app_secret or ""
        # Resolved to an SDK URL constant lazily in start()/_run(), so
        # construction stays free of SDK attribute access (tests build the
        # adapter with a sentinel lark_oapi).
        self._domain_key: str = source.domain or "feishu"
        self._require_mention: bool = source.require_mention
        self._bot_open_id: str | None = None
        self._emit: EmitFn | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._api_client: Any = None
        self._name_cache: dict[str, str | None] = {}

    def _resolve_domain(self) -> str:
        assert lark_oapi is not None
        return {
            "lark": lark_oapi.LARK_DOMAIN,
            "feishu": lark_oapi.FEISHU_DOMAIN,
        }.get(self._domain_key, lark_oapi.FEISHU_DOMAIN)

    async def start(self, emit: EmitFn) -> None:
        if lark_oapi is None:  # pragma: no cover - constructor already guards
            raise RuntimeError(f"Lark event source '{self.name}': {_INSTALL_HINT}")
        self._emit = emit
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._api_client = (
            lark_oapi.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(self._resolve_domain())
            .build()
        )
        self._thread = threading.Thread(
            target=self._run, name=f"lark-ws-{self.name}", daemon=True
        )
        self._thread.start()
        logger.info("lark[%s]: adapter started", self.name)

    async def stop(self) -> None:
        self._stopping.set()
        sdk_loop = self._sdk_loop
        client = self._ws_client
        if sdk_loop is not None and client is not None:

            def _shutdown() -> None:
                client._auto_reconnect = False
                task = sdk_loop.create_task(client._disconnect())
                task.add_done_callback(lambda _t: sdk_loop.stop())

            try:
                sdk_loop.call_soon_threadsafe(_shutdown)
            except RuntimeError:
                pass  # loop already closed

        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, _JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "lark[%s]: ws thread did not exit within %.0fs; "
                    "abandoning (daemon thread)",
                    self.name,
                    _JOIN_TIMEOUT,
                )
            else:
                logger.info("lark[%s]: adapter stopped", self.name)
        self._thread = None
        self._ws_client = None
        self._sdk_loop = None

    def _run(self) -> None:
        """Thread body: run the blocking SDK client, retry with backoff."""
        assert lark_oapi is not None
        backoff = _BACKOFF_INITIAL
        while not self._stopping.is_set():
            sdk_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(sdk_loop)
            # The SDK schedules everything on this module-level global.
            lark_oapi.ws.client.loop = sdk_loop
            self._sdk_loop = sdk_loop

            handler = (
                lark_oapi.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message)
                .build()
            )
            client = lark_oapi.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=handler,
                domain=self._resolve_domain(),
            )
            self._ws_client = client
            if self._stopping.is_set():
                self._drain_loop(sdk_loop)
                break
            try:
                client.start()  # blocks; SDK auto-reconnects internally
                if not self._stopping.is_set():
                    logger.error(
                        "lark[%s]: ws client returned unexpectedly; "
                        "restarting in %.0fs",
                        self.name,
                        backoff,
                    )
            except Exception:
                if self._stopping.is_set():
                    break
                logger.exception(
                    "lark[%s]: ws client died; restarting in %.0fs",
                    self.name,
                    backoff,
                )
            finally:
                self._drain_loop(sdk_loop)
            if self._stopping.wait(backoff):
                break
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def _drain_loop(self, sdk_loop: asyncio.AbstractEventLoop) -> None:
        try:
            pending = asyncio.all_tasks(sdk_loop)
            for task in pending:
                task.cancel()
            if pending:
                sdk_loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            sdk_loop.close()
        except Exception:
            logger.debug(
                "lark[%s]: error draining sdk loop", self.name, exc_info=True
            )

    def _on_message(self, data: Any) -> None:
        """SDK callback; fires on the WS thread. Hop into the bot's loop."""
        loop = self._loop
        if loop is None or self._emit is None:
            logger.warning("lark[%s]: event received before start; dropped", self.name)
            return
        try:
            payload = _payload_from(data)
        except Exception:
            logger.exception("lark[%s]: failed to decode event payload", self.name)
            return
        asyncio.run_coroutine_threadsafe(self._deliver(payload), loop)

    async def _deliver(self, payload: dict[str, Any]) -> None:
        emit = self._emit
        if emit is None:
            return
        try:
            if self._require_mention and not await self._addresses_bot(payload):
                return
            open_id = sender_open_id(payload)
            sender_name = await self._resolve_sender(open_id) if open_id else None
            event = map_message_event(self.name, payload, sender_name)
            await emit(event)
        except Exception:
            logger.exception("lark[%s]: failed to deliver event", self.name)

    async def _addresses_bot(self, payload: dict[str, Any]) -> bool:
        """Whether a message should pass the require_mention gate.

        A p2p (direct) message is implicitly addressed to the bot. A group
        message must @-mention the bot; a group message with no mentions is
        dropped without a lookup. When the bot's own open_id can't be
        resolved we fail open, preferring an extra notice over a silent drop.
        """
        if chat_type(payload) == "p2p":
            return True
        ids = mention_open_ids(payload)
        if not ids:
            return False
        bot_open_id = await self._ensure_bot_open_id()
        if bot_open_id is None:
            return True
        return bot_open_id in ids

    async def _ensure_bot_open_id(self) -> str | None:
        if self._bot_open_id is not None:
            return self._bot_open_id
        try:
            self._bot_open_id = await asyncio.to_thread(self._fetch_bot_open_id)
        except Exception:
            logger.warning(
                "lark[%s]: could not resolve bot open_id for mention gating",
                self.name,
                exc_info=True,
            )
            self._bot_open_id = None
        return self._bot_open_id

    def _fetch_bot_open_id(self) -> str | None:
        """Blocking fetch of the bot's own open_id via /bot/v3/info."""
        if self._api_client is None:
            return None
        from lark_oapi.core.enum import AccessTokenType, HttpMethod
        from lark_oapi.core.model import BaseRequest

        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        response = self._api_client.request(request)
        if not response.success() or response.raw is None or not response.raw.content:
            logger.debug(
                "lark[%s]: bot info lookup failed: code=%s msg=%s",
                self.name,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
            return None
        try:
            data = json.loads(response.raw.content)
        except (json.JSONDecodeError, TypeError):
            return None
        bot = data.get("bot") if isinstance(data, dict) else None
        open_id = bot.get("open_id") if isinstance(bot, dict) else None
        return open_id if isinstance(open_id, str) and open_id else None

    def _cache_name(self, open_id: str, name: str | None) -> str | None:
        """Store a resolved (possibly None) display name with LRU eviction."""
        self._name_cache[open_id] = name
        while len(self._name_cache) > _NAME_CACHE_SIZE:
            self._name_cache.pop(next(iter(self._name_cache)))
        return name

    async def _resolve_sender(self, open_id: str) -> str | None:
        if open_id in self._name_cache:
            return self._name_cache[open_id]
        try:
            name = await asyncio.to_thread(self._fetch_user_name, open_id)
        except Exception:
            logger.debug(
                "lark[%s]: could not resolve user name for %s",
                self.name,
                open_id,
                exc_info=True,
            )
            name = None
        return self._cache_name(open_id, name)

    async def reply(self, reply_ref: dict, text: str) -> None:
        """Send *text* back to the originating message, in its thread."""
        message_id = reply_ref.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("event carries no Lark message_id to reply to")
        await asyncio.to_thread(self._send_reply, message_id, text)

    def _send_reply(self, message_id: str, text: str) -> None:
        """Blocking reply via the REST client (runs in a worker thread)."""
        if self._api_client is None:
            raise RuntimeError("Lark adapter is not started")
        from lark_oapi.api.im.v1 import (  # type: ignore[import-untyped]
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .msg_type("text")
                .reply_in_thread(True)
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(
                f"Lark reply failed: code={getattr(response, 'code', None)} "
                f"msg={getattr(response, 'msg', None)}"
            )

    async def fetch_context(self, context_ref: dict) -> str | None:
        """Recent thread/chat messages around the event, oldest-first.

        Returns None when nothing useful is available; the caller degrades
        to the base event content. Text only — the caller wraps the return
        value as untrusted data, so it must carry no instructions.
        """
        chat_id = context_ref.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return None
        thread_id = context_ref.get("thread_id")
        anchor = context_ref.get("anchor_message_id")
        return await asyncio.to_thread(
            self._fetch_context_blocking, chat_id, thread_id, anchor
        )

    def _fetch_context_blocking(
        self,
        chat_id: str,
        thread_id: str | None,
        anchor: str | None,
        limit: int = _CONTEXT_MESSAGE_LIMIT,
    ) -> str | None:
        if self._api_client is None:
            raise RuntimeError("Lark adapter is not started")
        # A threaded message reads from its exact thread container. A
        # non-threaded message has no thread, so recent chat history is the
        # only surrounding context. Never cross-fall-back: listing whole-chat
        # history for a threaded message returns unrelated conversations.
        if isinstance(thread_id, str) and thread_id:
            items = self._list_messages("thread", thread_id, limit)
        else:
            items = self._list_messages("chat", chat_id, limit)
        lines = [
            line
            for item in items
            if (line := self._render_listed_message(item, anchor)) is not None
        ]
        return "\n".join(lines) if lines else None

    def _list_messages(
        self, container_id_type: str, container_id: str, limit: int
    ) -> list[Any]:
        """Blocking im.v1.message.list; newest *limit*, returned oldest-first."""
        from lark_oapi.api.im.v1 import (  # type: ignore[import-untyped]
            ListMessageRequest,
        )

        request = (
            ListMessageRequest.builder()
            .container_id_type(container_id_type)
            .container_id(container_id)
            .sort_type("ByCreateTimeDesc")
            .page_size(limit)
            .build()
        )
        response = self._api_client.im.v1.message.list(request)
        if not response.success():
            logger.debug(
                "lark[%s]: message.list(%s=%s) failed: code=%s msg=%s",
                self.name,
                container_id_type,
                container_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
            return []
        items = getattr(response.data, "items", None) or []
        return list(reversed(items))

    def _render_listed_message(self, item: Any, anchor: str | None) -> str | None:
        """One ``sender: text`` line for a listed message, or None to skip."""
        body = getattr(item, "body", None)
        text = extract_text(
            getattr(item, "msg_type", None), getattr(body, "content", None)
        )
        if text is None:
            return None
        open_id = getattr(getattr(item, "sender", None), "id", None)
        if not isinstance(open_id, str) or not open_id:
            open_id = None
        who = (
            (self._resolve_sender_blocking(open_id) if open_id else None)
            or open_id
            or "unknown"
        )
        marker = (
            " (event message)"
            if getattr(item, "message_id", None) == anchor
            else ""
        )
        return f"{who}{marker}: {text}"

    def _resolve_sender_blocking(self, open_id: str) -> str | None:
        """Cached, blocking counterpart to :meth:`_resolve_sender`."""
        if open_id in self._name_cache:
            return self._name_cache[open_id]
        try:
            name = self._fetch_user_name(open_id)
        except Exception:
            name = None
        return self._cache_name(open_id, name)

    def _fetch_user_name(self, open_id: str) -> str | None:
        """Blocking user-info fetch; tenant scope may not allow it."""
        if self._api_client is None:
            return None
        from lark_oapi.api.contact.v3 import GetUserRequest  # type: ignore[import-untyped]

        request = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        response = self._api_client.contact.v3.user.get(request)
        if (
            not response.success()
            or response.data is None
            or response.data.user is None
        ):
            logger.debug(
                "lark[%s]: user lookup for %s failed: code=%s msg=%s",
                self.name,
                open_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
            return None
        name = response.data.user.name
        return name if isinstance(name, str) and name else None
