"""Tests for ``Backend.command_capabilities`` and ``Backend.copy``.

Each backend declares the set of opt-in commands it implements and the
copy that drives user-facing UI; the bot uses these to gate command
registration and build the Telegram menu.

Asserted here:

* ``claude_sdk`` declares the three opt-in commands (``login``,
  ``usage``, ``mcp``) and carries the strings the bot used to hardcode.
* ``opencode`` declares no opt-in commands and leaves the auth-error
  hint ``None`` so Claude-shaped copy never reaches users.
"""

from __future__ import annotations

from open_shrimp.backend import BackendCopy
from open_shrimp.backend.claude_sdk import ClaudeSdkBackend
from open_shrimp.backend.opencode import OpenCodeBackend


class TestClaudeSdkCapabilities:
    def test_command_capabilities_lists_three_opt_in_commands(self) -> None:
        b = ClaudeSdkBackend()
        assert b.command_capabilities() == {"login", "usage", "mcp"}

    def test_copy_carries_claude_code_strings(self) -> None:
        b = ClaudeSdkBackend()
        copy = b.copy()
        assert isinstance(copy, BackendCopy)
        assert copy.login_command_description == "Re-authenticate Claude Code OAuth"
        assert copy.login_mini_app_body == "Re-authenticate Claude Code OAuth"
        assert copy.auth_error_hint == "Run /login to re-authenticate Claude Code."


class TestOpenCodeCapabilities:
    def test_command_capabilities_has_mcp(self) -> None:
        b = OpenCodeBackend()
        assert b.command_capabilities() == {"mcp"}

    def test_copy_auth_error_hint_is_none(self) -> None:
        """``None`` means the auth-error rendering site is skipped —
        OpenCode does not have a Claude-shaped ``/login`` flow to hint at."""
        b = OpenCodeBackend()
        copy = b.copy()
        assert isinstance(copy, BackendCopy)
        assert copy.auth_error_hint is None

    def test_copy_login_strings_present_for_future_opt_in(self) -> None:
        """Non-``None`` Mini-App and command-description strings so a
        future OpenCode-side login flow can flip ``"login"`` into
        capabilities without re-touching the copy site."""
        b = OpenCodeBackend()
        copy = b.copy()
        assert copy.login_command_description
        assert copy.login_mini_app_body
