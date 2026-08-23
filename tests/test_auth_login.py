"""The one answer to "is Claude signed in here", and the CLI that hands it out.

Three wizards and the boot-time readiness card ask this question, and only one
of them can call Python directly.  What is pinned here is the wire contract the
two GUI wizards decode and the precedence behind it: an operator whose
environment holds a key is signed in whatever the credential file says, because
that key is what the CLI would actually use.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_shrimp.backend.claude_sdk import login as login_mod
from open_shrimp.backend.claude_sdk.login import (
    AuthStatus,
    auth_status,
    run_interactive_login,
)
from open_shrimp.main import _run_auth_login, _run_auth_status


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process holding neither credential variable.

    The machine the tests run on is a developer's, and theirs may hold either.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def _on_disk(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    monkeypatch.setattr(
        "open_shrimp.backend.claude_sdk.cred_watcher.host_signed_in",
        lambda: present,
    )


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A managed data directory of this test's own, so a sign-in that creates
    its workspace creates it there and not in the developer's."""
    monkeypatch.setattr(login_mod.paths, "data_dir", lambda: tmp_path)
    return tmp_path


# -- What the answer is ------------------------------------------------------


class TestAuthStatus:
    def test_credentials_on_disk_are_oauth(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on_disk(monkeypatch, True)

        assert auth_status() == AuthStatus(True, "oauth")

    def test_an_api_key_is_named_as_one(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
        _on_disk(monkeypatch, False)

        assert auth_status() == AuthStatus(True, "api_key")

    def test_an_oauth_token_in_the_environment_is_named_as_one(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """How a headless install is set up, and it is not the same thing as
        an API key — a UI offering to "sign out" of one must not offer it for
        a variable it cannot unset."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-whatever")
        _on_disk(monkeypatch, False)

        assert auth_status() == AuthStatus(True, "env_token")

    def test_nothing_anywhere_is_signed_out(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``how`` is null rather than empty, so a UI can switch on it without
        a sentinel string."""
        _on_disk(monkeypatch, False)

        assert auth_status() == AuthStatus(False, None)

    @pytest.mark.parametrize(
        ("var", "how"),
        [("ANTHROPIC_API_KEY", "api_key"), ("CLAUDE_CODE_OAUTH_TOKEN", "env_token")],
    )
    def test_the_environment_beats_the_credential_file(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch, var: str, how: str
    ) -> None:
        """Both present is the common case on a developer's own machine, and
        the variable is what the CLI would actually authenticate with — so
        naming the file would name a credential that is never used."""
        monkeypatch.setenv(var, "whatever")
        _on_disk(monkeypatch, True)

        assert auth_status() == AuthStatus(True, how)

    def test_an_api_key_beats_an_oauth_token(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-whatever")

        assert auth_status() == AuthStatus(True, "api_key")


# -- Running the sign-in -----------------------------------------------------


class TestInteractiveLogin:
    def test_the_child_gets_this_terminal_and_no_browser_override(
        self, no_env: None, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inherited stdio, so the CLI draws its own prompts, and no BROWSER:
        the override elsewhere exists only where there is no desktop to open a
        window on."""
        calls: list[tuple] = []
        monkeypatch.setattr(login_mod, "find_claude_binary", lambda: "/opt/claude")
        monkeypatch.setattr(
            login_mod.subprocess,
            "run",
            lambda argv, **kwargs: calls.append((argv, kwargs)),
        )
        _on_disk(monkeypatch, True)

        assert run_interactive_login() is True
        (argv, kwargs), = calls
        assert argv == ["/opt/claude", "/login"]
        assert set(kwargs) == {"check", "cwd"}

    def test_the_child_starts_in_a_directory_of_its_own(
        self, no_env: None, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never an inherited working directory.  The CLI asks the user to
        vouch for the directory it starts in the first time it sees one, and
        inherited that is wherever the core was launched from — the folder an
        installer happened to run in, which the user is then asked about
        instead of being asked to sign in."""
        calls: list[tuple] = []
        monkeypatch.setattr(login_mod, "find_claude_binary", lambda: "/opt/claude")
        monkeypatch.setattr(
            login_mod.subprocess,
            "run",
            lambda argv, **kwargs: calls.append((argv, kwargs)),
        )
        _on_disk(monkeypatch, True)

        run_interactive_login()

        (_, kwargs), = calls
        assert kwargs["cwd"] == data_dir / "login"
        assert kwargs["cwd"].is_dir()

    def test_the_answer_is_the_credentials_after_the_child_exits(
        self, no_env: None, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the exit code: the CLI exits zero on a sign-in the user backed
        out of, and the only thing that settles it is what is on disk."""
        monkeypatch.setattr(login_mod, "find_claude_binary", lambda: "/opt/claude")
        monkeypatch.setattr(login_mod.subprocess, "run", lambda argv, **kw: None)
        _on_disk(monkeypatch, False)

        assert run_interactive_login() is False

    def test_ctrl_c_is_giving_up_not_a_crash(
        self, no_env: None, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wizard calls this before its config is written, so unwinding
        out of an abandoned sign-in would cost a whole setup."""

        def _interrupted(argv: list[str], **kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(login_mod, "find_claude_binary", lambda: "/opt/claude")
        monkeypatch.setattr(login_mod.subprocess, "run", _interrupted)
        _on_disk(monkeypatch, False)

        assert run_interactive_login() is False

    def test_no_cli_anywhere_raises_the_search_it_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _nothing() -> str:
            raise RuntimeError("Could not find the Claude CLI binary. Searched: …")

        monkeypatch.setattr(login_mod, "find_claude_binary", _nothing)

        with pytest.raises(RuntimeError, match="Could not find"):
            run_interactive_login()


# -- The CLI a GUI wizard shells out to --------------------------------------


class TestAuthStatusCommand:
    def _emitted(self, capsys: pytest.CaptureFixture[str]) -> dict:
        return json.loads(capsys.readouterr().out)

    def test_signed_in_is_the_documented_object(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _on_disk(monkeypatch, True)

        assert _run_auth_status(json_output=True) == 0
        assert self._emitted(capsys) == {
            "ok": True,
            "signed_in": True,
            "how": "oauth",
        }

    def test_signed_out_still_exits_zero(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A wizard renders "not signed in" as the step it is about to offer.
        Exiting non-zero for it would make that step indistinguishable from a
        check that could not run."""
        _on_disk(monkeypatch, False)

        assert _run_auth_status(json_output=True) == 0
        assert self._emitted(capsys) == {
            "ok": True,
            "signed_in": False,
            "how": None,
        }

    def test_a_check_that_could_not_run_says_so_and_exits_one(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        def _boom() -> bool:
            raise OSError("the keychain is locked")

        _on_disk(monkeypatch, False)
        monkeypatch.setattr(
            "open_shrimp.backend.claude_sdk.cred_watcher.host_signed_in", _boom
        )

        assert _run_auth_status(json_output=True) == 1
        emitted = self._emitted(capsys)
        assert emitted["ok"] is False
        assert "keychain" in emitted["error"]

    def test_the_text_form_names_which_credential(
        self, no_env: None, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

        assert _run_auth_status(json_output=False) == 0
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


class TestAuthLoginCommand:
    def test_a_finished_sign_in_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(login_mod, "run_interactive_login", lambda: True)

        assert _run_auth_login(hold=False) == 0
        assert "Signed in" in capsys.readouterr().out

    def test_an_abandoned_sign_in_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(login_mod, "run_interactive_login", lambda: False)

        assert _run_auth_login(hold=False) == 1
        assert "/login" in capsys.readouterr().out

    def test_no_cli_is_a_sentence_and_an_exit_code_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The message names every path it searched, which is what a
        GUI-spawned console shows."""

        def _nothing() -> bool:
            raise RuntimeError("Could not find the Claude CLI binary. Searched: …")

        monkeypatch.setattr(login_mod, "run_interactive_login", _nothing)

        assert _run_auth_login(hold=False) == 1
        assert "Could not find the Claude CLI binary" in capsys.readouterr().err

    def test_hold_waits_before_the_window_closes(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A console a GUI spawned takes its last line with it when the child
        exits, including the line saying the sign-in did not work."""
        waited: list[str] = []
        monkeypatch.setattr(login_mod, "run_interactive_login", lambda: False)
        monkeypatch.setattr("builtins.input", lambda prompt="": waited.append(prompt))

        assert _run_auth_login(hold=True) == 1
        assert waited and "Enter" in waited[0]

    def test_hold_survives_a_console_with_no_keyboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _closed(prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr(login_mod, "run_interactive_login", lambda: True)
        monkeypatch.setattr("builtins.input", _closed)

        assert _run_auth_login(hold=True) == 0
