"""``/share``: export a session as one self-contained HTML page.

The Claude reader runs against a transcript written to disk with the shapes
a plain parent walk gets wrong (parallel tool results, a compaction, a
subagent).  The OpenCode reader runs against a mock ``opencode serve``.  The
handler is driven against a stub backend and a stub bot.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import open_shrimp.client_manager as cm
from open_shrimp import share
from open_shrimp.backend.claude_sdk.export import export_transcript
from open_shrimp.backend.opencode import export as oc_export
from open_shrimp.backend.types import (
    SessionTranscript,
    TranscriptImage,
    TranscriptText,
    TranscriptToolCall,
)
from open_shrimp.config import Config, ContextConfig, TelegramConfig
from open_shrimp.db import ChatScope, init_db, set_session_id
from open_shrimp.handlers import commands
from tests.rich_stub import RichBot, RichMessage, rendered

SID = "11111111-2222-3333-4444-555555555555"
PNG = "iVBORw0KGgo="


# ── Claude ──


def _entry(
    uuid: str, kind: str, content: Any, parent: str | None, **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": kind, "content": content}
    if kind == "assistant":
        message["model"] = extra.pop("model", "claude-opus-5-5")
    return {
        "type": kind, "uuid": uuid, "parentUuid": parent, "sessionId": SID,
        "timestamp": extra.pop("ts", "2026-11-20T10:00:00.000Z"),
        "isSidechain": False, "message": message, **extra,
    }


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture
def claude_session(tmp_path: Path) -> Path:
    path = tmp_path / f"{SID}.jsonl"
    _write(path, [
        # Before the compaction: only reachable through logicalParentUuid.
        _entry("u1", "user", "first prompt", None),
        _entry("a1", "assistant", [{"type": "text", "text": "first reply"}], "u1"),
        {"type": "system", "subtype": "compact_boundary", "uuid": "cb",
         "parentUuid": None, "logicalParentUuid": "a1", "sessionId": SID},
        _entry("cs", "user", "Summary of the conversation so far", "cb",
               isCompactSummary=True),
        {"type": "attachment", "uuid": "att", "parentUuid": "cs", "sessionId": SID,
         "attachment": {"type": "instructions", "content": "CLAUDE.md body"}},
        _entry("m1", "user", "<hook output>", "att", isMeta=True),
        _entry("u2", "user", "fix the bug <b>now</b>", "m1",
               ts="2026-11-20T11:00:00.000Z"),
        _entry("a2", "assistant", [{"type": "thinking", "thinking": "let me look"}], "u2"),
        _entry("a3", "assistant", [{"type": "thinking", "thinking": "", "signature": "x"}], "a2"),
        # Two parallel calls: each result's parent is the entry holding its
        # tool_use, so r1 is off the leaf-to-root walk.
        _entry("a4", "assistant", [{"type": "tool_use", "id": "t1", "name": "Edit",
               "input": {"file_path": "/p/a.py", "old_string": "x = 1", "new_string": "x = 2"}}], "a3"),
        _entry("a5", "assistant", [{"type": "tool_use", "id": "t2", "name": "Read",
               "input": {"file_path": "/p/shot.png"}}], "a4"),
        _entry("r1", "user", [{"type": "tool_result", "tool_use_id": "t1",
               "content": "The file was updated"}], "a4",
               toolUseResult={"structuredPatch": [{"oldStart": 3, "oldLines": 1,
                              "newStart": 3, "newLines": 1, "lines": ["-x = 1", "+x = 2"]}]}),
        _entry("r2", "user", [{"type": "tool_result", "tool_use_id": "t2", "content": [
               {"type": "image", "source": {"type": "base64", "data": PNG,
                                            "media_type": "image/png"}}]}], "a5"),
        _entry("a6", "assistant", [{"type": "tool_use", "id": "t3", "name": "Agent",
               "input": {"description": "look around", "prompt": "find it"}}], "r2"),
        _entry("r3", "user", [{"type": "tool_result", "tool_use_id": "t3",
               "content": "found it"}], "a6"),
        _entry("a7", "assistant", [{"type": "text", "text": "Done. **Fixed**."}], "r3",
               ts="2026-11-20T11:30:00.000Z"),
    ])
    sub = tmp_path / SID / "subagents" / "agent-abc.jsonl"
    _write(sub, [
        _entry("s1", "user", "find it", None, isSidechain=True),
        _entry("s2", "assistant", [{"type": "text", "text": "it is in a.py"}], "s1",
               isSidechain=True, model="claude-haiku-4-5"),
    ])
    (sub.parent / "agent-abc.meta.json").write_text(json.dumps({"toolUseId": "t3"}))
    return path


def _shape(items: list[Any]) -> list[str]:
    return [
        f"tool:{i.name}" if isinstance(i, TranscriptToolCall) else f"{i.role}:{i.text}"
        for i in items
    ]


def test_claude_export_crosses_compaction_and_drops_injected(claude_session: Path):
    t = export_transcript(claude_session, SID)

    assert _shape(t.items) == [
        "user:first prompt",
        "assistant:first reply",
        "user:fix the bug <b>now</b>",
        "thinking:let me look",
        "tool:Edit",
        "tool:Read",
        "tool:Agent",
        "assistant:Done. **Fixed**.",
    ]
    assert t.models == ["claude-opus-5-5", "claude-haiku-4-5"]


def test_claude_export_joins_parallel_results(claude_session: Path):
    edit, read, agent = [i for i in export_transcript(claude_session, SID).items
                         if isinstance(i, TranscriptToolCall)]

    assert edit.output == "The file was updated"
    assert edit.diff == "@@ -3,1 +3,1 @@\n-x = 1\n+x = 2"
    assert read.images == [TranscriptImage("image/png", PNG)]
    assert agent.output == "found it"
    assert _shape(agent.subagent) == ["user:find it", "assistant:it is in a.py"]


def test_claude_export_diffs_a_new_file_from_its_content(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    _write(path, [
        _entry("u", "user", "write it", None),
        _entry("a", "assistant", [{"type": "tool_use", "id": "w", "name": "Write",
               "input": {"file_path": "/p/n.py", "content": "a\nb"}}], "u"),
        _entry("r", "user", [{"type": "tool_result", "tool_use_id": "w",
               "content": "created"}], "a",
               toolUseResult={"type": "create", "structuredPatch": []}),
    ])
    (call,) = [i for i in export_transcript(path, SID).items
               if isinstance(i, TranscriptToolCall)]
    assert call.diff == "+a\n+b"


def test_claude_export_leaves_an_unanswered_call_without_output(tmp_path: Path):
    path = tmp_path / f"{SID}.jsonl"
    _write(path, [
        _entry("u", "user", "run it", None),
        _entry("a", "assistant", [{"type": "tool_use", "id": "b", "name": "Bash",
               "input": {"command": "sleep 100"}}], "u"),
    ])
    (call,) = [i for i in export_transcript(path, SID).items
               if isinstance(i, TranscriptToolCall)]
    assert call.output is None


# ── OpenCode ──


def _oc_server() -> httpx.MockTransport:
    rows = {
        "root": [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1_763_632_800_000}},
             "parts": [
                 {"type": "text", "text": "fix it"},
                 {"type": "text", "text": "<file contents>", "synthetic": True},
                 {"type": "file", "mime": "image/png", "url": f"data:image/png;base64,{PNG}"},
             ]},
            {"info": {"id": "m2", "role": "assistant", "providerID": "openai",
                      "modelID": "gpt-6.1-sol", "time": {"created": 1_763_632_801_000}},
             "parts": [
                 {"type": "step-start"},
                 {"type": "reasoning", "text": "hmm", "time": {"start": 1_763_632_801_000}},
                 {"type": "tool", "tool": "edit", "callID": "c1", "state": {
                     "status": "completed", "input": {"filePath": "/p/a.py"},
                     "output": "Edit applied successfully.", "title": "a.py",
                     "metadata": {"diff": "@@ -1 +1 @@\n-a\n+b"},
                     "time": {"start": 1_763_632_802_000, "end": 1_763_632_803_000}}},
                 {"type": "tool", "tool": "task", "callID": "c2", "state": {
                     "status": "completed", "input": {"description": "dig"},
                     "output": "dug", "title": "dig",
                     "metadata": {"sessionId": "child"},
                     "time": {"start": 1, "end": 2}}},
                 {"type": "tool", "tool": "bash", "callID": "c3", "state": {
                     "status": "error", "input": {"command": "false"},
                     "error": "exit 1", "time": {"start": 1, "end": 2}}},
                 {"type": "text", "text": "Fixed."},
             ]},
            {"info": {"id": "m3", "role": "user", "time": {"created": 3}},
             "parts": [{"type": "compaction", "auto": True}]},
            {"info": {"id": "m4", "role": "assistant", "summary": True,
                      "providerID": "openai", "modelID": "gpt-6.1-sol", "time": {"created": 4}},
             "parts": [{"type": "text", "text": "Summary of everything"}]},
        ],
        "child": [
            {"info": {"id": "k1", "role": "user", "time": {"created": 5}},
             "parts": [{"type": "text", "text": "dig"}]},
            {"info": {"id": "k2", "role": "assistant", "providerID": "openai",
                      "modelID": "gpt-6.1-mini", "time": {"created": 6}},
             "parts": [{"type": "text", "text": "dug up"}]},
        ],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["directory"] == "/p"
        parts = request.url.path.strip("/").split("/")
        if parts[-1] == "message":
            return httpx.Response(200, json=rows[parts[1]])
        return httpx.Response(200, json={"id": parts[1], "title": "Fix it"})

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_opencode_export(monkeypatch: pytest.MonkeyPatch):
    transport = _oc_server()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        oc_export.httpx, "AsyncClient",
        lambda **kw: real(transport=transport, **kw),
    )

    t = await oc_export.export("http://oc", "Basic x", "root", "/p")

    assert t.title == "Fix it"
    assert t.models == ["openai/gpt-6.1-sol", "openai/gpt-6.1-mini"]
    assert _shape(t.items) == [
        "user:fix it", "thinking:hmm", "tool:edit", "tool:task", "tool:bash",
        "assistant:Fixed.",
    ]
    prompt, _, edit, task, bash, _ = t.items
    assert prompt.images == [TranscriptImage("image/png", PNG)]
    assert prompt.timestamp == datetime.fromtimestamp(1_763_632_800, tz=timezone.utc)
    assert edit.diff == "@@ -1 +1 @@\n-a\n+b"
    assert _shape(task.subagent) == ["user:dig", "assistant:dug up"]
    assert bash.is_error and bash.output == "exit 1"


# ── Renderer ──


def _transcript() -> SessionTranscript:
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    return SessionTranscript(
        session_id=SID,
        title="Fix the <parser>",
        models=["claude-opus-5-5"],
        items=[
            TranscriptText("user", "why <script>alert(1)</script>?", t0),
            TranscriptText("thinking", "considering", t0),
            TranscriptToolCall(
                name="Edit", input={"file_path": "/p/a.py"},
                output="ok", diff="@@ -1 +1 @@\n-old\n+new", timestamp=t0,
            ),
            TranscriptToolCall(
                name="Bash", input={"command": "cat </etc/x"}, output="<denied>",
                is_error=True, images=[TranscriptImage("image/png", PNG)],
                subagent=[TranscriptText("user", "nested")],
            ),
            TranscriptText("assistant", "It is **fixed** <img src=x onerror=y>"),
            TranscriptText("user", "thanks", t0.replace(hour=11, minute=5)),
        ],
    )


def test_render_escapes_everything_and_ships_no_script():
    page = share.render_html(_transcript(), agent_name="Claude Code")

    assert "<script" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "<title>Fix the &lt;parser&gt;</title>" in page
    assert "&lt;denied&gt;" in page


def test_render_structure():
    page = share.render_html(_transcript(), agent_name="Claude Code")

    assert "claude-opus-5-5 via Claude Code" in page
    assert "1 h 5 min" in page
    assert page.count('<section class="turn">') == 2
    assert "Turn 1" in page and "Turn 2" in page
    assert '<details class="thinking">' in page
    assert '<details class="tool error">' in page
    assert '<span class="add">+new</span>' in page
    assert '<span class="del">-old</span>' in page
    assert '<span class="hunk">' in page
    assert f'src="data:image/png;base64,{PNG}"' in page
    assert "<strong>fixed</strong>" in page
    assert '<div class="who">Prompt</div>nested' in page


def test_filename_is_an_ascii_slug():
    assert share.share_filename(_transcript()) == "fix-the-parser-11111111.html"
    untitled = SessionTranscript(session_id="ses_ab-cd")
    assert share.share_filename(untitled) == "agent-session-sesabcd.html"
    no_ascii = SessionTranscript(session_id="ses_ab-cd", title="修复解析器")
    assert share.share_filename(no_ascii) == "session-sesabcd.html"


# ── Handler ──


CHAT_ID = 500
SCOPE = ChatScope(chat_id=CHAT_ID, thread_id=11)


@pytest.fixture(autouse=True)
def _isolate_sessions():
    cm._active_sessions.clear()
    yield
    cm._active_sessions.clear()


@pytest.fixture
def db(tmp_path):
    db = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield db
    asyncio.run(db.close())


class _ShareBot(RichBot):
    def __init__(self) -> None:
        super().__init__()
        self.send_document = AsyncMock()


class _StubBackend:
    name = "claude_sdk"
    display_name = "Claude Code"

    def __init__(self) -> None:
        self.export_session = AsyncMock(return_value=_transcript())

    def command_capabilities(self) -> set[str]:
        return {"share"}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubBackend:
    backend = _StubBackend()
    monkeypatch.setattr(cm, "resolve_backend", lambda *a, **k: backend)
    return backend


async def _run(db, bot: _ShareBot) -> None:
    config = Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={"dev": ContextConfig(directory="/tmp", description="d", allowed_tools=[])},
        default_context="dev",
        backend="claude_sdk",
    )
    message = RichMessage("/share", chat_id=CHAT_ID, thread_id=SCOPE.thread_id, bot=bot)
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=CHAT_ID, type="private", PRIVATE="private"),
    )
    context = SimpleNamespace(bot=bot, bot_data={"config": config, "db": db})
    await commands.share_handler(update, context)


@pytest.mark.asyncio
async def test_share_sends_the_page_into_the_topic(db, stub):
    await set_session_id(db, SCOPE, "dev", "claude_sdk", "source-id")
    bot = _ShareBot()

    await _run(db, bot)

    assert stub.export_session.await_args.args == ("source-id", "/tmp")
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["message_thread_id"] == SCOPE.thread_id
    assert kwargs["filename"] == "fix-the-parser-11111111.html"
    assert kwargs["document"].startswith(b"<!DOCTYPE html>")
    assert bot.texts == []


@pytest.mark.asyncio
async def test_share_without_a_session_is_refused(db, stub):
    bot = _ShareBot()

    await _run(db, bot)

    stub.export_session.assert_not_awaited()
    assert "Nothing to share yet" in rendered(bot.texts[0])


@pytest.mark.asyncio
async def test_share_over_the_upload_limit_sends_nothing(db, stub, monkeypatch):
    await set_session_id(db, SCOPE, "dev", "claude_sdk", "source-id")
    monkeypatch.setattr(share, "MAX_SHARE_BYTES", 100)
    bot = _ShareBot()

    await _run(db, bot)

    bot.send_document.assert_not_awaited()
    assert "over the 50 MB" in rendered(bot.texts[0])


@pytest.mark.asyncio
async def test_share_export_failure_is_reported(db, stub):
    await set_session_id(db, SCOPE, "dev", "claude_sdk", "source-id")
    stub.export_session.side_effect = FileNotFoundError("Session source-id not found")
    bot = _ShareBot()

    await _run(db, bot)

    bot.send_document.assert_not_awaited()
    assert "Session source-id not found" in rendered(bot.texts[0])
