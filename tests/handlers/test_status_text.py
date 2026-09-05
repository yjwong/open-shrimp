"""Layout of the pinned status message.

Every row of the card is a separate fact — context, description, directory,
model, context size, cost — and a rich message collapses a bare newline into a
space, so the rows have to carry explicit breaks or they run together.
"""

from __future__ import annotations

import pytest

from open_shrimp.config import Config, ContextConfig, TelegramConfig
from open_shrimp.handlers.status_render import build_pinned_status


def _config(ctx: ContextConfig) -> Config:
    return Config(
        telegram=TelegramConfig(token="test"),
        allowed_users=[1],
        contexts={"default": ctx},
        default_context="default",
    )


def _ctx(**overrides: object) -> ContextConfig:
    fields: dict[str, object] = {
        "directory": "/home/u/project",
        "description": "OpenShrimp development",
        "allowed_tools": [],
        "model": "claude-opus-5",
    }
    fields.update(overrides)
    return ContextConfig(**fields)  # type: ignore[arg-type]


def test_rows_carry_explicit_breaks() -> None:
    ctx = _ctx(effort="high")
    status = build_pinned_status(
        "default", ctx, _config(ctx),
        model_usage={"claude-opus-5": {
            "costUSD": 9.9856, "contextWindow": 1_000_000,
        }},
        turn_usage={"input_tokens": 664_600},
    )

    assert "`default`<br>OpenShrimp development" in status
    assert "`/home/u/project`<br>" in status
    assert "`claude-opus-5`<br>" in status
    assert "**Effort:** `high`" in status
    assert "(66%)<br>" in status
    assert "$9.9856" in status
    # The description, the paths and the usage are three separate blocks.
    assert status.count("\n\n") == 2


def test_tasks_stay_a_list() -> None:
    """Checklist items are list rows, not <br>-joined text."""
    ctx = _ctx()
    status = build_pinned_status(
        "default", ctx, _config(ctx),
        todos=[
            {"content": "Write it", "status": "completed"},
            {"content": "Ship it", "status": "in_progress"},
        ],
    )

    assert "- [x] ~~Write it~~" in status
    assert "- [ ] **Ship it**" in status
    assert "<br>- [" not in status


@pytest.mark.parametrize("description", ["", "Two\nlines"])
def test_no_usage_block_without_usage(description: str) -> None:
    ctx = _ctx(description=description)
    status = build_pinned_status("default", ctx, _config(ctx))

    assert "Context:" not in status
    assert status.count("\n\n") == 1
