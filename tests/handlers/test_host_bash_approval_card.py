"""The host-escape approval card, before and after the decision.

The command is the decision, so the prompt spells it out; once decided the
card is one row the user scrolls past, with the command folded away.
"""

from __future__ import annotations

from open_shrimp.handlers.approval import (
    _format_host_bash_approval,
    _format_host_bash_final,
)

TOOL_INPUT = {
    "command": "systemctl --user restart open-shrimp",
    "description": "Restart the bot",
}


def test_the_prompt_spells_out_the_command() -> None:
    text = _format_host_bash_approval(TOOL_INPUT, remaining=30.0)

    assert "HOST shell" in text
    assert "Restart the bot" in text
    assert "systemctl --user restart open-shrimp" in text
    assert "Auto-deny in 30s" in text


def test_the_decided_card_folds_the_command_away() -> None:
    text = _format_host_bash_final(TOOL_INPUT, "approved")

    summary, _, body = text.partition("</summary>")
    assert summary == (
        "<details><summary>✅ **HOST shell** — Approved · Restart the bot"
    )
    assert "systemctl --user restart open-shrimp" in body


def test_a_denied_call_keeps_the_command_a_tap_away() -> None:
    text = _format_host_bash_final({"command": "rm -rf /"}, "denied")

    assert text.startswith("<details><summary>❌ **HOST shell** — Denied<")
    assert "rm -rf /" in text
