"""``/fork``: copy a topic's session into a new topic.

The handler is driven against a stub backend and a stub bot; the two
backends' fork paths are exercised on their own, the Claude one against a
real transcript on disk and the OpenCode one through its cut translation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import open_shrimp.client_manager as cm
from open_shrimp.backend.claude_sdk import backend as claude_backend
from open_shrimp.backend.opencode.fork import first_dropped_id, last_exchange
from open_shrimp.backend.types import (
    AssistantMessage,
    ForkResult,
    ResultMessage,
    TextBlock,
)
from open_shrimp.config import Config, ContextConfig, TelegramConfig
from open_shrimp.db import (
    ChatScope,
    add_additional_directory,
    get_active_context,
    get_additional_directories,
    get_session_id,
    init_db,
    set_session_id,
)
from open_shrimp.handlers import commands, state
from open_shrimp.hooks import ApprovalRule
from tests.rich_stub import RichBot, RichMessage, rendered

CHAT_ID = 500
SOURCE = ChatScope(chat_id=CHAT_ID, thread_id=11)
NEW_THREAD = 77
FORK = ChatScope(chat_id=CHAT_ID, thread_id=NEW_THREAD)


@pytest.fixture(autouse=True)
def _isolate_state():
    def clear() -> None:
        cm._active_sessions.clear()
        for container in (
            state._backend_overrides,
            state._model_overrides,
            state._effort_overrides,
            state._tool_approved_sessions,
            state._session_approved_dirs,
            state._additional_dir_cache,
        ):
            container.clear()
        state._edit_approved_sessions.clear()

    clear()
    yield
    clear()


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "dev": ContextConfig(
                directory="/tmp", description="d", allowed_tools=[],
            )
        },
        default_context="dev",
        backend="claude_sdk",
    )


class _ForkBot(RichBot):
    def __init__(self, *, is_forum: bool = True) -> None:
        super().__init__()
        self.create_forum_topic = AsyncMock(
            return_value=SimpleNamespace(message_thread_id=NEW_THREAD),
        )
        self.get_chat = AsyncMock(return_value=SimpleNamespace(is_forum=is_forum))

    def texts_in(self, thread_id: int | None) -> list[str]:
        return [rendered(c.text) for c in self.sends if c.thread_id == thread_id]


class _StubBackend:
    name = "claude_sdk"
    display_name = "Claude Code"

    def __init__(self) -> None:
        self.fork_session = AsyncMock(
            return_value=ForkResult(
                session_id="new-session-id",
                summary="Fix the parser",
                last_prompt="why does it crash?",
                last_reply="Because the **buffer** is empty.",
            ),
        )
        self.delete_session = AsyncMock()

    def command_capabilities(self) -> set[str]:
        return {"fork"}


class _Update:
    def __init__(self, bot: _ForkBot, *, chat_type: str = "private") -> None:
        self.message = RichMessage(
            "/fork", chat_id=CHAT_ID, thread_id=SOURCE.thread_id, bot=bot,
        )
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=1)
        self.effective_chat = SimpleNamespace(
            id=CHAT_ID, type=chat_type, PRIVATE="private",
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubBackend:
    backend = _StubBackend()
    monkeypatch.setattr(cm, "resolve_backend", lambda *a, **k: backend)
    monkeypatch.setattr(commands, "_update_pinned_status", AsyncMock())
    return backend


async def _run(db, bot: _ForkBot, *, chat_type: str = "private") -> _Update:
    update = _Update(bot, chat_type=chat_type)
    context = SimpleNamespace(bot=bot, bot_data={"config": _config(), "db": db})
    await commands.fork_handler(update, context)
    return update


@pytest.mark.asyncio
async def test_idle_fork_binds_and_inherits_before_posting(db, stub):
    await set_session_id(db, SOURCE, "dev", "claude_sdk", "source-id")
    await add_additional_directory(db, SOURCE, "dev", "/srv/extra")
    state._model_overrides[SOURCE] = "opus"
    state._effort_overrides[SOURCE] = "high"
    state._edit_approved_sessions.add((SOURCE, "dev"))
    state._tool_approved_sessions[(SOURCE, "dev")] = [ApprovalRule("Bash", "git *")]
    state._session_approved_dirs[(SOURCE, "dev")] = {"/srv/other"}
    bot = _ForkBot()

    await _run(db, bot)

    assert stub.fork_session.await_args.args[0] == "source-id"
    assert stub.fork_session.await_args.kwargs["at_message_id"] is None
    bot.create_forum_topic.assert_awaited_once_with(
        CHAT_ID, name="Fix the parser (fork)",
    )
    assert await get_active_context(db, FORK) == "dev"
    assert await get_session_id(db, FORK, "dev", "claude_sdk") == "new-session-id"
    assert state._model_overrides[FORK] == "opus"
    assert state._effort_overrides[FORK] == "high"
    approvals = state.session_approvals(FORK, "dev")
    assert approvals.all_edits
    assert approvals.tool_rules == [ApprovalRule("Bash", "git *")]
    assert approvals.directories == {"/srv/other"}
    assert await get_additional_directories(db, FORK, "dev") == ["/srv/extra"]
    assert state._additional_dir_cache[(FORK, "dev")] == ["/srv/extra"]

    header, prompt, reply = bot.texts_in(NEW_THREAD)
    assert "Forked from" in header and "new-sess" in header
    assert "not included" not in header
    assert prompt == "**You:** why does it crash?"
    assert reply.startswith("**Claude Code:**") and "buffer" in reply
    # Nothing goes back to the source topic on success.
    assert bot.texts_in(SOURCE.thread_id) == []


@pytest.mark.asyncio
async def test_long_summary_is_cut_to_the_topic_name_limit(db, stub):
    await set_session_id(db, SOURCE, "dev", "claude_sdk", "source-id")
    stub.fork_session.return_value = ForkResult(session_id="n", summary="x" * 300)
    bot = _ForkBot()

    await _run(db, bot)

    assert len(bot.create_forum_topic.await_args.kwargs["name"]) == 128


@pytest.mark.asyncio
async def test_missing_summary_names_the_topic_after_the_context(db, stub):
    await set_session_id(db, SOURCE, "dev", "claude_sdk", "source-id")
    stub.fork_session.return_value = ForkResult(session_id="n")
    bot = _ForkBot()

    await _run(db, bot)

    assert bot.create_forum_topic.await_args.kwargs["name"] == "dev (fork)"
    # No echo to post: the header stands alone.
    assert len(bot.texts_in(NEW_THREAD)) == 1


def _live(*, in_turn: bool, last_completed: str | None) -> cm.AgentSession:
    session = cm.AgentSession(
        client=SimpleNamespace(), session_id="live-id", context_name="dev",
    )
    session.in_turn = in_turn
    session.last_completed_message_id = last_completed
    cm._active_sessions[SOURCE] = session
    return session


@pytest.mark.asyncio
async def test_mid_turn_fork_cuts_at_the_last_completed_turn(db, stub):
    _live(in_turn=True, last_completed="msg-7")
    bot = _ForkBot()

    await _run(db, bot)

    assert stub.fork_session.await_args.args[0] == "live-id"
    assert stub.fork_session.await_args.kwargs["at_message_id"] == "msg-7"
    assert "not included" in bot.texts_in(NEW_THREAD)[0]


@pytest.mark.asyncio
async def test_idle_live_client_copies_everything(db, stub):
    _live(in_turn=False, last_completed="msg-7")
    bot = _ForkBot()

    await _run(db, bot)

    assert stub.fork_session.await_args.kwargs["at_message_id"] is None


@pytest.mark.asyncio
async def test_first_turn_still_running_is_refused(db, stub):
    _live(in_turn=True, last_completed=None)
    bot = _ForkBot()

    update = await _run(db, bot)

    stub.fork_session.assert_not_awaited()
    bot.create_forum_topic.assert_not_awaited()
    assert "Fork once the running one ends" in rendered(update.message.replies[-1])


@pytest.mark.asyncio
async def test_group_without_topics_is_refused(db, stub):
    await set_session_id(db, SOURCE, "dev", "claude_sdk", "source-id")
    bot = _ForkBot(is_forum=False)

    update = await _run(db, bot, chat_type="supergroup")

    stub.fork_session.assert_not_awaited()
    bot.create_forum_topic.assert_not_awaited()
    assert "no topics" in rendered(update.message.replies[-1])


@pytest.mark.asyncio
async def test_forum_supergroup_links_the_source_topic(db, stub):
    source = ChatScope(chat_id=-1001234, thread_id=11)
    await set_session_id(db, source, "dev", "claude_sdk", "source-id")
    bot = _ForkBot(is_forum=True)
    update = _Update(bot, chat_type="supergroup")
    update.message.chat_id = -1001234
    context = SimpleNamespace(bot=bot, bot_data={"config": _config(), "db": db})

    await commands.fork_handler(update, context)

    assert "https://t.me/c/1234/11" in bot.texts_in(NEW_THREAD)[0]


@pytest.mark.asyncio
async def test_no_session_yet_is_refused(db, stub):
    bot = _ForkBot()

    update = await _run(db, bot)

    stub.fork_session.assert_not_awaited()
    bot.create_forum_topic.assert_not_awaited()
    assert "Nothing to fork yet" in rendered(update.message.replies[-1])


@pytest.mark.asyncio
async def test_topic_failure_deletes_the_fork(db, stub):
    await set_session_id(db, SOURCE, "dev", "claude_sdk", "source-id")
    bot = _ForkBot()
    bot.create_forum_topic.side_effect = RuntimeError("no rights")

    update = await _run(db, bot)

    assert stub.delete_session.await_args.args[0] == "new-session-id"
    assert "Could not create a topic" in rendered(update.message.replies[-1])
    assert await get_active_context(db, FORK) is None


@pytest.mark.asyncio
async def test_backend_failure_creates_no_topic(db, stub):
    await set_session_id(db, SOURCE, "dev", "claude_sdk", "source-id")
    stub.fork_session.side_effect = ValueError("transcript is empty")
    bot = _ForkBot()

    update = await _run(db, bot)

    bot.create_forum_topic.assert_not_awaited()
    assert "transcript is empty" in rendered(update.message.replies[-1])


# ---------------------------------------------------------------------------
# State copy
# ---------------------------------------------------------------------------


def test_copy_scope_state_copies_containers_by_value():
    state._tool_approved_sessions[(SOURCE, "dev")] = [ApprovalRule("WebFetch")]
    state._session_approved_dirs[(SOURCE, "dev")] = {"/a"}

    state.copy_scope_state(SOURCE, FORK, "dev")
    state._tool_approved_sessions[(FORK, "dev")].append(ApprovalRule("Bash"))
    state._session_approved_dirs[(FORK, "dev")].add("/b")

    assert state._tool_approved_sessions[(SOURCE, "dev")] == [ApprovalRule("WebFetch")]
    assert state._session_approved_dirs[(SOURCE, "dev")] == {"/a"}


def test_copy_scope_state_leaves_absent_entries_absent():
    state.copy_scope_state(SOURCE, FORK, "dev")

    assert FORK not in state._model_overrides
    assert (FORK, "dev") not in state._tool_approved_sessions
    assert not state.session_approvals(FORK, "dev").all_edits


# ---------------------------------------------------------------------------
# Turn boundary tracking
# ---------------------------------------------------------------------------


class _ScriptedClient:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages

    async def receive_response(self):
        for message in self._messages:
            yield message


@pytest.mark.asyncio
async def test_result_records_the_turns_last_top_level_message():
    session = cm.AgentSession(
        client=_ScriptedClient([
            AssistantMessage(content=[TextBlock("a")], message_id="m1"),
            AssistantMessage(
                content=[TextBlock("sub")], message_id="sub-1",
                parent_tool_use_id="toolu_1",
            ),
            AssistantMessage(content=[TextBlock("b")], message_id="m2"),
            ResultMessage(session_id="s"),
        ]),
    )
    session.in_turn = True

    seen: list[Any] = []
    async for message in cm.receive_events(session):
        seen.append(message)
        if isinstance(message, AssistantMessage) and message.message_id == "m2":
            # Mid-turn, the previous boundary still stands.
            assert session.last_completed_message_id is None

    assert session.last_completed_message_id == "m2"
    assert not session.in_turn


# ---------------------------------------------------------------------------
# Claude: offline fork against a sandboxed claude-home
# ---------------------------------------------------------------------------


def _entry(kind: str, content: Any, parent: str | None, sid: str, cwd: str) -> dict:
    return {
        "type": kind,
        "uuid": str(uuid.uuid4()),
        "parentUuid": parent,
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": "2026-11-01T00:00:00Z",
        "message": {"role": kind, "content": content},
    }


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture
def sandboxed_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A claude-home the sandbox would mount, and a host config dir to watch."""
    home = tmp_path / "claude-home"
    host_config = tmp_path / "host-claude"
    host_config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host_config))
    monkeypatch.setattr(
        claude_backend, "_sandbox_home_mount",
        lambda *a: SimpleNamespace(host_dir=home, holds_session_state=True),
    )
    return home, host_config


def _project_dir(home: Path, directory: Path) -> Path:
    from claude_agent_sdk._internal.sessions import (
        _canonicalize_path,
        _sanitize_path,
    )

    return home / "projects" / _sanitize_path(_canonicalize_path(str(directory)))


@pytest.mark.asyncio
async def test_claude_sandboxed_fork_writes_into_the_claude_home(
    tmp_path, sandboxed_home,
):
    home, host_config = sandboxed_home
    workdir = tmp_path / "proj"
    workdir.mkdir()
    sid = str(uuid.uuid4())
    u1 = _entry("user", "fix the parser", None, sid, str(workdir))
    a1 = _entry(
        "assistant", [{"type": "text", "text": "Fixed."}], u1["uuid"], sid,
        str(workdir),
    )
    u2 = _entry("user", "now the lexer", a1["uuid"], sid, str(workdir))
    a2 = _entry(
        "assistant", [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}],
        u2["uuid"], sid, str(workdir),
    )
    source = _project_dir(home, workdir) / f"{sid}.jsonl"
    _write_transcript(source, [u1, a1, u2, a2])

    result = await claude_backend.ClaudeSdkBackend.fork_session(
        object(), sid, workdir, at_message_id=a1["uuid"],
        ctx=None, ctx_name="dev", sandbox_managers={},
    )

    fork_path = source.parent / f"{result.session_id}.jsonl"
    assert fork_path.is_file()
    assert oct(fork_path.stat().st_mode & 0o777) == "0o600"
    assert list(host_config.iterdir()) == []
    kept = [
        json.loads(line) for line in fork_path.read_text().splitlines()
    ]
    messages = [e for e in kept if e.get("type") in ("user", "assistant")]
    assert [m["message"]["content"] for m in messages] == [
        "fix the parser", [{"type": "text", "text": "Fixed."}],
    ]
    assert result.summary == "fix the parser"
    assert result.last_prompt == "fix the parser"
    assert result.last_reply == "Fixed."
    titles = [e["customTitle"] for e in kept if e.get("type") == "custom-title"]
    assert titles == ["fix the parser (fork)"]

    await claude_backend.ClaudeSdkBackend.delete_session(
        object(), result.session_id, workdir,
        ctx=None, ctx_name="dev", sandbox_managers={},
    )
    assert not fork_path.exists()
    assert source.exists()


def test_claude_echo_skips_tool_results_and_borrows_no_older_reply():
    sid, cwd = "s", "/w"
    u1 = _entry("user", "first", None, sid, cwd)
    a1 = _entry("assistant", [{"type": "text", "text": "old reply"}], None, sid, cwd)
    u2 = _entry("user", [{"type": "text", "text": "second"}], None, sid, cwd)
    a2 = _entry("assistant", [{"type": "tool_use", "id": "t"}], None, sid, cwd)
    result = _entry(
        "user", [{"type": "tool_result", "tool_use_id": "t", "content": "x"}],
        None, sid, cwd,
    )

    assert claude_backend._last_exchange([u1, a1, u2, a2, result]) == ("second", None)


# ---------------------------------------------------------------------------
# OpenCode: "last to keep" -> "first to drop"
# ---------------------------------------------------------------------------


def _rows(*ids: str) -> list[dict]:
    return [{"info": {"id": i}, "parts": []} for i in ids]


def test_opencode_keeping_a_message_drops_the_next():
    assert first_dropped_id(_rows("m1", "m2", "m3"), "m2") == "m3"


def test_opencode_keeping_the_last_message_sends_no_cut():
    assert first_dropped_id(_rows("m1", "m2"), "m2") is None


def test_opencode_no_cut_requested_sends_no_cut():
    assert first_dropped_id(_rows("m1"), None) is None


def test_opencode_unknown_message_raises():
    with pytest.raises(ValueError):
        first_dropped_id(_rows("m1", "m2"), "gone")


def test_opencode_echo_ignores_synthetic_parts():
    rows = [
        {"info": {"id": "u", "role": "user"}, "parts": [
            {"type": "text", "text": "file contents", "synthetic": True},
            {"type": "text", "text": "what is this?"},
        ]},
        {"info": {"id": "a", "role": "assistant"}, "parts": [
            {"type": "reasoning", "text": "hmm"},
            {"type": "text", "text": "A config file."},
        ]},
    ]

    assert last_exchange(rows) == ("what is this?", "A config file.")
