"""What a Bash card's summary row says.

The row is what the user reads when the card is collapsed, so it carries the
agent's description of the command; the command text itself is in the body.
"""

from __future__ import annotations

from open_shrimp.tool_cards import bash_card, bash_summary


def test_description_replaces_the_command_on_the_row() -> None:
    row = bash_summary(
        {"command": "git add -A && git commit -q -F - <<'EOF'", "description": "Commit"},
        "💻", "Bash", elapsed=1.5,
    )

    assert row == "💻 **Bash** — Commit · 1.5s"


def test_command_is_the_fallback_when_no_description_came() -> None:
    row = bash_summary({"command": "pytest  -q"}, "💻", "Bash")

    assert row == "💻 **Bash** — `pytest -q`"


def test_long_fallback_command_is_clipped() -> None:
    row = bash_summary({"command": "echo " + "x" * 200}, "💻", "Bash")

    assert row.endswith("…`")
    assert len(row) < 120


def test_the_card_body_keeps_the_full_command() -> None:
    command = "git add -A && git commit -q -F -"
    card = bash_card(
        {"command": command, "description": "Commit"}, "💻", "Bash", open=False,
    )

    assert "— Commit" in card
    assert command in card


def test_failure_and_elapsed_ride_along_with_the_description() -> None:
    row = bash_summary(
        {"command": "pytest", "description": "Run tests"},
        "💻", "Bash", elapsed=82.0, is_error=True,
    )

    assert row == "💻 **Bash** — Run tests · **failed** · 1m22s"
