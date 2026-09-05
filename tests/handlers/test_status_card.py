"""The /status card: what it says, and what its buttons do.

The card replaced a two-column Markdown table.  Telegram sizes table columns
by content, so a label column half the screen wide wrapped a project path
across six lines on a phone — every assertion about full-width lines here is
guarding against that coming back.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from tests.rich_stub import RichBot, RichMessage, rendered

from open_shrimp.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_shrimp.db import ChatScope, init_db, set_active_context, set_session_id
from open_shrimp.handlers import commands
from open_shrimp.handlers import state

CHAT_ID = 100
SCOPE = ChatScope(chat_id=CHAT_ID, thread_id=None)
DIRECTORY = "/home/yjwong/Documents/glints-ai-transformation-labs"
SESSION_ID = "012e4cc2-a51b-4c9d-8f70-6ad2b1e5c3f4"


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


@pytest.fixture(autouse=True)
def clean_scope_state():
    """Every scope-keyed holder the card reads, emptied around each test.

    Listed by name rather than swept out of ``vars(state)``, which would
    also clear constants and the resume caches.
    """
    yield
    for holder in (
        state._running_tasks,
        state._run_started_at,
        state._injectable_sessions,
        state._setup_queues,
        state._active_bg_tasks,
        state._model_overrides,
        state._effort_overrides,
        state._pending_approvals,
        state._edit_approved_sessions,
        state._tool_approved_sessions,
        state._session_approved_dirs,
    ):
        holder.clear()


def _config(**ctx_extra: Any) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "labs": ContextConfig(
                directory=DIRECTORY,
                description="d",
                allowed_tools=[],
                **ctx_extra,
            )
        },
        default_context="labs",
        review=ReviewConfig(),
    )


class _StubUpdate:
    def __init__(self, bot: RichBot) -> None:
        self.message = RichMessage("/status", chat_id=CHAT_ID, bot=bot)
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": 1})()
        self.effective_chat = type(
            "C", (), {"id": CHAT_ID, "type": "private", "PRIVATE": "private"}
        )()


class _StubContext:
    def __init__(self, db, config: Config, bot: RichBot) -> None:
        self.bot = bot
        self.bot_data = {"config": config, "db": db}
        self.args: list[str] = []


class _StubQuery:
    """A CallbackQuery stand-in that records answers and markup swaps."""

    def __init__(self, bot: RichBot) -> None:
        self.message = RichMessage("", chat_id=CHAT_ID, message_id=7, bot=bot)
        self.from_user = type("U", (), {"id": 1})()
        self.answers: list[str | None] = []
        self.markups: list[Any] = []

        async def _edit_reply_markup(reply_markup=None):
            self.markups.append(reply_markup)

        self.message.edit_reply_markup = _edit_reply_markup

    async def answer(self, text: str | None = None, **_: Any) -> None:
        self.answers.append(text)


async def _bound(db, **ctx_extra: Any) -> Config:
    """A config whose only project is bound to SCOPE."""
    config = _config(**ctx_extra)
    await set_active_context(db, SCOPE, "labs")
    return config


async def _send_status(db, config: Config) -> tuple[str, Any]:
    bot = RichBot()
    await commands.status_handler(_StubUpdate(bot), _StubContext(db, config, bot))
    assert len(bot.sends) == 1
    return rendered(bot.sends[0].text), bot.sends[0].reply_markup


def _callbacks(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_has_no_table(db):
    config = await _bound(db)
    text, _ = await _send_status(db, config)
    assert "| :---" not in text, "the wrapping two-column table is back"


@pytest.mark.asyncio
async def test_directory_gets_a_line_to_itself(db):
    config = await _bound(db)
    text, _ = await _send_status(db, config)
    assert any(
        line.endswith(f"`{DIRECTORY}`") for line in text.splitlines()
    ), text


@pytest.mark.asyncio
async def test_session_id_is_whole_and_copyable(db):
    config = await _bound(db)
    await set_session_id(db, SCOPE, "labs", SESSION_ID)
    text, _ = await _send_status(db, config)
    # Monospace is tap-to-copy in Telegram, and a truncated id copies to
    # nothing usable.
    assert f"`{SESSION_ID}`" in text
    assert "..." not in text


@pytest.mark.asyncio
async def test_idle_says_it_once(db):
    config = await _bound(db)
    text, _ = await _send_status(db, config)
    assert "Idle" in text
    # Running / Injectable / Setup queued used to spend three rows on "No".
    assert "Injectable" not in text
    assert "Setup queued" not in text


@pytest.mark.asyncio
async def test_running_scope_reports_elapsed_and_queue(db):
    config = await _bound(db)

    async def _forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_forever())
    state._running_tasks[SCOPE] = task
    state._run_started_at[SCOPE] = time.monotonic() - 134
    state._setup_queues[SCOPE] = [("hi", [])]
    try:
        text, markup = await _send_status(db, config)
    finally:
        task.cancel()

    assert "Running" in text
    assert "2m 14s" in text or "2m14s" in text, text
    assert "1 queued" in text
    assert "starting up" in text  # not yet injectable
    assert commands.STATUS_CANCEL in _callbacks(markup)


@pytest.mark.asyncio
async def test_cancel_button_is_absent_when_idle(db):
    config = await _bound(db)
    _, markup = await _send_status(db, config)
    assert commands.STATUS_CANCEL not in _callbacks(markup)
    assert commands.STATUS_REFRESH in _callbacks(markup)
    assert commands.STATUS_CLEAR in _callbacks(markup)
    assert commands.STATUS_MODEL in _callbacks(markup)
    assert commands.STATUS_CONTEXT in _callbacks(markup)


@pytest.mark.asyncio
async def test_pending_approval_gets_its_own_line(db):
    config = await _bound(db)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    state.register_pending_approval(SCOPE, CHAT_ID, 5, future)
    text, _ = await _send_status(db, config)
    assert "Waiting on your approval" in text


@pytest.mark.asyncio
async def test_session_approvals_are_listed(db):
    from open_shrimp.hooks import ApprovalRule

    config = await _bound(db)
    state._edit_approved_sessions.add((SCOPE, "labs"))
    state._tool_approved_sessions[(SCOPE, "labs")] = [
        ApprovalRule("Bash", "git *"),
    ]
    state._session_approved_dirs[(SCOPE, "labs")] = {"/srv"}
    text, _ = await _send_status(db, config)
    assert "all edits" in text
    assert "1 tool rule" in text
    assert "1 extra dir" in text


@pytest.mark.asyncio
async def test_overrides_are_called_out(db):
    config = await _bound(db)
    state._model_overrides[SCOPE] = "claude-fable-5-1"
    text, _ = await _send_status(db, config)
    assert "claude-fable-5-1" in text
    assert "(override)" in text


@pytest.mark.asyncio
async def test_background_tasks_render_one_per_paragraph(db):
    config = await _bound(db)
    state.register_transient_task(
        SCOPE,
        "a3f1b2c4d5e6f7",
        description="Reviewing the sandbox port-forward path",
        task_type="local_agent",
    )
    text, _ = await _send_status(db, config)
    assert "Background tasks (1)" in text
    assert "Reviewing the sandbox port-forward path" in text
    assert "| Id |" not in text


@pytest.mark.asyncio
async def test_sandbox_state_and_forwards_are_shown(db):
    from open_shrimp.config import SandboxConfig, sandbox_backend

    class _FakeSandbox:
        def running(self) -> bool:
            return True

        def supports_port_forwarding(self) -> bool:
            return True

        def list_port_forwards(self, scope_key):
            assert scope_key == SCOPE.key, "forwards must be scoped to this chat"
            return [
                SimpleNamespace(
                    id="f1",
                    guest_port=3000,
                    host_port=8081,
                    scope_key=scope_key,
                    description="preview",
                )
            ]

    class _FakeManager:
        def get_active_sandbox(self, name):
            return _FakeSandbox()

    config = await _bound(db, sandbox=SandboxConfig(enabled=True, backend="libvirt"))
    bot = RichBot()
    stub = _StubContext(db, config, bot)
    stub.bot_data["sandbox_managers"] = {
        sandbox_backend(config.contexts["labs"]): _FakeManager()
    }
    await commands.status_handler(_StubUpdate(bot), stub)
    text = rendered(bot.sends[0].text)

    # The house summary-row grammar from tool_cards: em dash, then middle dots.
    assert "**Sandbox** — `libvirt` · running" in text
    assert "`127.0.0.1:8081`" in text
    assert "guest `3000`" in text
    assert "preview" in text


@pytest.mark.asyncio
async def test_unsandboxed_context_says_nothing_about_sandboxes(db):
    config = await _bound(db)
    text, _ = await _send_status(db, config)
    assert "Sandbox" not in text


@pytest.mark.asyncio
async def test_unbound_scope_still_gets_a_card(db):
    """The moment you most want to see what is set and what is not.

    A scope only reaches this state with ``default_context`` unset, so the
    config carries the project without pointing anything at it.
    """
    config = _config()
    config.default_context = None
    text, markup = await _send_status(db, config)
    assert "no project picked" in text
    assert "1 project configured" in text
    assert _callbacks(markup) == [commands.STATUS_CONTEXT]


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_edits_the_card_in_place(db):
    config = await _bound(db)
    bot = RichBot()
    query = _StubQuery(bot)
    handled = await commands.handle_status_callback(
        query, commands.STATUS_REFRESH, config, _StubContext(db, config, bot),
    )
    assert handled
    assert not bot.sends, "refresh must not post a second card"
    assert len(bot.edits) == 1
    assert "Idle" in rendered(bot.edits[0].text)
    assert query.answers == ["Refreshed"]


@pytest.mark.asyncio
async def test_clear_asks_before_it_clears(db):
    config = await _bound(db)
    await set_session_id(db, SCOPE, "labs", SESSION_ID)
    bot = RichBot()
    query = _StubQuery(bot)
    await commands.handle_status_callback(
        query, commands.STATUS_CLEAR, config, _StubContext(db, config, bot),
    )
    # Only the keyboard changed; the session is untouched until confirmed.
    assert not bot.edits
    assert len(query.markups) == 1
    confirm = _callbacks(query.markups[0])
    assert commands.STATUS_CLEAR_CONFIRM in confirm
    assert commands.STATUS_REFRESH in confirm

    from open_shrimp.db import get_session_id

    assert await get_session_id(db, SCOPE, "labs") == SESSION_ID


@pytest.mark.asyncio
async def test_confirmed_clear_drops_the_session(db):
    config = await _bound(db)
    await set_session_id(db, SCOPE, "labs", SESSION_ID)
    bot = RichBot()
    query = _StubQuery(bot)
    await commands.handle_status_callback(
        query,
        commands.STATUS_CLEAR_CONFIRM,
        config,
        _StubContext(db, config, bot),
    )

    from open_shrimp.db import get_session_id

    assert await get_session_id(db, SCOPE, "labs") is None
    assert query.answers == ["Session cleared"]


@pytest.mark.asyncio
async def test_model_button_posts_a_picker_beside_the_card(db):
    config = await _bound(db)
    bot = RichBot()
    query = _StubQuery(bot)
    await commands.handle_status_callback(
        query, commands.STATUS_MODEL, config, _StubContext(db, config, bot),
    )
    # A new message, so the status card survives the detour.
    assert len(bot.sends) == 1
    assert not bot.edits
    assert "Model" in rendered(bot.sends[0].text)


@pytest.mark.asyncio
async def test_context_button_posts_the_picker(db):
    config = await _bound(db)
    bot = RichBot()
    query = _StubQuery(bot)
    await commands.handle_status_callback(
        query, commands.STATUS_CONTEXT, config, _StubContext(db, config, bot),
    )
    assert len(bot.sends) == 1
    assert any(
        cb.startswith("ctx:") or cb.startswith("context")
        for cb in _callbacks(bot.sends[0].reply_markup)
    ), _callbacks(bot.sends[0].reply_markup)


@pytest.mark.asyncio
async def test_cancel_on_an_idle_scope_says_so(db):
    config = await _bound(db)
    bot = RichBot()
    query = _StubQuery(bot)
    await commands.handle_status_callback(
        query, commands.STATUS_CANCEL, config, _StubContext(db, config, bot),
    )
    assert query.answers == ["Nothing running."]


@pytest.mark.asyncio
async def test_unrelated_callback_is_not_claimed(db):
    config = _config()
    bot = RichBot()
    assert not await commands.handle_status_callback(
        _StubQuery(bot), "model:sonnet", config, _StubContext(db, config, bot),
    )


# ---------------------------------------------------------------------------
# The seams the card is built on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_state_is_probed_once_per_ttl(db):
    """A mashed Refresh must not open one SSH connection per tap."""
    from open_shrimp.config import SandboxConfig, sandbox_backend
    from open_shrimp.sandbox import status as sandbox_status

    probes = 0

    class _CountingSandbox:
        def running(self) -> bool:
            nonlocal probes
            probes += 1
            return True

        def supports_port_forwarding(self) -> bool:
            return False

    class _FakeManager:
        def get_active_sandbox(self, name):
            return _CountingSandbox()

    sandbox_status.invalidate("labs")
    config = await _bound(db, sandbox=SandboxConfig(enabled=True, backend="libvirt"))
    ctx = config.contexts["labs"]
    managers = {sandbox_backend(ctx): _FakeManager()}
    try:
        for _ in range(4):
            snapshot = await sandbox_status.describe_sandbox(
                managers, "labs", ctx, SCOPE.key,
            )
        assert snapshot.state == "running"
        assert probes == 1
    finally:
        sandbox_status.invalidate("labs")


@pytest.mark.asyncio
async def test_a_backend_that_will_not_say_is_not_cached(db):
    """An "unknown" is a failure to answer, so the next look tries again."""
    from open_shrimp.config import SandboxConfig, sandbox_backend
    from open_shrimp.sandbox import status as sandbox_status

    probes = 0

    class _BrokenSandbox:
        def running(self) -> bool:
            nonlocal probes
            probes += 1
            raise OSError("libvirt is down")

        def supports_port_forwarding(self) -> bool:
            return False

    class _FakeManager:
        def get_active_sandbox(self, name):
            return _BrokenSandbox()

    sandbox_status.invalidate("labs")
    config = await _bound(db, sandbox=SandboxConfig(enabled=True, backend="libvirt"))
    ctx = config.contexts["labs"]
    managers = {sandbox_backend(ctx): _FakeManager()}
    try:
        for _ in range(3):
            snapshot = await sandbox_status.describe_sandbox(
                managers, "labs", ctx, SCOPE.key,
            )
        assert snapshot.state == "unknown"
        assert probes == 3
    finally:
        sandbox_status.invalidate("labs")


@pytest.mark.asyncio
async def test_cancel_button_and_slash_cancel_tear_down_the_same_way(db):
    """One helper, so the two entry points cannot drift apart."""
    from open_shrimp.handlers.commands import _cancel_scope

    async def _forever() -> None:
        await asyncio.Event().wait()

    state.set_running_turn(SCOPE, asyncio.create_task(_forever()))
    state._injectable_sessions[SCOPE] = object()
    state._setup_queues[SCOPE] = [("a", []), ("b", [])]

    assert await _cancel_scope(SCOPE) == 2
    assert state.get_running_turn(SCOPE) is None
    assert state.get_run_elapsed(SCOPE) is None
    assert not state.is_injectable(SCOPE)
    assert state.queued_count(SCOPE) == 0
    # Idle scope: nothing to stop, and it says so rather than reporting 0.
    assert await _cancel_scope(SCOPE) is None


def test_clearing_the_running_turn_drops_its_start_stamp():
    """The two dicts are written and cleared by one pair of functions."""
    loop = asyncio.new_event_loop()
    try:
        fake = loop.create_future()
        state._running_tasks[SCOPE] = fake
        state._run_started_at[SCOPE] = time.monotonic()
        assert state.clear_running_turn(SCOPE) is fake
        assert SCOPE not in state._run_started_at
    finally:
        loop.close()
