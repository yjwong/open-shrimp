from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from open_shrimp.handlers.approval import _send_approval_keyboard
from open_shrimp.handlers.state import _approval_futures
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
