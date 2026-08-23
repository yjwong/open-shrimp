"""The boot-time checklist, which exists so that no failure is silent.

Each check is tested for the answer it gives *and* for what it refuses to
claim: a row that says "signed in" when nobody is signed in is worse than no
row, because it converts a solvable problem into a mysterious one — and a row
that says "ready" because the check crashed is the same lie told by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    SandboxConfig,
    TelegramConfig,
)
from open_shrimp.readiness import (
    KEYS,
    Row,
    State,
    _autostart,
    _mini_apps,
    _project_folders,
    _run_check,
    _sandbox,
    _sign_in,
    check_readiness,
    readiness_text,
)
from open_shrimp.web_url import is_public_base


def _config(
    *,
    directory: str = "/tmp",
    backend: str = "claude_sdk",
    public_url: str | None = None,
    sandbox: SandboxConfig | None = None,
    instance_name: str | None = None,
) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[11],
        contexts={
            "default": ContextConfig(
                directory=directory,
                description="the website redesign",
                allowed_tools=[],
                sandbox=sandbox,
            )
        },
        default_context="default",
        review=ReviewConfig(public_url=public_url),
        backend=backend,
        instance_name=instance_name,
    )


class TestPublicBase:
    """The predicate behind the Mini App row, shared with the command guard."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://tidy-otter-plays.trycloudflare.com",
            "https://bots.example.com/openshrimp",
            "https://8.8.8.8",
        ],
    )
    def test_a_real_address_passes(self, url: str) -> None:
        assert is_public_base(_config(public_url=url))

    def test_the_host_port_fallback_is_never_public(self) -> None:
        """Its https is a fiction — the listener speaks plain HTTP — so no
        review.host makes it an address Telegram could open."""
        assert not is_public_base(_config())

        vps = _config()
        vps.review.host = "203.0.113.10"
        assert not is_public_base(vps)

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1:8080",
            "https://[::1]:8080",
            "https://localhost:8080",
            "https://192.168.1.40:8080",
            "https://100.90.1.2:8080",  # a tailnet CGNAT address
            "https://shrimp.local:8080",
            "https://box.home.arpa",
            "http://bots.example.com",
        ],
    )
    def test_nothing_telegram_cannot_open_passes(self, url: str) -> None:
        assert not is_public_base(_config(public_url=url))


class TestMiniApps:
    @pytest.mark.asyncio
    async def test_a_loopback_base_fails_without_asking_the_network(self) -> None:
        state, detail = await _mini_apps(_config())

        assert state is State.PROBLEM
        assert "/login" in detail

    @pytest.mark.asyncio
    async def test_a_name_that_only_resolves_inside_the_lan_fails(
        self, monkeypatch
    ) -> None:
        """Split-horizon DNS answers a probe from this machine and not from
        Telegram's servers, so the address decides, not the probe."""
        async def _resolve(host: str) -> list[str]:
            return ["192.168.1.40"]

        monkeypatch.setattr("open_shrimp.readiness._global_addresses", _resolve)
        state, detail = await _mini_apps(_config(public_url="https://nas.example.com"))

        assert state is State.PROBLEM
        assert "your own network" in detail

    @pytest.mark.asyncio
    async def test_the_edge_gets_a_few_tries_before_it_is_called_broken(
        self, monkeypatch
    ) -> None:
        """cloudflared prints the URL before the edge registers it, so a
        single 530 would make the operator's very first card a false alarm."""
        codes = iter([530, 502, 404])
        monkeypatch.setattr("open_shrimp.readiness._PROBE_GAP", 0)
        monkeypatch.setattr(
            "open_shrimp.readiness._global_addresses",
            _resolving_to(["8.8.8.8"]),
        )
        monkeypatch.setattr("httpx.AsyncClient", _client(status=lambda: next(codes)))

        state, _ = await _mini_apps(_config(public_url="https://otter.trycloudflare.com"))

        assert state is State.OK

    @pytest.mark.asyncio
    async def test_an_edge_that_never_comes_up_fails(self, monkeypatch) -> None:
        monkeypatch.setattr("open_shrimp.readiness._PROBE_GAP", 0)
        monkeypatch.setattr(
            "open_shrimp.readiness._global_addresses",
            _resolving_to(["8.8.8.8"]),
        )
        monkeypatch.setattr("httpx.AsyncClient", _client(status=lambda: 530))

        state, detail = await _mini_apps(_config(public_url="https://otter.trycloudflare.com"))

        assert state is State.PROBLEM
        assert "530" in detail

    @pytest.mark.asyncio
    async def test_a_malformed_url_is_a_dead_button_not_a_crash(
        self, monkeypatch
    ) -> None:
        """httpx.InvalidURL is a sibling of HTTPError, not a subclass, so a
        narrow except would have let the row vanish instead of failing."""
        import httpx

        monkeypatch.setattr("open_shrimp.readiness._PROBE_GAP", 0)
        monkeypatch.setattr(
            "open_shrimp.readiness._global_addresses",
            _resolving_to(["8.8.8.8"]),
        )
        monkeypatch.setattr(
            "httpx.AsyncClient", _client(raises=httpx.InvalidURL("bad"))
        )

        state, detail = await _mini_apps(_config(public_url="https://exa mple.com"))

        assert state is State.PROBLEM
        assert "InvalidURL" in detail


def _resolving_to(addresses: list[str]):
    async def _resolve(host: str) -> list[str]:
        return addresses

    return _resolve


def _client(*, status=None, raises=None):
    """A stand-in for httpx.AsyncClient that answers however the test says."""

    class _Response:
        def __init__(self, code: int) -> None:
            self.status_code = code

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def head(self, url: str):
            if raises is not None:
                raise raises
            return _Response(status())

    return _Client


class TestProjectFolder:
    def test_a_directory_that_is_there_passes(self, tmp_path: Path) -> None:
        state, _ = _project_folders(_config(directory=str(tmp_path)))
        assert state is State.OK

    def test_a_missing_directory_names_itself(self, tmp_path: Path) -> None:
        gone = tmp_path / "renamed-last-week"
        state, detail = _project_folders(_config(directory=str(gone)))

        assert state is State.PROBLEM
        assert str(gone) in detail

    def test_an_unwritable_directory_fails(self, tmp_path: Path) -> None:
        """Read-only fails at the first edit rather than the first turn, which
        is later and harder to connect to a cause."""
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            state, detail = _project_folders(_config(directory=str(locked)))
        finally:
            locked.chmod(0o700)

        assert state is State.PROBLEM
        assert "write" in detail


class TestSignIn:
    @pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
    def test_either_credential_in_the_environment_is_signed_in(
        self, monkeypatch, var: str
    ) -> None:
        """A headless install authenticates with the second one, and telling
        that operator to /login sends them to fix nothing."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv(var, "whatever")

        assert _sign_in(_config()) == (State.OK, "")

    def test_no_credentials_points_at_login(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(
            "open_shrimp.backend.claude_sdk.cred_watcher.host_signed_in",
            lambda: False,
        )
        state, detail = _sign_in(_config())

        assert state is State.PROBLEM
        assert "/login" in detail

    def test_the_row_reads_the_shared_answer(self, monkeypatch) -> None:
        """The wizards shell out to the same function this row calls, so a
        host they call signed in cannot be one this row nags."""
        from open_shrimp.backend.claude_sdk import login as login_mod

        monkeypatch.setattr(
            login_mod, "auth_status", lambda: login_mod.AuthStatus(True, "oauth")
        )

        assert _sign_in(_config()) == (State.OK, "")

    def test_opencode_is_not_asked(self, monkeypatch) -> None:
        """Provider credentials there are a different mechanism with a
        different fix, and a row nobody can act on is noise."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _sign_in(_config(backend="opencode")) is None


class TestSandbox:
    def test_no_sandbox_means_no_row(self) -> None:
        assert _sandbox(_config()) is None

    def test_a_backend_with_no_checks_on_this_platform_says_nothing(
        self, monkeypatch
    ) -> None:
        """doctor would run nothing here, so claiming a pass would be a green
        tick nobody established."""
        monkeypatch.setattr("open_shrimp.doctor.checks_for_backend", lambda b: [])
        assert _sandbox(_config(sandbox=SandboxConfig(backend="hcs", enabled=True))) is None

    def test_a_failed_prerequisite_carries_doctors_remedy(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "open_shrimp.doctor.checks_for_backend",
            lambda backend: [
                ("libvirt", lambda config: (False, "add yourself to the libvirt group"))
            ],
        )
        state, detail = _sandbox(
            _config(sandbox=SandboxConfig(backend="libvirt", enabled=True))
        )

        assert state is State.PROBLEM
        assert "add yourself to the libvirt group" in detail

    def test_a_check_that_raises_is_unknown_not_ready(self, monkeypatch) -> None:
        """The row that matters most must not go green because the probe for
        it crashed."""

        def _boom(config):
            raise RuntimeError("libvirt exploded")

        monkeypatch.setattr(
            "open_shrimp.doctor.checks_for_backend",
            lambda backend: [("libvirt", _boom)],
        )
        state, detail = _sandbox(
            _config(sandbox=SandboxConfig(backend="libvirt", enabled=True))
        )

        assert state is State.UNKNOWN
        assert "libvirt" in detail


class TestAutostart:
    def test_a_supervised_core_is_not_asked(self, monkeypatch) -> None:
        """The desktop apps restart the core themselves and are their own
        login item, so the core telling the operator to install a service
        would be both wrong and unfixable from the phone."""
        monkeypatch.setenv("OPENSHRIMP_SUPERVISED", "1")
        assert _autostart(_config()) is None

    def test_an_unregistered_service_names_the_command(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENSHRIMP_SUPERVISED", raising=False)
        monkeypatch.setattr("open_shrimp.service.is_service_installed", lambda n: False)
        state, detail = _autostart(_config())

        assert state is State.PROBLEM
        assert "openshrimp install" in detail

    def test_the_instance_name_reaches_the_query(self, monkeypatch) -> None:
        """Windows registers the logon task per instance, so an instance-blind
        query would tell a correctly-configured operator to register a second,
        differently-named task."""
        monkeypatch.delenv("OPENSHRIMP_SUPERVISED", raising=False)
        seen: list[str | None] = []
        monkeypatch.setattr(
            "open_shrimp.service.is_service_installed",
            lambda n: seen.append(n) or True,
        )
        _autostart(_config(instance_name="work"))

        assert seen == ["work"]


class TestText:
    def test_each_state_reads_differently(self) -> None:
        text = readiness_text(
            [
                Row("sign_in", "Signed in to Claude", State.OK, "unused"),
                Row("project", "Project folder", State.PROBLEM, "nothing at /gone"),
                Row("sandbox", "Sandbox ready", State.UNKNOWN, "couldn't check"),
            ]
        )

        assert "✅ Signed in to Claude" in text
        assert "unused" not in text
        assert "❌ Project folder" in text
        assert "nothing at /gone" in text
        assert "❔ Sandbox ready" in text
        assert "couldn't check" in text

    def test_a_runaway_detail_is_clamped(self) -> None:
        """Doctor's remedies are joined per backend and can run long; the card
        has one message to fit in."""
        text = readiness_text(
            [Row("sandbox", "Sandbox ready", State.PROBLEM, "x" * 5000)]
        )

        assert len(text) < 600
        assert text.endswith("…")


class TestCheckReadiness:
    @pytest.mark.asyncio
    async def test_every_key_it_produces_is_one_onceness_can_release(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``KEYS`` is what a caller walks to give claims back, so a row whose
        key is missing from it would be reported once and then never again."""
        monkeypatch.delenv("OPENSHRIMP_SUPERVISED", raising=False)
        monkeypatch.setattr(
            "open_shrimp.doctor.checks_for_backend",
            lambda b: [("libvirt", lambda config: (True, "fine"))],
        )
        rows = await check_readiness(
            _config(
                directory=str(tmp_path),
                sandbox=SandboxConfig(backend="libvirt", enabled=True),
            )
        )

        assert {row.key for row in rows} == set(KEYS)

    @pytest.mark.asyncio
    async def test_a_raising_check_becomes_an_unknown_row(self) -> None:
        """Dropping it would read to the caller as "not currently a problem",
        which hands back a claim on no evidence."""

        def _boom(config):
            raise RuntimeError("no")

        row = await _run_check("sign_in", "Signed in", _boom, _config())

        assert row is not None and row.state is State.UNKNOWN

    @pytest.mark.asyncio
    async def test_a_check_that_never_answers_becomes_unknown(
        self, monkeypatch
    ) -> None:
        """libvirt.open takes no timeout and a worker thread cannot be
        cancelled, so the bound has to live here."""
        import asyncio

        async def _hang(config):
            await asyncio.sleep(60)

        monkeypatch.setattr("open_shrimp.readiness._BUDGET", 0.05)
        row = await _run_check("mini_apps", "Mini Apps", _hang, _config())

        assert row is not None and row.state is State.UNKNOWN
        assert "didn't answer" in row.detail

    @pytest.mark.asyncio
    async def test_an_inapplicable_check_produces_no_row(self) -> None:
        row = await _run_check("sandbox", "Sandbox ready", lambda c: None, _config())

        assert row is None
