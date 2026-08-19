"""Tests for the ``/login`` pty transport and session.

The transport is the one part of ``/login`` with a platform-specific
implementation, so these exercise a real child under a real pty rather
than mocking it — a mocked pty cannot show that the window size arrived
or that the stream ever ends.  The Windows half is verified on a Windows
host; here the POSIX half stands in for the shared contract.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from open_shrimp.terminal.api import _LoginSession, _MarkerScanner
from open_shrimp.terminal.pty_transport import PtyProcess, spawn_pty

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX pty; ConPTY is verified on Windows"
)


def test_transport_missing_a_member_cannot_be_constructed():
    """A half-implemented transport must fail loudly, not report itself dead.

    The members carry the closed-state rule, so an implementation that
    inherited a stub would answer ``alive`` with ``None`` — falsy — and
    the session would be torn down as if the child had exited.
    """

    class Incomplete(PtyProcess):
        @property
        def pid(self) -> int:
            return 0

        async def _read(self) -> bytes:
            return b""

        def _write(self, data: bytes) -> None:
            pass

        def _resize(self, rows: int, cols: int) -> None:
            pass

        async def _close(self) -> None:
            pass

        # _is_running is not implemented.

    with pytest.raises(TypeError, match="_is_running"):
        Incomplete()


def test_marker_scanner_matches_across_a_split_and_fires_once():
    scanner = _MarkerScanner("Login successful")
    assert not scanner.feed("nothing here")
    assert not scanner.feed("Login succ")
    assert scanner.feed("essful. Press Enter")
    # Later chunks, including a second occurrence, must not re-fire.
    assert not scanner.feed("Login successful again")


async def _drain_until(pty, marker: bytes, limit: int = 50) -> bytes:
    out = b""
    for _ in range(limit):
        chunk = await pty.read()
        if not chunk:
            break
        out += chunk
        if marker in out:
            break
    return out


@pytest.mark.asyncio
async def test_pty_reports_requested_window_size():
    pty = await spawn_pty(
        ["/bin/sh", "-c", "stty size"], dict(os.environ), rows=40, cols=100
    )
    try:
        out = await _drain_until(pty, b"100")
        assert "40 100" in out.decode()
    finally:
        await pty.close()


@pytest.mark.asyncio
async def test_pty_round_trips_input_and_ends_stream():
    script = 'printf "READY\\n"; read x; printf "GOT[%s]\\n" "$x"'
    pty = await spawn_pty(["/bin/sh", "-c", script], dict(os.environ))
    try:
        await _drain_until(pty, b"READY")
        pty.write(b"code#state\r")

        rest = await _drain_until(pty, b"GOT[")
        assert "GOT[code#state]" in rest.decode()

        # The child exits; the stream must end rather than block forever.
        async with asyncio.timeout(5):
            while await pty.read():
                pass
        assert not pty.alive
    finally:
        await pty.close()


@pytest.mark.asyncio
async def test_pty_close_is_idempotent():
    pty = await spawn_pty(["/bin/sh", "-c", "sleep 30"], dict(os.environ))
    assert pty.alive
    await pty.close()
    await pty.close()
    assert not pty.alive


@pytest.mark.asyncio
async def test_login_session_exits_repl_on_success_marker():
    """The marker is what turns a finished OAuth into a dead subprocess.

    ``/login`` leaves the REPL sitting at its prompt, so the session
    watches for the marker and drives the REPL out; without it the
    subprocess outlives the sign-in.
    """
    # Split the marker across two writes so the sliding-window scan is
    # what catches it, not a single lucky read.
    script = (
        'printf "Login succ"; sleep 0.1; printf "essful\\n"; '
        'read a; read b; printf "SAW[%s]\\n" "$b"'
    )
    pty = await spawn_pty(["/bin/sh", "-c", script], dict(os.environ))
    session = _LoginSession(pty)
    session.start_background()
    try:
        async with asyncio.timeout(15):
            await session.wait()
        assert "SAW[/exit]" in session._output
    finally:
        await session.destroy()


@pytest.mark.asyncio
async def test_session_dies_even_when_the_repl_ignores_exit(monkeypatch):
    """A completed sign-in must end the process, prompt or no prompt.

    Signing in on a fresh profile lands on a workspace-trust dialog that
    swallows ``/exit``.  Nothing else reaps the session — the next
    ``/login`` finds it alive and attaches — so the deadline is what
    makes "signed in" and "finished" the same thing.
    """
    monkeypatch.setattr(_LoginSession, "_SETTLE_AFTER_LOGIN", 0.1)
    monkeypatch.setattr(_LoginSession, "_EXIT_DEADLINE", 1.0)

    # Announces success, then ignores its input forever, as a selection
    # prompt does with a typed command.
    script = 'printf "Login successful\\n"; while true; do sleep 5; done'
    pty = await spawn_pty(["/bin/sh", "-c", script], dict(os.environ))
    session = _LoginSession(pty)
    session.start_background()
    try:
        async with asyncio.timeout(20):
            await session.wait()
        assert not session.alive
    finally:
        await session.destroy()


@pytest.mark.asyncio
async def test_succeeded_session_is_not_reattached_to():
    """A spent session must not be offered to the next /login.

    Attaching would replay whatever the CLI moved on to after signing
    in, which reads as a broken login screen.
    """
    script = 'printf "waiting\\n"; read x'
    pty = await spawn_pty(["/bin/sh", "-c", script], dict(os.environ))
    session = _LoginSession(pty)
    session.start_background()
    try:
        # The session owns the pty; read through it, never alongside it.
        async with asyncio.timeout(10):
            while "waiting" not in session._output:
                await asyncio.sleep(0.05)
        assert session.reusable, "an in-flight sign-in is resumable"

        session._succeeded = True
        assert session.alive, "still running, but spent"
        assert not session.reusable
    finally:
        await session.destroy()
