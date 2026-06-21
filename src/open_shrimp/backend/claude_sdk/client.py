"""``ClaudeSdkClient`` — wraps ``ClaudeSDKClient`` to satisfy ``BackendClient``.

A thin wrapper holding one SDK client.  Every method delegates; the only real
logic is:

* ``receive_response`` applies ``translate._to_backend_event`` to each SDK
  message so SDK types never escape the adapter (this translation used to live
  in ``client_manager.receive_events`` / ``agent.run_agent``).
* ``is_alive`` owns the ``_transport._process.returncode`` poke that was
  ``client_manager._is_client_alive`` — SDK-private state belongs here.
* ``connect`` carries the resume-fallback retry that was inline in
  ``client_manager.get_or_create_session``: on a ``ProcessError`` with a stale
  ``resume``, rebuild the inner client with ``resume`` cleared and reconnect.
* ``session_id`` exposes the client's own view, captured from the init
  ``SystemMessage`` as ``receive_response`` streams.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, ProcessError

from open_shrimp.backend import types as bt
from open_shrimp.backend.claude_sdk.options import translate_options
from open_shrimp.backend.claude_sdk.translate import _to_backend_event
from open_shrimp.backend.protocol import BackendOptions

logger = logging.getLogger(__name__)


class ClaudeSdkClient:
    """A ``BackendClient`` backed by one ``ClaudeSDKClient``.

    Constructed (not connected) by ``ClaudeSdkBackend.make_client`` — matching
    the live ``ClaudeSDKClient(options=...)`` then separate ``.connect()``.
    """

    def __init__(self, options: BackendOptions) -> None:
        # Keep the backend-neutral options so ``connect`` can rebuild the inner
        # SDK client with ``resume`` cleared on the resume-fallback path.
        self._options = options
        self._client = ClaudeSDKClient(options=translate_options(options))
        self._session_id: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def is_alive(self) -> bool:
        """Check if the CLI subprocess is still running.

        Pokes into the transport's private state to detect a terminated
        process.  Returns True if the process appears healthy or if the
        state cannot be determined (fail-open).
        """
        try:
            transport = self._client._transport
            if transport is None:
                return False
            process = getattr(transport, "_process", None)
            if process is None:
                return False
            return process.returncode is None
        except Exception:
            return True

    async def connect(self) -> None:
        """Connect, falling back to a fresh session on a stale ``resume``.

        The SDK raises ``ProcessError`` when ``--resume`` points at a session
        that no longer exists (e.g. the container state was rebuilt or the
        ``.jsonl`` was deleted).  In that case we rebuild the inner client with
        ``resume`` cleared and reconnect, rather than surfacing a cryptic error.
        Behavior-identical to the inline retry this replaced.
        """
        try:
            await self._client.connect()
        except ProcessError:
            if not self._options.resume:
                raise
            logger.warning(
                "Failed to resume session %s – starting fresh",
                self._options.resume,
            )
            self._options.resume = None
            self._client = ClaudeSDKClient(
                options=translate_options(self._options)
            )
            await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def query(self, prompt: str) -> None:
        await self._client.query(prompt)

    async def receive_response(self) -> AsyncIterator[bt.Message]:
        """Yield translated events; capture the session id from the stream.

        Translation moves *into* the wrapper (it used to be applied by the
        manager after this call) so SDK message types never escape the adapter.
        The session id is captured from the init ``SystemMessage`` / final
        ``ResultMessage`` so the wrapper's ``session_id`` property is populated
        before the manager records it.
        """
        async for message in self._client.receive_response():
            event = _to_backend_event(message)
            if event is None:
                continue
            if isinstance(event, bt.SystemMessage):
                sid = getattr(event, "session_id", None)
                if sid:
                    self._session_id = sid
            elif isinstance(event, bt.ResultMessage):
                if event.session_id:
                    self._session_id = event.session_id
            yield event

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def stop_task(self, task_id: str) -> None:
        await self._client.stop_task(task_id)

    async def get_mcp_status(self) -> dict[str, Any]:
        return await self._client.get_mcp_status()

    async def reconnect_mcp_server(self, name: str) -> None:
        await self._client.reconnect_mcp_server(name)

    async def toggle_mcp_server(self, name: str, *, enabled: bool) -> None:
        await self._client.toggle_mcp_server(name, enabled=enabled)


__all__ = ["ClaudeSdkClient"]
