"""What a scope is told when its sandbox refuses to start.

The failure this replaces was silent in the only way that matters: the message
never ran, and the reply said nothing the operator could act on.  So each test
here asserts what the reply *refuses* to say as well as what it says — a
sandbox failure that renders "An error occurred while processing your request"
has thrown away a remedy the code already held, and one that tells a user
outside the ``docker`` group to start a daemon that is already running sends
them somewhere there is nothing to fix.
"""

from __future__ import annotations

import asyncio
import platform
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from open_shrimp import doctor, sandbox_diagnosis
from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    SandboxConfig,
    TelegramConfig,
)
from open_shrimp.db import ChatScope, init_db, set_active_context
from open_shrimp.handlers.messages import _start_agent_task
from open_shrimp.handlers.state import _running_tasks
from open_shrimp.markdown import TELEGRAM_MAX_LENGTH, split_message
from open_shrimp.sandbox.base import SandboxStartupError
from open_shrimp.sandbox_diagnosis import diagnose, failure_reply

CHAT_ID = 100
SCOPE = ChatScope(chat_id=CHAT_ID, thread_id=None)

# The longest remedy the tree actually holds, quoted from ``doctor``'s HCS RDP
# helper check: a Windows path, a bracketed aside, and a release-asset name,
# every one of which MarkdownV2 would reject as a bad entity.
LONG_REMEDY = (
    "work: no prebuilt helper in C:\\Users\\me\\AppData\\Local\\openshrimp\\"
    "rdp and no sandbox.mingw_bin to build one — unpack the "
    "openshrimp-hcs-rdp-helper-windows-x64.zip release asset there (or point "
    "OPENSHRIMP_HCS_RDP_HELPER at it), or set sandbox.mingw_bin to an MSYS2 "
    "mingw64 bin directory (e.g. C:\\msys64\\mingw64\\bin)"
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Each test diagnoses from scratch; the TTL cache is process-wide."""
    sandbox_diagnosis._cache.clear()
    yield
    sandbox_diagnosis._cache.clear()


def _config(backend: str = "docker") -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "work": ContextConfig(
                directory="/tmp",
                description="the website redesign",
                allowed_tools=[],
                sandbox=SandboxConfig(backend=backend, enabled=True),
            )
        },
        default_context="work",
        review=ReviewConfig(),
    )


def _error(backend: str = "docker", message: str = "boom") -> SandboxStartupError:
    return SandboxStartupError("work", backend, RuntimeError(message))


def _checks(monkeypatch, *checks: tuple[str, Any]) -> None:
    monkeypatch.setattr(
        "open_shrimp.doctor.checks_for_backend", lambda backend: list(checks)
    )


# -- W3: the Docker remedies that did not exist --------------------------------


class TestDockerCheck:
    """``docker info`` exits non-zero for two opposite reasons."""

    def _run(self, monkeypatch, *, returncode: int, stderr: bytes):
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=returncode, stderr=stderr),
        )
        return doctor._check_docker(None)

    def test_permission_denied_names_the_group_not_the_daemon(self, monkeypatch):
        ok, detail = self._run(
            monkeypatch,
            returncode=1,
            stderr=(
                b"permission denied while trying to connect to the Docker "
                b"daemon socket at unix:///var/run/docker.sock"
            ),
        )

        assert not ok
        assert "usermod -aG docker" in detail
        # The trap this check used to set: the daemon is running, and sending
        # the operator to start it wastes the one attempt they will make.
        assert "daemon is not running" not in detail

    def test_the_group_remedy_says_a_fresh_login_is_needed(self, monkeypatch):
        """A new terminal in the same session still carries the old token, so
        a remedy that stops at ``usermod`` leaves the user where it found
        them."""
        _, detail = self._run(
            monkeypatch, returncode=1, stderr=b"permission denied"
        )

        assert "log out and back in" in detail

    def test_a_dead_daemon_still_says_so(self, monkeypatch):
        ok, detail = self._run(
            monkeypatch,
            returncode=1,
            stderr=b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )

        assert not ok
        assert "daemon is not running" in detail
        assert "usermod" not in detail

    def test_a_running_daemon_passes(self, monkeypatch):
        ok, detail = self._run(monkeypatch, returncode=0, stderr=b"")

        assert ok
        assert "/usr/bin/docker" in detail


# -- B1: the Lima check that could never run ----------------------------------


class TestLimaCheck:
    def test_the_check_answers_instead_of_raising(self, monkeypatch):
        """It called a name that did not exist, so it raised ``NameError`` on
        the only platform it applies to — and a crash reads as ``UNKNOWN``,
        not as the missing binary it was written to report."""
        monkeypatch.setattr(
            "open_shrimp.doctor.find_binary", lambda name: None
        )
        ok, detail = doctor._check_lima(None)

        assert not ok
        assert "brew install lima" in detail

    def test_every_lima_prerequisite_runs_on_darwin(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        checks = doctor.checks_for_backend("lima")

        assert checks, "Lima has prerequisites on macOS"
        for label, check in checks:
            ok, detail = check(None)
            assert isinstance(ok, bool), label
            assert detail


# -- The libvirt prerequisites, which the same priority order now speaks for --


class TestLibvirtChecks:
    def test_the_missing_binding_names_its_install_line(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "libvirt", None)
        ok, detail = doctor._check_libvirt(None)

        assert not ok
        assert "pip install libvirt-python" in detail

    def test_an_absent_virtiofsd_is_not_a_blocker(self, monkeypatch):
        """It is downloaded on first use, and the manager falls back to 9p if
        even that fails — so reporting it as a failed prerequisite would put a
        remedy in front of whatever actually went wrong."""
        from open_shrimp.sandbox import libvirt_helpers

        monkeypatch.setattr(libvirt_helpers, "find_virtiofsd", lambda: None)
        ok, detail = doctor._check_virtiofsd(None)

        assert ok
        assert "downloaded on first use" in detail


class TestLibvirtImportRemedy:
    def test_the_remedy_rides_on_the_exception(self, monkeypatch):
        from open_shrimp import paths
        from open_shrimp.sandbox import libvirt_helpers
        from open_shrimp.sandbox.manager import LibvirtSandboxManager

        paths.init_paths()
        monkeypatch.setattr(libvirt_helpers, "ensure_virtiofsd", lambda: None)
        monkeypatch.setitem(sys.modules, "libvirt", None)

        with pytest.raises(ImportError) as caught:
            LibvirtSandboxManager().start_reaper()

        # ``str()`` is what a caller renders; "No module named 'libvirt'" is
        # a symptom with no way out of it.
        assert "pip install libvirt-python" in str(caught.value)


# -- The report the reply sends people to --------------------------------------


class TestTheReport:
    """``openshrimp doctor`` is what every reply here points at, so it has to
    survive the checks it runs."""

    def test_one_crashing_check_does_not_take_the_report_with_it(
        self, monkeypatch, capsys
    ):
        def _boom(config):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(
            doctor,
            "_CHECKS",
            [
                ("Wobbly", _boom, None, ()),
                ("Steady", lambda config: (True, "fine"), None, ()),
            ],
        )
        code = doctor.run_doctor(None)
        printed = capsys.readouterr().out

        assert code == 1
        assert "Steady" in printed
        # A crash is reported as itself, never as a pass.
        assert "could not run" in printed


# -- W2: which of the two sources answers -------------------------------------


class TestDiagnose:
    @pytest.mark.asyncio
    async def test_a_failing_prerequisite_leads_but_keeps_the_exception(
        self, monkeypatch,
    ):
        """The remedy leads, because it is the actionable half.  The exception
        still travels with it, since a check can fail over something this
        failure had nothing to do with."""
        _checks(monkeypatch, ("Docker", lambda config: (False, "join the docker group")))

        what, prerequisite = await diagnose(
            _error(message="Failed to start container"), _config()
        )

        assert prerequisite
        assert "join the docker group" in what
        assert "Failed to start container" in what
        assert what.index("join the docker group") < what.index("Failed to start")

    @pytest.mark.asyncio
    async def test_a_clean_host_renders_the_failure_itself(self, monkeypatch):
        _checks(monkeypatch, ("Docker", lambda config: (True, "daemon running")))

        what, prerequisite = await diagnose(
            _error(message="VM SSH not reachable after 120s"), _config()
        )

        assert not prerequisite
        assert "VM SSH not reachable after 120s" in what

    @pytest.mark.asyncio
    async def test_a_check_that_crashed_is_not_a_pass(self, monkeypatch):
        """The same lie ``readiness`` refuses to tell: a probe that could not
        answer must not be reported as one that answered well."""

        def _boom(config):
            raise RuntimeError("libvirt exploded")

        _checks(monkeypatch, ("libvirt", _boom))

        what, prerequisite = await diagnose(_error("libvirt"), _config("libvirt"))

        assert not prerequisite
        assert "libvirt" in what
        assert "couldn't check" in what

    @pytest.mark.asyncio
    async def test_a_backend_with_no_checks_here_falls_back_to_the_failure(
        self, monkeypatch
    ):
        _checks(monkeypatch)

        what, prerequisite = await diagnose(_error(message="grim failed"), _config())

        assert not prerequisite
        assert "grim failed" in what

    @pytest.mark.asyncio
    async def test_a_failure_with_nothing_to_say_still_says_something(
        self, monkeypatch
    ):
        _checks(monkeypatch)
        error = SandboxStartupError("work", "docker", RuntimeError(""))

        what, _ = await diagnose(error, _config())

        assert what.strip()


class TestDiagnosisCost:
    """W7: a scope retrying in a loop must not shell out on every turn."""

    @pytest.mark.asyncio
    async def test_a_retry_reuses_the_diagnosis(self, monkeypatch):
        runs = 0

        def _count(config):
            nonlocal runs
            runs += 1
            return False, "join the docker group"

        _checks(monkeypatch, ("Docker", _count))

        for _ in range(3):
            await diagnose(_error(), _config())

        assert runs == 1

    @pytest.mark.asyncio
    async def test_an_expired_diagnosis_is_taken_again(self, monkeypatch):
        runs = 0

        def _count(config):
            nonlocal runs
            runs += 1
            return False, "join the docker group"

        _checks(monkeypatch, ("Docker", _count))
        clock = [1000.0]
        monkeypatch.setattr(sandbox_diagnosis, "_now", lambda: clock[0])
        await diagnose(_error(), _config())
        await diagnose(_error(), _config())
        clock[0] += sandbox_diagnosis._TTL + 1
        await diagnose(_error(), _config())

        assert runs == 2


class TestFailureReply:
    @pytest.mark.asyncio
    async def test_it_names_the_project_and_says_the_message_did_not_run(
        self, monkeypatch
    ):
        _checks(monkeypatch, ("Docker", lambda config: (False, "join the docker group")))

        text = await failure_reply(_error(), _config())

        assert '"work"' in text
        assert "hasn't run" in text
        assert "join the docker group" in text
        assert "/context" in text

    @pytest.mark.asyncio
    async def test_a_runtime_failure_is_not_sold_as_a_missing_piece(
        self, monkeypatch
    ):
        _checks(monkeypatch, ("Docker", lambda config: (True, "daemon running")))

        text = await failure_reply(
            _error(message="guest control agent never came up"), _config()
        )

        assert "guest control agent never came up" in text
        assert "again" in text

    @pytest.mark.asyncio
    async def test_the_longest_real_remedy_survives_the_split(self, monkeypatch):
        """W5/W6: the remedy is the payload, so nothing in the send path may
        clip it or mangle its backslashes."""
        _checks(monkeypatch, ("HCS RDP helper", lambda config: (False, LONG_REMEDY)))

        text = await failure_reply(_error("hcs"), _config("hcs"))
        parts = split_message(text)

        assert all(len(part) <= TELEGRAM_MAX_LENGTH for part in parts)
        assert LONG_REMEDY in "".join(parts)
        assert "C:\\msys64\\mingw64\\bin" in text


# -- The real error path -------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    conn = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield conn
    asyncio.run(conn.close())


class _Context:
    """The slice of ``ContextTypes.DEFAULT_TYPE`` the dispatch path touches."""

    def __init__(self, sandbox: "_RefusingSandbox") -> None:
        self.bot = AsyncMock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=7)
        self.bot_data: dict[str, Any] = {
            "sandbox_managers": {"docker": _StubManager(sandbox)}
        }


class _RefusingSandbox:
    """A sandbox that is already built and running, and will not start.

    Only the lifecycle the launch path touches is implemented; anything else
    being reached would mean the failure was not raised where it is claimed.
    """

    def __init__(self, message: str = "Failed to start container") -> None:
        self._message = message
        self.context_name = "work"
        self.attempted = False

    def environment_ready(self) -> bool:
        return True

    def running(self) -> bool:
        return True

    def ensure_environment(self, *, log_file=None, progress=None) -> None:
        self.attempted = True
        raise RuntimeError(self._message)


class _StubManager:
    def __init__(self, sandbox: _RefusingSandbox) -> None:
        self._sandbox = sandbox

    def agent_home_dir(self, context_name: str) -> Path:
        return Path("/tmp") / context_name

    def create_sandbox(self, context_name, ctx, runtime=None) -> _RefusingSandbox:
        return self._sandbox


async def _drive(db, message: str = "Failed to start container") -> _Context:
    """One turn against a sandbox that refuses, through the real handler.

    Nothing between the lifecycle call and the reply is stubbed: the refusal
    is raised by ``ensure_environment`` and travels the same path a real
    Docker failure would.
    """
    sandbox = _RefusingSandbox(message)
    context = _Context(sandbox)
    await set_active_context(db, SCOPE, "work")
    await _start_agent_task(
        prompt="what changed today?",
        attachments=[],
        scope=SCOPE,
        config=_config(),
        db=db,
        context=context,
        user_id=1,
    )
    task = _running_tasks.get(SCOPE)
    if task is not None:
        await task
    assert sandbox.attempted, "the lifecycle call was never reached"
    return context


def _sent(context: _Context) -> list[str]:
    return [
        call.kwargs.get("text", "")
        for call in context.bot.send_message.await_args_list
    ]


def _failure_message(context: _Context) -> str:
    matching = [text for text in _sent(context) if "sandbox" in text]
    assert matching, f"nothing said about the sandbox: {_sent(context)}"
    return "\n".join(matching)


class TestTheWholeChain:
    """From the lifecycle call that raises to the words the user reads."""

    @pytest.mark.asyncio
    async def test_a_missing_prerequisite_reaches_the_user(self, monkeypatch, db):
        _checks(
            monkeypatch,
            ("Docker", lambda config: (False, "add yourself to the docker group")),
        )

        context = await _drive(db)

        said = _failure_message(context)
        assert "add yourself to the docker group" in said
        assert "An error occurred while processing your request" not in "".join(
            _sent(context)
        )

    @pytest.mark.asyncio
    async def test_a_runtime_failure_reaches_the_user(self, monkeypatch, db):
        _checks(monkeypatch, ("Docker", lambda config: (True, "daemon running")))

        context = await _drive(db, "VM SSH not reachable after 120s")

        assert "VM SSH not reachable after 120s" in _failure_message(context)

    @pytest.mark.asyncio
    async def test_the_remedy_is_sent_plain(self, monkeypatch, db):
        """MarkdownV2 would reject these remedies outright, and a message
        Telegram refuses is worse than the sentence it replaced."""
        _checks(monkeypatch, ("HCS RDP helper", lambda config: (False, LONG_REMEDY)))

        context = await _drive(db)

        remedy_calls = [
            call
            for call in context.bot.send_message.await_args_list
            if "mingw64" in call.kwargs.get("text", "")
        ]
        assert remedy_calls
        for call in remedy_calls:
            assert call.kwargs.get("parse_mode") is None
        assert LONG_REMEDY in "".join(
            call.kwargs["text"] for call in remedy_calls
        )

    @pytest.mark.asyncio
    async def test_a_remedy_past_the_length_limit_is_split_not_clipped(
        self, monkeypatch, db
    ):
        """W6: the remedy is the payload, so the limit costs a second message
        rather than the end of the sentence."""
        long_report = "\n".join(f"{i}: {LONG_REMEDY}" for i in range(20))
        _checks(monkeypatch, ("HCS RDP helper", lambda config: (False, long_report)))

        context = await _drive(db)

        parts = [text for text in _sent(context) if "mingw64" in text]
        assert len(parts) > 1, "a reply over the limit must arrive in pieces"
        assert all(len(part) <= TELEGRAM_MAX_LENGTH for part in parts)
        assert "".join(parts).count(LONG_REMEDY) == 20


class TestTheReconnectPath:
    """A sandbox that dies mid-session is the likeliest real failure there is,
    and its reconnect must not flatten the diagnosis into "reconnect failed"
    — the caller's fallback text blames a process that terminated, when what
    happened is that the machine lost a prerequisite."""

    @pytest.mark.asyncio
    async def test_a_startup_failure_survives_the_reconnect(self, monkeypatch):
        from open_shrimp import client_manager

        async def _refuse(**kwargs):
            raise SandboxStartupError("work", "docker", RuntimeError("boom"))

        monkeypatch.setattr(client_manager, "get_or_create_session", _refuse)
        monkeypatch.setitem(
            client_manager._active_sessions,
            SCOPE,
            client_manager.AgentSession(
                client=SimpleNamespace(), session_id="s-1", context_name="work"
            ),
        )

        with pytest.raises(SandboxStartupError):
            await client_manager.reconnect_session(
                scope=SCOPE,
                context_name="work",
                context=_config().contexts["work"],
            )
