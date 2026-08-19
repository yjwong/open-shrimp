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
