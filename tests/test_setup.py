"""Tests for the setup wizard.

The wizard writes the first entry in ``allowed_users``, which is the only auth
boundary in the product, so most of what is asserted here is about *who* ends
up in it rather than about the YAML that comes out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from open_shrimp import setup as setup_mod
from open_shrimp.backend.claude_sdk.login import AuthStatus
from open_shrimp.backend.claude_sdk.projects import ClaudeProject
from open_shrimp.doctor import SandboxOffer
from open_shrimp.setup import _path_completer, _validate_directory, run_setup_wizard
from tests.telegram_stub import FakeTelegram, wait_for

OPERATOR = 424242

# What a ``codes`` entry may be: the text to type, or something handed the
# wizard that decides on the spot — which is how a test waits for the bot to
# send before reading the code back off it.
Answer = str | Callable[["_Wizard"], str]

# Every prompt the project step asks, by prefix.  Routed rather than counted,
# so a test about enrollment says nothing about projects and does not have to
# be edited when the import step changes shape.
_PROJECT_PROMPTS = (
    "Projects",
    "Folder ",
    "Call it",
    "Model",
    "Custom model name",
)

# The project step answered end to end: add a folder by path, keep the name it
# suggests, continue, the CLI's own model.
PROJECT_ANSWERS = ("a", "/tmp", "", "", "1")

# "Enter a custom model name" is the entry one past the end of the menu, so it
# is counted rather than written down: adding a model to the shortlist must not
# be a test edit.
CUSTOM_MODEL = str(len(setup_mod._MODELS) + 1)


def _model_choice(alias: str) -> str:
    """What to type to pick *alias*, found in the menu rather than counted off
    it — the order is the wizard's to change."""
    aliases = [entry[0] for entry in setup_mod._MODELS]
    return str(aliases.index(alias) + 1)


# What enabling the sandbox would mean on this host, decided by the test rather
# than by the machine it runs on.  Setup names one backend per platform, so
# this is one offer and not a list — a test that wants the other shape passes
# an unavailable offer, or none at all.
_OFFER = SandboxOffer(
    backend="libvirt",
    label="libvirt",
    summary="each project runs in its own virtual machine",
    available=True,
    detail="",
)

_UNAVAILABLE = replace(
    _OFFER, available=False, detail="libvirt-python not installed"
)


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

    ``answers`` are consumed in order for every prompt except seven: the
    token, which comes from ``tokens``; the code, which comes from ``codes``;
    the sandbox, sign-in, provider-connect and autostart offers of the closing
    steps, which come from ``sandbox``, ``sign_in``, ``connect`` and
    ``autostart``; and the project step, which comes from ``projects`` — so
    that a test saying nothing about any of them is not made to.  ``codes``
    defaults to answering every prompt with the code the operator actually
    received — the handshake is exercised, not stubbed.

    What the project step discovers, what enabling the sandbox would mean
    here, whether Claude is already signed in and which OpenCode providers are
    already connected are all decided here.  None of them may come from the
    machine the tests run on: the developer's own ``~/.claude.json`` would
    otherwise change what the wizard imports, whether libvirt happens to be
    installed would change whether the sandbox is asked about at all, and their
    own credentials — Claude's, or an ``auth.json`` from an opencode they use
    themselves — would decide which credential step appears.
    """

    def __init__(
        self,
        fake: FakeTelegram,
        *answers: str,
        operator: int = OPERATOR,
        tokens: tuple[str, ...] = ("111:AAA-bbb",),
        codes: tuple[Answer, ...] = (),
        clients: tuple[Callable[[], object], ...] = (),
        sandbox: str = "n",
        autostart: str = "n",
        connected: tuple[str, ...] = (),
        connect: str = "n",
        installed: bool = False,
        projects: tuple[str, ...] = PROJECT_ANSWERS,
        discovered: tuple[ClaudeProject, ...] = (),
        offer: SandboxOffer | None = _OFFER,
        signed_in: bool = True,
        sign_in: str = "n",
        login_works: bool = True,
    ):
        self.fake = fake
        self.operator = operator
        self.code_prompts = 0
        self.prompts: list[str] = []
        self._answers = iter(answers)
        self._tokens = list(tokens)
        self._codes = list(codes)
        self._clients = list(clients)
        self._sandbox = sandbox
        self._autostart = autostart
        self._connected = list(connected)
        self._connect = connect
        self._installed = installed
        self._projects = list(projects)
        self._discovered = list(discovered)
        self._offer = offer
        self._signed_in = signed_in
        self._sign_in = sign_in
        self._login_works = login_works
        self.logins = 0
        self.provider_logins: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if prompt.startswith("Telegram bot token") and self._tokens:
            return self._tokens.pop(0)
        if prompt.startswith("Enable sandbox"):
            return self._sandbox
        if prompt.startswith("Sign in now"):
            return self._sign_in
        if prompt.startswith("Connect "):
            return self._connect
        if prompt.startswith("Keep OpenShrimp running"):
            return self._autostart
        if prompt.startswith("Setup code"):
            self.code_prompts += 1
            answer = self._codes.pop(0) if self._codes else _code_for(self.operator)
            return answer(self) if callable(answer) else answer
        if prompt.startswith(_PROJECT_PROMPTS) and self._projects:
            return self._projects.pop(0)
        return next(self._answers)

    def _client(self):
        return (self._clients.pop(0) if self._clients else self.fake.client)()

    def _status(self) -> AuthStatus:
        return AuthStatus(self._signed_in, "oauth" if self._signed_in else None)

    def _login(self) -> bool:
        self.logins += 1
        self._signed_in = self._login_works
        return self._signed_in

    def _connected_providers(self) -> dict[str, str | None]:
        # Membership is the whole question the wizard asks of this, so the
        # values are not invented: a fabricated "api" would be a fact no
        # assertion mentions and no caller reads.
        return dict.fromkeys(self._connected)

    def _provider_login(self, provider_id: str) -> bool:
        self.provider_logins.append(provider_id)
        return self._login_works

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
                    with patch(
                        "open_shrimp.backend.claude_sdk.projects."
                        "discover_claude_projects",
                        lambda: list(self._discovered),
                    ):
                        with patch(
                            "open_shrimp.doctor.blessed_offer",
                            lambda config=None: self._offer,
                        ), patch(
                            "open_shrimp.backend.claude_sdk.login.auth_status",
                            self._status,
                        ), patch(
                            "open_shrimp.backend.claude_sdk.login."
                            "run_interactive_login",
                            self._login,
                        ), patch(
                            "open_shrimp.backend.opencode.auth."
                            "connected_providers",
                            self._connected_providers,
                        ), patch(
                            "open_shrimp.backend.opencode.login."
                            "run_provider_login",
                            self._provider_login,
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
        wizard = _Wizard(fake, "y")
        wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]
        assert raw["telegram"]["token"] == "111:AAA-bbb"

    def test_the_backlog_never_receives_a_setup_code(self, tmp_path: Path) -> None:
        """Telegram queues updates for a day; the backlog is not a candidate."""
        fake = _fake_with_operator()
        fake.queue_message(user_id=666, username="stranger")

        wizard = _Wizard(fake, "y")
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

        wizard = _Wizard(fake, "y", codes=(_press_enter,))
        with patch.object(setup_mod.enrollment, "EnrollmentWindow", _capture):
            wizard.run(tmp_path / "config.yaml")

        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]
        # No code was ever issued on this path.
        assert not any("Setup code" in text for _, text in fake.sent)

    def test_the_wizard_hands_back_a_drained_queue(self, tmp_path: Path) -> None:
        """The core's first poll must replay nothing the wizard consumed."""
        fake = _fake_with_operator()
        wizard = _Wizard(fake, "y")
        wizard.run(tmp_path / "config.yaml")

        assert fake.pending == []

    def test_the_last_word_is_a_promise_not_an_explanation(
        self, tmp_path: Path
    ) -> None:
        """Orientation belongs on first boot, when the bot can act on it."""
        fake = _fake_with_operator()
        wizard = _Wizard(fake, "y")
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

        wizard = _Wizard(fake, "y")
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

        wizard = _Wizard(fake, "y", "y", codes=(_leave_it_overnight,))
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

        wizard = _Wizard(fake, "y", codes=(_code_for(101, after=3),))
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

        wizard = _Wizard(fake, "n", str(OPERATOR))
        wizard.run(tmp_path / "config.yaml")

        assert "Another OpenShrimp is already connected" in capsys.readouterr().out
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["allowed_users"] == [OPERATOR]

    def test_manual_entry_stays_available(self, tmp_path: Path) -> None:
        fake = _fake_with_operator()

        wizard = _Wizard(fake, "5150", codes=("id",))
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
            _fake_with_operator(),
            "y",
            projects=("a", "/tmp", "myproject", "", _model_choice("sonnet")),
        )
        wizard.run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert "myproject" in raw["contexts"]
        # Setup names no default: it cannot know which project a topic
        # means, and a guess binds every unbound scope to it.
        assert "default_context" not in raw

        ctx = raw["contexts"]["myproject"]
        assert ctx["description"] == "tmp"
        assert ctx["model"] == "sonnet"
        assert ctx["allowed_tools"] == ["LSP", "AskUserQuestion"]
        review = raw["review"]
        assert review["tunnel"] == "cloudflared"
        assert 49152 <= review["port"] <= 65535

    def test_uses_defaults(self, tmp_path: Path) -> None:
        """The suggested name and the CLI's own model, both taken by Enter."""
        config_path = tmp_path / "config.yaml"
        _Wizard(_fake_with_operator(), "y").run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["tmp"]["description"] == "tmp"
        assert "model" not in raw["contexts"]["tmp"]

    def test_custom_model(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            projects=("a", "/tmp", "", "", CUSTOM_MODEL, "claude-custom-model"),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["tmp"]["model"] == "claude-custom-model"

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
            tokens=("bad-token", "111:AAA-bbb"),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["telegram"]["token"] == "111:AAA-bbb"

    def test_invalid_directory_reprompts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            projects=(
                "a",
                "/nonexistent/path/that/does/not/exist",
                "/tmp",
                "",
                "",
                "1",
            ),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["tmp"]["directory"] == "/tmp"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.yaml"
        _Wizard(_fake_with_operator(), "y").run(config_path)
        assert config_path.exists()

    def test_tilde_directory_accepted(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(), "y", projects=("a", "~", "home", "", "1")
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        resolved = raw["contexts"]["home"]["directory"]
        assert "~" not in resolved
        assert Path(resolved).is_absolute()

    def test_config_roundtrips_through_load(self, tmp_path: Path) -> None:
        from open_shrimp.config import load_config

        config_path = tmp_path / "config.yaml"
        _Wizard(_fake_with_operator(), "y").run(config_path)

        config = load_config(str(config_path))
        assert config.telegram.token == "111:AAA-bbb"
        assert config.allowed_users == [OPERATOR]
        assert config.default_context is None


def _found(directory: str, context_name: str, *, started: int = 0) -> ClaudeProject:
    return ClaudeProject(
        directory=directory,
        name=Path(directory).name,
        context_name=context_name,
        last_start_time=started,
    )


class TestProjectImport:
    """The step that replaced "type one absolute path".

    Discovery, the tick list and the one question about isolation.  Every
    directory used here exists, because the wizard checks: a path that is not
    there is a re-prompt, not a context.
    """

    def _projects(self, tmp_path: Path) -> tuple[ClaudeProject, ...]:
        for name in ("api", "talenthub.glints.com"):
            (tmp_path / name).mkdir()
        return (
            _found(str(tmp_path / "api"), "api", started=2),
            _found(str(tmp_path / "talenthub.glints.com"), "talenthub-glints-com"),
        )

    def test_everything_found_is_imported_by_pressing_enter(
        self, tmp_path: Path
    ) -> None:
        """Pre-ticked is the point: a user with four projects taps once."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("", "1"),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert sorted(raw["contexts"]) == ["api", "talenthub-glints-com"]

    def test_a_folder_name_no_context_may_take_still_imports(
        self, tmp_path: Path
    ) -> None:
        """The name came from a folder, not from a keyboard, so the wizard
        must never reach the validator's refusal with it."""
        from open_shrimp.config import load_config

        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("", "1"),
        ).run(config_path)

        config = load_config(str(config_path))
        assert config.contexts["talenthub-glints-com"].description == (
            "talenthub.glints.com"
        )

    def test_unticking_leaves_a_project_out(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("2", "", "1"),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert list(raw["contexts"]) == ["api"]

    def test_skip_writes_a_config_with_no_projects(self, tmp_path: Path) -> None:
        """The outcome a fresh machine is entitled to: a config that loads, and
        the OpenShrimp context to add projects from."""
        from open_shrimp.config import load_config

        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("s",),
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"] == {}
        assert "default_context" not in raw

        config = load_config(str(config_path))
        assert config.contexts == {}
        assert config.default_context is None

    def test_skipping_asks_nothing_about_isolation(self, tmp_path: Path) -> None:
        """With nothing imported there is nothing to isolate, and a question
        with no consequence teaches the user to answer without reading."""
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("s",),
        )
        wizard.run(tmp_path / "config.yaml")

        assert not [p for p in wizard.prompts if p.startswith("Enable sandbox")]

    def test_the_sandbox_answer_is_written_to_every_imported_project(
        self, tmp_path: Path
    ) -> None:
        """One question, asked once, for the whole import."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("", "1"),
            sandbox="y",
        ).run(config_path)

        raw = yaml.safe_load(config_path.read_text())
        # computer_use rides along unasked: it widens no boundary the sandbox
        # question did not already settle, and it is an input to the guest's
        # fingerprint, so turning it on later rebuilds the guest from scratch.
        # The wizard is the only moment the decision is cheap.
        assert [ctx["sandbox"] for ctx in raw["contexts"].values()] == [
            {"backend": "libvirt", "computer_use": True},
            {"backend": "libvirt", "computer_use": True},
        ]

    def test_the_wizard_grants_no_host_escape(self, tmp_path: Path) -> None:
        """The one thing a sandboxed context must never get from setup.

        `computer_use` defaulting to true makes the sandbox block something
        the wizard populates rather than merely names, so the boundary that
        matters is pinned here: `allow_host_escape` exposes host_bash, and no
        question the wizard asks may reach it.
        """
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path)[:1],
            projects=("", "1"),
            sandbox="y",
        ).run(config_path)

        sandbox = yaml.safe_load(config_path.read_text())["contexts"]["api"]["sandbox"]
        assert "allow_host_escape" not in sandbox
        assert sandbox == {"backend": "libvirt", "computer_use": True}

    def test_the_sandbox_question_never_names_a_backend(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Setup asks whether a project is isolated, not which hypervisor
        isolates it — a user who needs the difference explained cannot weigh
        it, and the config Mini App can still move a context afterwards."""
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path)[:1],
            projects=("", "1"),
            sandbox="y",
        )
        wizard.run(tmp_path / "config.yaml")

        asked = [p for p in wizard.prompts if p.startswith("Enable sandbox")]
        assert asked == ["Enable sandbox [Y/n]: "]
        assert "libvirt" not in capsys.readouterr().out

    def test_declining_the_sandbox_writes_no_sandbox_block(
        self, tmp_path: Path
    ) -> None:
        """Refusing is a supported answer, not a failed step: the context is
        written without a sandbox rather than not written at all."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path)[:1],
            projects=("", "1"),
            sandbox="n",
        ).run(config_path)

        assert "sandbox" not in yaml.safe_load(config_path.read_text())["contexts"]["api"]

    def test_a_host_that_cannot_sandbox_is_told_why_and_asked_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The only honest answer to a question whose yes cannot be honoured
        is not to ask it — but silence would read as a missing feature, so the
        remedy is shown where the question would have been."""
        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path)[:1],
            projects=("", "1"),
            offer=_UNAVAILABLE,
        )
        wizard.run(config_path)

        assert not [p for p in wizard.prompts if p.startswith("Enable sandbox")]
        assert "libvirt-python not installed" in capsys.readouterr().out
        assert "sandbox" not in yaml.safe_load(config_path.read_text())["contexts"]["api"]

    def test_a_platform_with_no_blessed_backend_still_finishes(
        self, tmp_path: Path
    ) -> None:
        """No backend at all is the same outcome as one that will not start:
        setup completes, unsandboxed, rather than stranding the user."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path)[:1],
            projects=("", "1"),
            offer=None,
        ).run(config_path)

        assert "sandbox" not in yaml.safe_load(config_path.read_text())["contexts"]["api"]

    def test_a_hand_typed_name_that_collides_is_refused_before_the_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The config write would keep the last of two and report an import of
        two projects that produced one."""
        (tmp_path / "elsewhere").mkdir()
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            discovered=self._projects(tmp_path),
            projects=("a", str(tmp_path / "elsewhere"), "api", "other", "", "1"),
        ).run(config_path)

        assert "already called 'api'" in capsys.readouterr().out
        raw = yaml.safe_load(config_path.read_text())
        assert sorted(raw["contexts"]) == ["api", "other", "talenthub-glints-com"]

    def test_a_machine_with_nothing_to_import_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fresh machine has no ``~/.claude.json`` and must still reach a
        working config, by path."""
        config_path = tmp_path / "config.yaml"
        _Wizard(_fake_with_operator(), "y").run(config_path)

        assert "None found on this machine" in capsys.readouterr().out
        assert list(yaml.safe_load(config_path.read_text())["contexts"]) == ["tmp"]


class TestSignInOffer:
    """The step that exists so nobody finishes setup and is then told to /login.

    The wizard and the readiness card read one answer, so what is asserted is
    when the step appears at all: never on a host that is already signed in,
    always on one that is not.
    """

    def test_a_signed_in_host_is_asked_nothing_and_told_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wizard = _Wizard(_fake_with_operator(), "y", signed_in=True)
        wizard.run(tmp_path / "config.yaml")

        assert not [p for p in wizard.prompts if p.startswith("Sign in now")]
        assert wizard.logins == 0
        assert "signing in" not in capsys.readouterr().out

    def test_accepting_runs_the_sign_in_here_and_confirms_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Inline, in this terminal: the wizard already owns one and the CLI
        wants one."""
        wizard = _Wizard(_fake_with_operator(), "y", signed_in=False, sign_in="y")
        wizard.run(tmp_path / "config.yaml")

        assert wizard.logins == 1
        assert "Signed in." in capsys.readouterr().out

    def test_declining_still_writes_the_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The readiness card asks again at the next boot, so the wizard names
        the fallback and moves on."""
        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(_fake_with_operator(), "y", signed_in=False, sign_in="n")
        wizard.run(config_path)

        assert wizard.logins == 0
        assert "/login" in capsys.readouterr().out
        assert yaml.safe_load(config_path.read_text())["allowed_users"] == [OPERATOR]

    def test_a_sign_in_that_did_not_take_points_at_telegram(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            signed_in=False,
            sign_in="y",
            login_works=False,
        ).run(config_path)

        assert "/login" in capsys.readouterr().out
        assert config_path.exists()

    def test_an_unreadable_credential_store_offers_the_step(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The probe runs before the config is written, so it may not decide
        whether the config gets written at all."""

        def _unreadable() -> AuthStatus:
            raise OSError("credential store is unreadable")

        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(_fake_with_operator(), "y", sign_in="n")
        wizard._status = _unreadable  # type: ignore[method-assign]
        wizard.run(config_path)

        assert "Sign in now?" in "".join(wizard.prompts)
        assert yaml.safe_load(config_path.read_text())["allowed_users"] == [OPERATOR]

    def test_a_missing_cli_does_not_lose_the_config(self, tmp_path: Path) -> None:
        """find_claude_binary raises when it finds nothing, and the config is
        the irreplaceable artifact."""

        def _missing() -> bool:
            raise RuntimeError("Could not find the Claude CLI binary.")

        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(_fake_with_operator(), "y", signed_in=False, sign_in="y")
        wizard._login = _missing  # type: ignore[method-assign]
        wizard.run(config_path)

        assert yaml.safe_load(config_path.read_text())["allowed_users"] == [OPERATOR]


class TestModelMenu:
    """What the picker offers, and what picking one of them writes.

    The backend is never a question: it follows from the model, so what is
    asserted is that the menu says which lab a model belongs to and never says
    "backend", and that the config that comes out routes the turn.
    """

    def test_every_row_names_a_lab_and_none_names_a_backend(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """"gpt-5.6-sol" without "OpenAI" does not tell somebody which account
        they are about to be asked to log into.  "opencode" would tell them
        about a decision that is not theirs to make."""
        _Wizard(_fake_with_operator(), "y").run(tmp_path / "config.yaml")

        out = capsys.readouterr().out
        menu = out.split("Which model should they use?")[1].split("Model [1]")[0]
        # The split has to have cut something, or the assertions below would be
        # about the whole transcript.
        assert "Enter a custom model name" in menu
        for lab in ("Anthropic", "OpenAI", "Google", "xAI", "DeepSeek"):
            assert lab in menu
        assert "backend" not in menu.lower()

    def test_an_opencode_model_routes_every_imported_project(
        self, tmp_path: Path
    ) -> None:
        """Per context, not top level, so a later project on Claude needs no
        second answer to a question this wizard asks once."""
        (tmp_path / "second").mkdir()
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            projects=(
                "a", "/tmp", "",
                "a", str(tmp_path / "second"), "",
                "", _model_choice("openai/gpt-5.6-sol"),
            ),
        ).run(config_path)

        contexts = yaml.safe_load(config_path.read_text())["contexts"]
        assert len(contexts) == 2
        for context in contexts.values():
            assert context["model"] == "openai/gpt-5.6-sol"
            assert context["backend"] == "opencode"

    def test_a_claude_model_writes_no_backend_key_at_all(
        self, tmp_path: Path
    ) -> None:
        """A Claude-only config comes out byte-identical to the one this
        wizard wrote before it could offer anything else."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            projects=("a", "/tmp", "", "", _model_choice("sonnet")),
        ).run(config_path)

        assert "backend" not in yaml.safe_load(config_path.read_text())["contexts"]["tmp"]

    @pytest.mark.parametrize(
        ("typed", "backend"),
        [("openai/o5-preview", "opencode"), ("claude-custom-model", None)],
    )
    def test_a_custom_name_is_routed_by_its_shape(
        self, tmp_path: Path, typed: str, backend: str | None
    ) -> None:
        """The one rule for a model no shortlist contains: a name carrying a
        slash is provider-qualified, so it is OpenCode's, and a bare one is
        not — which is what OpenCode itself enforces at runtime."""
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            connected=("openai",),
            projects=("a", "/tmp", "", "", CUSTOM_MODEL, typed),
        ).run(config_path)

        context = yaml.safe_load(config_path.read_text())["contexts"]["tmp"]
        assert context["model"] == typed
        assert context.get("backend") == backend


class TestUnbuildablePlatform:
    """A host opencode publishes no build for is offered none of its models.

    The check is in the shared catalog, so this holds for all three wizards at
    once: without it the picker offers an entry whose config ``_validate_raw``
    then refuses, and the wizard writes a first config the core will not start
    from.
    """

    @pytest.fixture
    def no_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "open_shrimp.backend.opencode.release.no_build_reason",
            lambda: "no opencode build is published for Linux ppc64le",
        )

    def _labs(self) -> set[str]:
        from open_shrimp.backend.setup_models import menu_provider, setup_models

        return {menu_provider(choice.alias) for choice in setup_models()}

    def test_the_menu_offers_claude_alone(self, no_build: None) -> None:
        assert self._labs() == {"Anthropic"}

    def test_a_host_with_a_build_is_offered_the_shortlist(self) -> None:
        assert self._labs() > {"Anthropic"}


class TestProviderConnect:
    """The credential step for a model that is not Claude's.

    Which credential is offered follows from the model and nothing else, so
    what is asserted is that nobody is asked for both — and that none of the
    ways this step can fail costs the config, which is the artifact the wizard
    exists to produce.
    """

    def _picked(self, alias: str) -> tuple[str, ...]:
        return ("a", "/tmp", "", "", _model_choice(alias))

    def test_an_opencode_model_is_never_offered_the_claude_sign_in(
        self, tmp_path: Path
    ) -> None:
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            signed_in=False,
            projects=self._picked("openai/gpt-5.6-sol"),
        )
        wizard.run(tmp_path / "config.yaml")

        assert not [p for p in wizard.prompts if p.startswith("Sign in now")]
        assert wizard.logins == 0
        assert "Connect OpenAI now?" in "".join(wizard.prompts)

    def test_a_claude_model_never_reaches_for_the_opencode_binary(
        self, tmp_path: Path
    ) -> None:
        """The fetch is 50 MB, and a Claude context has nothing to do with
        it."""
        wizard = _Wizard(
            _fake_with_operator(), "y", signed_in=True, projects=self._picked("sonnet")
        )
        wizard.run(tmp_path / "config.yaml")

        assert wizard.provider_logins == []
        assert not [p for p in wizard.prompts if p.startswith("Connect ")]

    def test_a_provider_already_connected_is_asked_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Matching what the Claude branch does for a host already signed in:
        the credential is there, so there is nothing to offer."""
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            connected=("openai",),
            projects=self._picked("openai/gpt-5.6-sol"),
        )
        wizard.run(tmp_path / "config.yaml")

        assert wizard.provider_logins == []
        assert not [p for p in wizard.prompts if p.startswith("Connect ")]
        assert "connecting OpenAI" not in capsys.readouterr().out

    def test_accepting_runs_the_login_for_that_provider_alone(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            connect="y",
            projects=self._picked("google/gemini-3.7-flash"),
        )
        wizard.run(tmp_path / "config.yaml")

        assert wizard.provider_logins == ["google"]
        assert "Connected." in capsys.readouterr().out

    def test_declining_names_the_command_and_still_writes_the_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare ``opencode`` on $PATH is deliberately not the binary
        OpenShrimp runs, so the sentence names the one that is."""
        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            connect="n",
            projects=self._picked("xai/grok-4.6"),
        )
        wizard.run(config_path)

        assert wizard.provider_logins == []
        out = capsys.readouterr().out
        assert "auth login --provider xai" in out
        assert yaml.safe_load(config_path.read_text())["allowed_users"] == [OPERATOR]

    def test_a_login_that_did_not_take_still_writes_the_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = tmp_path / "config.yaml"
        _Wizard(
            _fake_with_operator(),
            "y",
            connect="y",
            login_works=False,
            projects=self._picked("deepseek/deepseek-v4-pro"),
        ).run(config_path)

        assert "auth login --provider deepseek" in capsys.readouterr().out
        assert yaml.safe_load(config_path.read_text())["allowed_users"] == [OPERATOR]

    def test_a_fetch_that_could_not_run_does_not_lose_the_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A platform opencode publishes no build for raises out of the fetch,
        and the config is the irreplaceable artifact."""

        def _no_build(provider_id: str) -> bool:
            raise RuntimeError("no opencode build is published for this platform")

        config_path = tmp_path / "config.yaml"
        wizard = _Wizard(
            _fake_with_operator(),
            "y",
            connect="y",
            projects=self._picked("openai/gpt-5.6-sol"),
        )
        wizard._provider_login = _no_build  # type: ignore[method-assign]
        wizard.run(config_path)

        assert "no opencode build is published" in capsys.readouterr().out
        assert yaml.safe_load(config_path.read_text())["allowed_users"] == [OPERATOR]


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
        _Wizard(_fake_with_operator(), "y", autostart="y").run(config_path)

        assert registrations == [(str(config_path), {"start": False})]

    def test_declining_leaves_nothing_registered(
        self,
        tmp_path: Path,
        registrations: list[tuple],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _Wizard(_fake_with_operator(), "y", autostart="n").run(tmp_path / "config.yaml")

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
        wizard = _Wizard(_fake_with_operator(), "y")
        wizard.run(tmp_path / "config.yaml")

        assert not [p for p in wizard.prompts if p.startswith("Keep OpenShrimp running")]
        assert registrations == []

    def test_an_existing_registration_is_not_asked_about(
        self, tmp_path: Path, registrations: list[tuple]
    ) -> None:
        wizard = _Wizard(_fake_with_operator(), "y", installed=True)
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
        _Wizard(_fake_with_operator(), "y", autostart="y").run(config_path)

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
