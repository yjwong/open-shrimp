from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import open_shrimp.handlers.approval as approval_module
from open_shrimp.db import ChatScope
from open_shrimp.handlers.approval import (
    _send_approval_keyboard,
    handle_approval_callback,
)
from open_shrimp.handlers.state import _approval_futures
from open_shrimp.handlers.state import _pending_tool_approvals
from open_shrimp.hooks import ApprovalDecision
from open_shrimp.stream import _SUPPRESS_NOTIFICATION_TOOLS


class _FakeBot:
    def __init__(self) -> None:
        self.reply_markup: Any = None

    async def send_message(self, **kwargs: Any) -> Any:
        self.reply_markup = kwargs.get("reply_markup")
        return SimpleNamespace(message_id=123)


@pytest.mark.asyncio
async def test_apply_patch_keyboard_omits_generic_accept_all(tmp_path) -> None:
    bot = _FakeBot()
    tool_use_id = "tu_apply_patch"
    task = asyncio.create_task(
        _send_approval_keyboard(
            bot=bot,  # type: ignore[arg-type]
            chat_id=1,
            tool_name="ApplyPatch",
            tool_input={
                "patchText": "\n".join((
                    "*** Begin Patch",
                    "*** Update File: a.py",
                    "@@",
                    "-old",
                    "+new",
                    "*** End Patch",
                )),
            },
            tool_use_id=tool_use_id,
            cwd=str(tmp_path),
        ),
    )

    try:
        while bot.reply_markup is None:
            await asyncio.sleep(0)

        labels = [
            button.text
            for row in bot.reply_markup.inline_keyboard
            for button in row
        ]
        assert "Accept all edits" in labels
        assert "Allow all ApplyPatch this session" not in labels
        assert "Always allow ApplyPatch" not in labels

        _approval_futures[f"approve:{tool_use_id}"].set_result(False)
        result = await task
        assert isinstance(result, ApprovalDecision)
        assert not result.approved
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for key in list(_approval_futures):
            if tool_use_id in key:
                _approval_futures.pop(key, None)


def test_apply_patch_stream_notification_is_suppressed() -> None:
    assert "ApplyPatch" in _SUPPRESS_NOTIFICATION_TOOLS


@pytest.mark.asyncio
async def test_skill_keyboard_uses_broad_full_width_buttons() -> None:
    bot = _FakeBot()
    tool_use_id = "tu_skill"
    task = asyncio.create_task(
        _send_approval_keyboard(
            bot=bot,  # type: ignore[arg-type]
            chat_id=1,
            tool_name="skill",
            tool_input={"name": "frontend-patterns"},
            tool_use_id=tool_use_id,
            always_patterns=["frontend-patterns"],
        ),
    )

    try:
        while bot.reply_markup is None:
            await asyncio.sleep(0)

        rows = [
            [button.text for button in row]
            for row in bot.reply_markup.inline_keyboard
        ]
        labels = [label for row in rows for label in row]
        assert "Always allow: frontend-patterns" not in labels
        assert ["Allow all skill this session"] in rows
        assert ["Always allow skill"] in rows

        _approval_futures[f"approve:{tool_use_id}"].set_result(False)
        result = await task
        assert isinstance(result, ApprovalDecision)
        assert not result.approved
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for key in list(_approval_futures):
            if tool_use_id in key:
                _approval_futures.pop(key, None)


@pytest.mark.asyncio
async def test_always_allow_tool_remembers_without_session_patch(monkeypatch) -> None:
    scope = ChatScope(chat_id=1, thread_id=2)
    token = "skill_token"
    data = f"accept_all_tool_always:{token}"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool | ApprovalDecision] = loop.create_future()
    tasks: list[asyncio.Task[None]] = []

    class _FakeMessage:
        chat_id = scope.chat_id
        message_thread_id = scope.thread_id
        text_markdown_v2 = "Tool: skill"
        text = "Tool: skill"

        async def edit_text(self, **kwargs: Any) -> None:
            return None

        async def edit_reply_markup(self, **kwargs: Any) -> None:
            return None

    class _FakeQuery:
        message = _FakeMessage()

        async def answer(self, *args: Any, **kwargs: Any) -> None:
            return None

        def get_bot(self) -> Any:
            return SimpleNamespace(edit_message_text=lambda **kwargs: None)

    class _FakeApplication:
        def create_task(self, coro: Any) -> asyncio.Task[None]:
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

    async def fake_get_context(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        return "ctx", SimpleNamespace()

    async def fake_sleep(delay: float) -> None:
        return None

    async def fake_patch_project_tool_permission(
        patch_scope: ChatScope, tool_name: str,
    ) -> bool:
        assert patch_scope == scope
        assert tool_name == "skill"
        return True

    async def fail_patch_active_session_rules(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("active session rules should not be patched")

    monkeypatch.setattr("open_shrimp.handlers.utils._get_context", fake_get_context)
    monkeypatch.setattr(approval_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        approval_module,
        "_patch_project_tool_permission",
        fake_patch_project_tool_permission,
    )
    monkeypatch.setattr(
        approval_module,
        "_patch_active_session_rules",
        fail_patch_active_session_rules,
    )

    _approval_futures[data] = future
    _pending_tool_approvals[token] = "skill"
    try:
        handled = await handle_approval_callback(
            _FakeQuery(),
            data,
            SimpleNamespace(),
            SimpleNamespace(
                bot_data={"db": object()},
                application=_FakeApplication(),
            ),
        )
        assert handled is True
        result = future.result()
        assert isinstance(result, ApprovalDecision)
        assert result.approved is True
        assert result.remember is True
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        _approval_futures.pop(data, None)
        _pending_tool_approvals.pop(token, None)
        from open_shrimp.handlers.state import _tool_approved_sessions

        _tool_approved_sessions.pop((scope, "ctx"), None)


@pytest.mark.asyncio
async def test_bash_keyboard_keeps_pattern_always_only() -> None:
    bot = _FakeBot()
    tool_use_id = "tu_bash"
    task = asyncio.create_task(
        _send_approval_keyboard(
            bot=bot,  # type: ignore[arg-type]
            chat_id=1,
            tool_name="Bash",
            tool_input={"command": "git status", "description": "Check status"},
            tool_use_id=tool_use_id,
            always_patterns=["git status *"],
        ),
    )

    try:
        while bot.reply_markup is None:
            await asyncio.sleep(0)

        labels = [
            button.text
            for row in bot.reply_markup.inline_keyboard
            for button in row
        ]
        assert "Always allow: git status *" in labels
        assert "Allow all Bash this session" not in labels
        assert "Always allow Bash" not in labels

        _approval_futures[f"approve:{tool_use_id}"].set_result(False)
        result = await task
        assert isinstance(result, ApprovalDecision)
        assert not result.approved
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for key in list(_approval_futures):
            if tool_use_id in key:
                _approval_futures.pop(key, None)
