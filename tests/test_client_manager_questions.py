from __future__ import annotations

from typing import Any

import pytest

from open_shrimp.client_manager import CallbackContext, _make_opencode_questions_proxy


@pytest.mark.asyncio
async def test_opencode_question_proxy_normalizes_multiple_flag() -> None:
    received: list[dict[str, Any]] = []

    async def handle_questions(
        questions: list[dict[str, Any]],
    ) -> dict[str, str]:
        received.extend(questions)
        return {"Pick several": "Alpha, Gamma"}

    proxy = _make_opencode_questions_proxy(
        CallbackContext(handle_user_questions=handle_questions)
    )
    answers = await proxy([
        {
            "question": "Pick several",
            "options": [{"label": "Alpha"}, {"label": "Gamma"}],
            "multiple": True,
        }
    ])

    assert received[0]["multiSelect"] is True
    assert answers == [["Alpha", "Gamma"]]


@pytest.mark.asyncio
async def test_opencode_question_proxy_keeps_single_answer_nested() -> None:
    async def handle_questions(
        questions: list[dict[str, Any]],
    ) -> dict[str, str]:
        assert questions[0]["multiSelect"] is False
        return {"Pick one": "Beta"}

    proxy = _make_opencode_questions_proxy(
        CallbackContext(handle_user_questions=handle_questions)
    )
    answers = await proxy([{"question": "Pick one", "multiple": False}])

    assert answers == [["Beta"]]
