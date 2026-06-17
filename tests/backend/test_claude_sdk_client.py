"""``ClaudeSdkClient`` wrapper tests: connect resume-fallback, liveness
poke, and receive_response translation + session-id capture.
"""

from __future__ import annotations

import claude_agent_sdk.types as sdk
import pytest
from claude_agent_sdk import ProcessError

import open_shrimp.backend.claude_sdk.client as client_mod
from open_shrimp.backend import types as bt
from open_shrimp.backend.claude_sdk.client import ClaudeSdkClient
from open_shrimp.backend.protocol import BackendOptions

pytestmark = pytest.mark.asyncio


class _FakeInner:
    """Stand-in for ClaudeSDKClient; records the options it was built with."""

    instances: list["_FakeInner"] = []

    def __init__(self, options):
        self.options = options
        self.connect_calls = 0
        # First constructed instance fails connect if it has a resume set.
        _FakeInner.instances.append(self)

    async def connect(self):
        self.connect_calls += 1
        if self.options.resume:
            raise ProcessError("stale resume")


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeInner.instances.clear()
    yield
    _FakeInner.instances.clear()


async def test_connect_resume_fallback_rebuilds_without_resume(monkeypatch):
    """On ProcessError with a stale resume, the wrapper rebuilds the inner
    client with resume cleared and reconnects — and the options object the
    manager passed has its resume nulled (so AgentSession.session_id can be
    corrected)."""
    monkeypatch.setattr(client_mod, "ClaudeSDKClient", _FakeInner)

    opts = BackendOptions(cwd="/w", resume="sess-stale")
    wrapper = ClaudeSdkClient(opts)
    await wrapper.connect()

    # Two inner clients built: the resuming one (failed) + the fresh retry.
    assert len(_FakeInner.instances) == 2
    assert _FakeInner.instances[0].options.resume is not None  # first attempt
    assert _FakeInner.instances[1].options.resume is None  # retry cleared
    # The shared options object the manager holds reflects the fallback.
    assert opts.resume is None


async def test_connect_without_resume_does_not_swallow_process_error(monkeypatch):
    """A ProcessError on a fresh (no-resume) connect must propagate."""

    class _AlwaysFails(_FakeInner):
        async def connect(self):
            self.connect_calls += 1
            raise ProcessError("spawn failure")

    monkeypatch.setattr(client_mod, "ClaudeSDKClient", _AlwaysFails)
    wrapper = ClaudeSdkClient(BackendOptions(cwd="/w"))
    with pytest.raises(ProcessError):
        await wrapper.connect()


async def test_receive_response_translates_and_captures_session_id(monkeypatch):
    """receive_response yields backend.types (not SDK types) and captures the
    session id from the stream into the wrapper's session_id property."""

    class _Inner(_FakeInner):
        async def receive_response(self):
            yield sdk.SystemMessage(subtype="init", data={"session_id": "s1"})
            yield sdk.ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s1",
                total_cost_usd=0.0,
            )

    monkeypatch.setattr(client_mod, "ClaudeSDKClient", _Inner)
    wrapper = ClaudeSdkClient(BackendOptions(cwd="/w"))
    await wrapper.connect()

    out = [m async for m in wrapper.receive_response()]
    assert all(isinstance(m, bt.Message.__args__) for m in out)  # backend types
    assert isinstance(out[0], bt.SystemMessage)
    assert isinstance(out[-1], bt.ResultMessage)
    assert wrapper.session_id == "s1"


async def test_is_alive_fails_open_without_transport(monkeypatch):
    monkeypatch.setattr(client_mod, "ClaudeSDKClient", _FakeInner)
    wrapper = ClaudeSdkClient(BackendOptions(cwd="/w"))
    # _FakeInner has no _transport attribute → fail-open True.
    assert wrapper.is_alive() is True
