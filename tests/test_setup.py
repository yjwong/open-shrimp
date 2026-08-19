"""Tests for the setup wizard.

The wizard writes the first entry in ``allowed_users``, which is the only auth
boundary in the product, so most of what is asserted here is about *who* ends
up in it rather than about the YAML that comes out.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from open_shrimp import setup as setup_mod
from open_shrimp.setup import _path_completer, _validate_directory, run_setup_wizard
from tests.telegram_stub import FakeTelegram, wait_for

OPERATOR = 424242

# What a ``codes`` entry may be: the text to type, or something handed the
# wizard that decides on the spot — which is how a test waits for the bot to
# send before reading the code back off it.
Answer = str | Callable[["_Wizard"], str]


def _code_for(user_id: int, *, after: int = 1) -> Callable[[_Wizard], str]:
    """Type the code the bot actually sent ``user_id``.

    ``after`` is how many sends to wait for first, which is how a test names
    the second code without racing the poll that delivers it.
    """

    def _answer(wizard: _Wizard) -> str:
        wait_for(lambda: len(wizard.fake.sent) >= after)
        return wizard.fake.code_sent_to(user_id)

    return _answer


def _typed(text: str, *, after: int = 1) -> Callable[[_Wizard], str]:
    """Type something of the test's choosing, once the bot has sent."""

    def _answer(wizard: _Wizard) -> str:
        wait_for(lambda: len(wizard.fake.sent) >= after)
        return text

    return _answer


class _Wizard:
    """Drives ``run_setup_wizard`` against a fake Telegram.

    ``answers`` are consumed in order for every prompt except three: the
    token, which comes from ``tokens``; the code, which comes from ``codes``;
    and the autostart offer, which comes from ``autostart`` so that a test
    saying nothing about it is not made to.  ``codes`` defaults to answering
    every prompt with the code the operator actually received — the handshake
    is exercised, not stubbed.
    """

    def __init__(
        self,
        fake: FakeTelegram,
        *answers: str,
        operator: int = OPERATOR,
        tokens: tuple[str, ...] = ("111:AAA-bbb",),
        codes: tuple[Answer, ...] = (),
        clients: tuple[Callable[[], object], ...] = (),
        autostart: str = "n",
        installed: bool = False,
    ):
        self.fake = fake
        self.operator = operator
        self.code_prompts = 0
        self.prompts: list[str] = []
        self._answers = iter(answers)
        self._tokens = list(tokens)
        self._codes = list(codes)
        self._clients = list(clients)
        self._autostart = autostart
        self._installed = installed

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if prompt.startswith("Telegram bot token") and self._tokens:
            return self._tokens.pop(0)
        if prompt.startswith("Keep OpenShrimp running"):
            return self._autostart
        if prompt.startswith("Setup code"):
            self.code_prompts += 1
            answer = self._codes.pop(0) if self._codes else _code_for(self.operator)
            return answer(self) if callable(answer) else answer
        return next(self._answers)

    def _client(self):
        return (self._clients.pop(0) if self._clients else self.fake.client)()

    def run(self, config_path: Path) -> None:
        # Whether autostart is already registered decides whether the wizard
        # asks about it at all, so it is answered here rather than left to the
        # machine the tests happen to run on.
        with patch("builtins.input", side_effect=self):
            with patch.object(setup_mod, "_open_client", self._client):
                with patch(
                    "open_shrimp.service.is_service_installed",
                    lambda name: self._installed,
                ):
                    run_setup_wizard(config_path)


def _fake_with_operator(**kwargs) -> FakeTelegram:
    """A bot whose only in-window message comes from the operator."""
    fake = FakeTelegram(**kwargs)
    fake.deliver_on_poll(1, user_id=OPERATOR, username="ada_l")
    return fake


class TestEnrollment:
    """The handshake, end to end through the wizard."""

    def test_the_code_enrolls_the_person_who_received_it(self, tmp_path: Path) -> None:
        fake = _fake_with_operator()
        wizard = _Wizard(fake, "y", "myproject", "/tmp", "My project", "4")
        wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]
        assert raw["telegram"]["token"] == "111:AAA-bbb"

    def test_the_backlog_never_receives_a_setup_code(self, tmp_path: Path) -> None:
        """Telegram queues updates for a day; the backlog is not a candidate."""
        fake = _fake_with_operator()
        fake.queue_message(user_id=666, username="stranger")

        wizard = _Wizard(fake, "y", "default", "/tmp", "test", "1")
        wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]
        assert {chat for chat, _ in fake.sent} == {OPERATOR}

    def test_a_wrong_code_does_not_enroll_and_does_not_end_the_window(
        self, tmp_path: Path
    ) -> None:
        fake = _fake_with_operator()

        wizard = _Wizard(
            fake,
            "y",
            "default",
            "/tmp",
            "test",
            "1",
            codes=(_typed("000000"), _code_for(OPERATOR)),
        )
        wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]
        assert wizard.code_prompts == 2

    def test_declining_the_confirmation_leaves_the_allowlist_untouched(
        self, tmp_path: Path
    ) -> None:
        """A decline reopens the window; the spent code cannot be retyped."""
        fake = FakeTelegram()
        fake.deliver_on_poll(1, user_id=OPERATOR, username="ada_l")
        fake.deliver_on_poll(3, user_id=OPERATOR + 1, username="bob")

        wizard = _Wizard(
            fake,
            "n",
            "y",
            "default",
            "/tmp",
            "test",
            "1",
            codes=(_code_for(OPERATOR), _code_for(OPERATOR + 1, after=2)),
        )
        wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR + 1]

    def test_the_deep_link_nonce_skips_the_code_entry(self, tmp_path: Path) -> None:
        fake = FakeTelegram()
        real_window = setup_mod.enrollment.EnrollmentWindow

        def _capture(*args, **kwargs):
            """Have the operator open the link the wizard just generated."""
            window = real_window(*args, **kwargs)
            fake.deliver_on_poll(
                1,
                user_id=OPERATOR,
                username="ada_l",
                text=f"/start {window.nonce}",
            )
            return window

        def _press_enter(wizard: _Wizard) -> str:
            """Pressing Enter is all the deep-link path asks for."""
            wait_for(lambda: wizard.fake.polls >= 2)
            return ""

        wizard = _Wizard(fake, "y", "default", "/tmp", "test", "1", codes=(_press_enter,))
        with patch.object(setup_mod.enrollment, "EnrollmentWindow", _capture):
            wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]
        # No code was ever issued on this path.
        assert not any("Setup code" in text for _, text in fake.sent)

    def test_the_wizard_hands_back_a_drained_queue(self, tmp_path: Path) -> None:
        """The core's first poll must replay nothing the wizard consumed."""
        fake = _fake_with_operator()
        wizard = _Wizard(fake, "y", "default", "/tmp", "test", "1")
        wizard.run(tmp_path / "config.yaml")

        assert fake.pending == []

    def test_the_last_word_is_a_promise_not_an_explanation(
        self, tmp_path: Path
    ) -> None:
        """Orientation belongs on first boot, when the bot can act on it."""
        fake = _fake_with_operator()
        wizard = _Wizard(fake, "y", "default", "/tmp", "test", "1")
        wizard.run(tmp_path / "config.yaml")

        final = fake.sent[-1][1]
        assert "You're all set" in final
        assert "/context" not in final

    def test_every_word_lands_in_the_thread_it_was_asked_from(
        self, tmp_path: Path
    ) -> None:
        """Both the code and the sign-off, or the operator is left looking at a
        conversation the bot never spoke into."""
        fake = FakeTelegram()
        fake.deliver_on_poll(1, user_id=OPERATOR, username="ada_l", thread_id=931)

        wizard = _Wizard(fake, "y", "default", "/tmp", "test", "1")
        wizard.run(tmp_path / "config.yaml")

        assert fake.threads == [931, 931]

    def test_an_expired_window_offers_a_fresh_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A wizard left open on a desk overnight must not still be enrollable
        in the morning — but it must still be usable."""
        fake = FakeTelegram()
        clock = [0.0]
        real_window = setup_mod.enrollment.EnrollmentWindow
        windows: list[object] = []

        def _capture(*args, **kwargs):
            kwargs["clock"] = lambda: clock[0]
            window = real_window(*args, **kwargs)
            windows.append(window)
            if len(windows) == 2:
                # The second window is the one that gets a candidate.
                fake.deliver_on_poll(fake.polls + 1, user_id=OPERATOR, username="ada_l")
            return window

        def _leave_it_overnight(_: _Wizard) -> str:
            clock[0] += setup_mod.enrollment.WINDOW_SECONDS + 1
            return ""

        wizard = _Wizard(
            fake, "y", "y", "default", "/tmp", "test", "1", codes=(_leave_it_overnight,)
        )
        with patch.object(setup_mod.enrollment, "EnrollmentWindow", _capture):
            wizard.run(tmp_path / "config.yaml")

        out = capsys.readouterr().out
        assert "The setup window closed" in out
        assert len(windows) == 2
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]

    def test_a_flood_is_surfaced_not_swallowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = FakeTelegram()
        for i in range(1, 5):
            fake.deliver_on_poll(i, user_id=100 + i, username=f"u{i}")
        fake.deliver_on_poll(5, user_id=OPERATOR, username="ada_l")

        wizard = _Wizard(
            fake, "y", "default", "/tmp", "test", "1", codes=(_code_for(101, after=3),)
        )
        wizard.run(tmp_path / "config.yaml")

        out = capsys.readouterr().out
        assert "Several people have messaged this bot" in out
        # The fifth sender never got a code, cap or no cap.
        assert OPERATOR not in [chat for chat, text in fake.sent if "Setup code" in text]

    def test_a_running_core_says_so_instead_of_raising(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = FakeTelegram()
        fake.conflict = True

        wizard = _Wizard(fake, "n", str(OPERATOR), "default", "/tmp", "test", "1")
        wizard.run(tmp_path / "config.yaml")

        assert "Another OpenShrimp is already connected" in capsys.readouterr().out
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]

    def test_manual_entry_stays_available(self, tmp_path: Path) -> None:
        fake = _fake_with_operator()

        wizard = _Wizard(fake, "5150", "default", "/tmp", "test", "1", codes=("id",))
        wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [5150]

    def test_a_token_telegram_rejects_is_reprompted(self, tmp_path: Path) -> None:
        fake = _fake_with_operator()
        rejected = FakeTelegram()
        rejected.bad_token = True

        _Wizard(
            fake,
            "y",
            "default",
            "/tmp",
            "test",
            "1",
            tokens=("111:AAA-bbb", "222:CCC-ddd"),
            clients=(rejected.client, fake.client),
        ).run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["telegram"]["token"] == "222:CCC-ddd"
        assert raw["allowed_users"] == [OPERATOR]


class TestRunSetupWizard:
    """The rest of the wizard, unchanged by the handshake."""

    def test_creates_valid_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(
            _fake_with_operator(), "y", "myproject", "/tmp", "My project", "4"
        )
        wizard.run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert "myproject" in raw["contexts"]
        # Setup names no default: it cannot know which project a topic
        # means, and a guess binds every unbound scope to it.
        assert "default_context" not in raw

        ctx = raw["contexts"]["myproject"]
        assert ctx["description"] == "My project"
        assert ctx["model"] == "sonnet"
        assert ctx["allowed_tools"] == ["LSP", "AskUserQuestion"]
        review = raw["review"]
        assert review["tunnel"] == "cloudflared"
        assert 49152 <= review["port"] <= 65535

    def test_uses_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(_fake_with_operator(), "y", "", "/tmp", "", "").run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["default"]["description"] == "Default context"
        assert "model" not in raw["contexts"]["default"]

    def test_custom_model(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            "default",
            "/tmp",
            "test",
            "6",
            "claude-custom-model",
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["default"]["model"] == "claude-custom-model"

    def test_ctrl_c_cancels(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                run_setup_wizard(config_path)
            assert exc_info.value.code == 0

        assert not config_path.exists()

    def test_eof_cancels(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"

        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                run_setup_wizard(config_path)
            assert exc_info.value.code == 0

        assert not config_path.exists()

    def test_invalid_token_reprompts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            "default",
            "/tmp",
            "test",
            "1",
            tokens=("bad-token", "111:AAA-bbb"),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["telegram"]["token"] == "111:AAA-bbb"

    def test_invalid_directory_reprompts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            "default",
            "/nonexistent/path/that/does/not/exist",
            "/tmp",
            "test",
            "1",
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["default"]["directory"] == "/tmp"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.yaml"
        _Wizard(_fake_with_operator(), "y", "default", "/tmp", "test", "1").run(
            config_path
        )
        assert config_path.exists()

    def test_tilde_directory_accepted(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(_fake_with_operator(), "y", "default", "~", "test", "1").run(
            config_path
        )

        raw = yaml.safe_load(config_path.read_text())
        resolved = raw["contexts"]["default"]["directory"]
        assert "~" not in resolved
        assert Path(resolved).is_absolute()

    def test_config_roundtrips_through_load(self, tmp_path: Path) -> None:
        from open_shrimp.config import load_config

        config_path = tmp_path / "config.yaml"
        _Wizard(_fake_with_operator(), "y", "default", "/tmp", "test", "1").run(
            config_path
        )

        config = load_config(str(config_path))
        assert config.telegram.token == "111:AAA-bbb"
        assert config.allowed_users == [OPERATOR]
        assert config.default_context is None


class TestAutostartOffer:
    """The last question the wizard asks, and what it does with the answer."""

    @pytest.fixture
    def registrations(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        """Every install_service call, without one ever being made.

        Nothing here may reach the real installer: it writes the unit file
        this machine is already served by, which no instance name scopes away.
        """
        monkeypatch.delenv("OPENSHRIMP_SUPERVISED", raising=False)
        calls: list[tuple] = []
        monkeypatch.setattr(
            "open_shrimp.service.install_service",
            lambda config_path, **kwargs: calls.append((config_path, kwargs)),
        )
        return calls

    def test_accepting_the_offer_registers_but_does_not_start(
        self, tmp_path: Path, registrations: list[tuple]
    ) -> None:
        """One core owns the Telegram poll, and the wizard hands over to one
        the moment it returns — so what is registered here is registered for
        the next login and started at no point."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(), "y", "default", "/tmp", "test", "1", autostart="y"
        ).run(config_path)

        assert registrations == [(str(config_path), {"start": False})]

    def test_declining_leaves_nothing_registered(
        self,
        tmp_path: Path,
        registrations: list[tuple],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _Wizard(
            _fake_with_operator(), "y", "default", "/tmp", "test", "1", autostart="n"
        ).run(tmp_path / "config.yaml")

        assert registrations == []
        # Declining must still say what happens next and how to change it.
        assert "openshrimp install" in capsys.readouterr().out

    def test_a_supervised_wizard_is_not_asked(
        self,
        tmp_path: Path,
        registrations: list[tuple],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The desktop apps spawn the core and are their own login item, so a
        service registered underneath one is a second core on one token."""
        monkeypatch.setenv("OPENSHRIMP_SUPERVISED", "1")
        wizard = _Wizard(_fake_with_operator(), "y", "default", "/tmp", "test", "1")
        wizard.run(tmp_path / "config.yaml")

        assert not [p for p in wizard.prompts if p.startswith("Keep OpenShrimp running")]
        assert registrations == []

    def test_an_existing_registration_is_not_asked_about(
        self, tmp_path: Path, registrations: list[tuple]
    ) -> None:
        wizard = _Wizard(
            _fake_with_operator(), "y", "default", "/tmp", "test", "1", installed=True
        )
        wizard.run(tmp_path / "config.yaml")

        assert not [p for p in wizard.prompts if p.startswith("Keep OpenShrimp running")]
        assert registrations == []

    def test_a_failed_registration_does_not_lose_the_config(
        self, tmp_path: Path, registrations: list[tuple], monkeypatch
    ) -> None:
        """The config is the irreplaceable artifact: an autostart that could
        not be registered is a downgrade, not a setup failure.  Refusing to
        install is spelled sys.exit, so that is what is thrown here."""

        def _refuse(*_a: object, **_k: object) -> None:
            raise SystemExit(1)

        monkeypatch.setattr("open_shrimp.service.install_service", _refuse)
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(), "y", "default", "/tmp", "test", "1", autostart="y"
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["allowed_users"] == [OPERATOR]


class TestPathCompleter:
    """Tests for the readline path completer."""

    def test_completes_existing_directory(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()
        result = _path_completer(str(tmp_path) + "/sub", 0)
        assert result is not None
        assert "subdir/" in result

    def test_returns_none_for_no_match(self, tmp_path: Path) -> None:
        result = _path_completer(str(tmp_path) + "/nonexistent_xyz", 0)
        assert result is None

    def test_returns_none_past_end(self, tmp_path: Path) -> None:
        (tmp_path / "only_one").mkdir()
        assert _path_completer(str(tmp_path) + "/only_one", 0) is not None
        assert _path_completer(str(tmp_path) + "/only_one", 1) is None

    def test_tilde_completion(self) -> None:
        result = _path_completer("~/.", 0)
        assert result is not None


class TestValidateDirectory:
    """Tests for _validate_directory with tilde expansion."""

    def test_tilde_is_expanded(self) -> None:
        assert _validate_directory("~") is None

    def test_nonexistent_path_rejected(self) -> None:
        assert _validate_directory("/nonexistent/path/xyz") is not None
