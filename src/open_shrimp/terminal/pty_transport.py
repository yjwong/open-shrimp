"""A child process attached to a pseudo-terminal.

A pty is the one piece of running an interactive CLI with no portable
API: POSIX has ``openpty``, Windows has ConPTY, and they agree on
nothing but the byte stream.  This module is that agreement.

:class:`PtyProcess` owns the one rule both platforms share — after
``close`` a transport is inert: reads are end of stream, writes and
resizes are dropped, ``alive`` is false, and closing again does nothing.
Subclasses implement only the platform mechanics, in
:mod:`open_shrimp.terminal.pty_posix` and
:mod:`open_shrimp.terminal.conpty`, each imported only on the platform
it serves — both reach for system APIs the other does not have.

The sole caller today is the ``/login`` terminal Mini App, which runs
the agent CLI's sign-in TUI; nothing here knows that.
"""

from __future__ import annotations

import abc
import sys


class PtyUnavailable(RuntimeError):
    """No pty transport exists for this host."""


class PtyProcess(abc.ABC):
    """A child process attached to a pseudo-terminal.

    Abstract rather than a Protocol so a missing member fails at
    construction: the members carry the closed-state rule, and a
    subclass that silently inherited a stub would report itself dead.
    """

    def __init__(self) -> None:
        self._closed = False

    @property
    @abc.abstractmethod
    def pid(self) -> int:
        """The child's process id, for logging."""

    @property
    def alive(self) -> bool:
        """Whether the child is still running and not yet closed."""
        return not self._closed and self._is_running()

    async def read(self) -> bytes:
        """Read the next chunk of terminal output.

        Returns empty bytes at end of stream — the child has exited and
        the terminal is drained, or the transport is closed.
        """
        if self._closed:
            return b""
        return await self._read()

    def write(self, data: bytes) -> None:
        """Write to the child's terminal input. Never raises."""
        if self._closed:
            return
        self._write(data)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal. Never raises."""
        if self._closed:
            return
        self._resize(rows, cols)

    async def close(self) -> None:
        """Terminate the child and release the terminal. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._close()

    # ── Platform mechanics ──

    @abc.abstractmethod
    def _is_running(self) -> bool:
        """Whether the child process itself has yet to exit."""

    @abc.abstractmethod
    async def _read(self) -> bytes:
        """Read a chunk, or empty bytes at end of stream."""

    @abc.abstractmethod
    def _write(self, data: bytes) -> None:
        """Write to the terminal, swallowing transport errors."""

    @abc.abstractmethod
    def _resize(self, rows: int, cols: int) -> None:
        """Resize the terminal, swallowing transport errors."""

    @abc.abstractmethod
    async def _close(self) -> None:
        """Terminate the child and release platform resources."""


async def spawn_pty(
    argv: list[str],
    env: dict[str, str],
    rows: int = 24,
    cols: int = 80,
) -> PtyProcess:
    """Start *argv* attached to a pseudo-terminal.

    Raises:
        PtyUnavailable: if this host has no usable pty.
    """
    if sys.platform == "win32":
        from open_shrimp.terminal import conpty

        return await conpty.spawn(argv, env, rows, cols)

    from open_shrimp.terminal import pty_posix

    return await pty_posix.spawn(argv, env, rows, cols)
