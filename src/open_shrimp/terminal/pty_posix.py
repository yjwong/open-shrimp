"""POSIX pty transport: ``openpty`` plus a child in its own session.

Imported only on POSIX — see :mod:`open_shrimp.terminal.pty_transport`.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import struct
import sys
import termios

from open_shrimp.terminal.pty_transport import PtyProcess

if sys.platform == "win32":  # pragma: no cover - import guard
    raise ImportError("open_shrimp.terminal.pty_posix is POSIX-only")

logger = logging.getLogger(__name__)

# How long the child gets to exit on a polite signal before it is killed.
_TERMINATE_GRACE = 3.0
_KILL_GRACE = 2.0

_READ_SIZE = 4096


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PosixPtyProcess(PtyProcess):
    """A child holding the slave end of an ``openpty`` pair."""

    def __init__(
        self, proc: asyncio.subprocess.Process, master_fd: int
    ) -> None:
        super().__init__()
        self._proc = proc
        self._master_fd = master_fd

    @property
    def pid(self) -> int:
        return self._proc.pid or 0

    def _is_running(self) -> bool:
        return self._proc.returncode is None

    async def _read(self) -> bytes:
        # The master is selectable, so readability is an event loop
        # concern rather than a thread's: a blocking read parked in the
        # default executor would hold one of its threads for the whole
        # session, and cancelling the await would not release it.
        loop = asyncio.get_running_loop()
        readable = loop.create_future()

        def _on_readable() -> None:
            if not readable.done():
                readable.set_result(None)

        loop.add_reader(self._master_fd, _on_readable)
        try:
            await readable
        finally:
            with contextlib.suppress(OSError, ValueError):
                loop.remove_reader(self._master_fd)

        try:
            return os.read(self._master_fd, _READ_SIZE)
        except OSError:
            # EIO on the master once the last slave fd closes — this
            # pty's end of stream, not a fault.
            return b""

    def _write(self, data: bytes) -> None:
        try:
            os.write(self._master_fd, data)
        except OSError:
            logger.debug("Write to pty failed", exc_info=True)

    def _resize(self, rows: int, cols: int) -> None:
        try:
            _set_winsize(self._master_fd, rows, cols)
        except OSError:
            logger.debug("Resize of pty failed", exc_info=True)

    async def _close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._master_fd)
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                async with asyncio.timeout(_TERMINATE_GRACE):
                    await self._proc.wait()
            except TimeoutError:
                logger.warning(
                    "pty child (pid=%d) did not exit on SIGTERM, killing",
                    self.pid,
                )
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(_KILL_GRACE):
                        await self._proc.wait()


async def spawn(
    argv: list[str], env: dict[str, str], rows: int, cols: int,
    cwd: str | None = None,
) -> PtyProcess:
    """Start *argv* attached to a new pty."""
    master_fd, slave_fd = pty.openpty()
    try:
        _set_winsize(slave_fd, rows, cols)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=cwd,
            # Own session, so the child has a controlling terminal and
            # any children of its own die with it.
            preexec_fn=os.setsid,
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return PosixPtyProcess(proc, master_fd)
