from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from open_shrimp.config import ContextConfig, SandboxConfig
from open_shrimp.db import ChatScope
from open_shrimp.handlers import commands
from open_shrimp.opencode_client import SessionInfo


pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class FakeServer:
    base_url: str = "http://127.0.0.1:4096"
    auth_header: str = "Bearer sandbox"


class FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_environment(self) -> None:
        self.calls.append("ensure_environment")

    def ensure_running(self) -> None:
        self.calls.append("ensure_running")

    def provision_workspace(self) -> None:
        self.calls.append("provision_workspace")

    def ensure_opencode_server(self) -> FakeServer:
        self.calls.append("ensure_opencode_server")
        return FakeServer()


class FakeManager:
    def __init__(self, opencode_home: Path, sandbox: FakeSandbox) -> None:
        self._opencode_home = opencode_home
        self.sandbox = sandbox
        self.created = False

    def opencode_home_dir(self, context_name: str) -> Path:
        return self._opencode_home / context_name

    def create_sandbox(self, context_name: str, context: ContextConfig) -> FakeSandbox:
        self.created = True
        return self.sandbox


def _sandbox_context(directory: Path) -> ContextConfig:
    return ContextConfig(
        directory=str(directory),
        description="sandbox",
        allowed_tools=[],
        sandbox=SandboxConfig(backend="docker"),
    )


async def test_sandbox_resume_listing_uses_sandbox_opencode_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _sandbox_context(tmp_path / "repo")
    home_base = tmp_path / "state"
    (home_base / "sandboxed").mkdir(parents=True)
    sandbox = FakeSandbox()
    manager = FakeManager(home_base, sandbox)
    seen: dict[str, Any] = {}

    async def fake_list_sessions(
        directory: str,
        *,
        limit: int = 500,
        base_url: str | None = None,
        auth_header: str | None = None,
    ) -> list[SessionInfo]:
        seen.update(
            directory=directory,
            limit=limit,
            base_url=base_url,
            auth_header=auth_header,
        )
        return [SessionInfo("ses_1", "sandbox session", 10)]

    monkeypatch.setattr(commands, "list_sessions", fake_list_sessions)

    sessions = await commands._list_sessions_for_context(
        "sandboxed",
        ctx,
        sandbox_manager=manager,
        limit=1,
    )

    assert [s.session_id for s in sessions] == ["ses_1"]
    assert manager.created is True
    assert sandbox.calls == [
        "ensure_environment",
        "ensure_running",
        "provision_workspace",
        "ensure_opencode_server",
    ]
    assert seen == {
        "directory": str(tmp_path / "repo"),
        "limit": 1,
        "base_url": "http://127.0.0.1:4096",
        "auth_header": "Bearer sandbox",
    }


async def test_sandbox_resume_listing_skips_uninitialized_sandbox_state(
    tmp_path: Path,
) -> None:
    ctx = _sandbox_context(tmp_path / "repo")
    sandbox = FakeSandbox()
    manager = FakeManager(tmp_path / "missing-state", sandbox)

    sessions = await commands._list_sessions_for_context(
        "sandboxed",
        ctx,
        sandbox_manager=manager,
    )

    assert sessions == []
    assert manager.created is False
    assert sandbox.calls == []


class FakeMessage:
    text = "/resume ses_target"

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class FakeUpdate:
    effective_user = type("User", (), {"id": 1})()

    def __init__(self) -> None:
        self.effective_message = FakeMessage()


class FakeContext:
    def __init__(self) -> None:
        self.bot = object()
        self.bot_data: dict[str, Any] = {
            "config": object(),
            "db": object(),
        }


async def test_direct_resume_cancels_running_task_before_switching(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _sandbox_context(tmp_path / "repo")
    update = FakeUpdate()
    context = FakeContext()
    scope = ChatScope(1, 123)
    calls: list[str] = []

    monkeypatch.setattr(commands, "_is_authorized", lambda user_id, config: True)
    monkeypatch.setattr(commands, "chat_scope_from_message", lambda message: scope)

    async def fake_get_context(
        scope_arg: ChatScope, config: Any, db: Any,
    ) -> tuple[str, ContextConfig]:
        assert scope_arg == scope
        return "sandboxed", ctx

    async def fake_list_sessions_for_context(*args: Any, **kwargs: Any) -> list[SessionInfo]:
        return [SessionInfo("ses_target", "target title", 10)]

    async def fake_stop_runner(scope_arg: ChatScope) -> None:
        assert scope_arg == scope
        calls.append("stop_runner")

    async def fake_close_session(scope_arg: ChatScope) -> None:
        assert scope_arg == scope
        calls.append("close")

    async def fake_set_session_id(
        db: Any, scope_arg: ChatScope, ctx_name: str, session_id: str,
    ) -> None:
        assert scope_arg == scope
        assert ctx_name == "sandboxed"
        assert session_id == "ses_target"
        calls.append("set")

    async def fake_update_pinned_status(*args: Any, **kwargs: Any) -> None:
        calls.append("pin")

    monkeypatch.setattr(commands, "_get_context", fake_get_context)
    monkeypatch.setattr(commands, "_select_sandbox_manager_for_context", lambda ctx, context: None)
    monkeypatch.setattr(commands, "_list_sessions_for_context", fake_list_sessions_for_context)
    monkeypatch.setattr(commands, "stop_runner", fake_stop_runner)
    monkeypatch.setattr(commands, "close_session", fake_close_session)
    monkeypatch.setattr(commands, "set_session_id", fake_set_session_id)
    monkeypatch.setattr(commands, "_update_pinned_status", fake_update_pinned_status)

    await commands.resume_handler(update, context)  # type: ignore[arg-type]

    assert calls == ["stop_runner", "close", "set", "pin"]
    assert update.effective_message.replies
