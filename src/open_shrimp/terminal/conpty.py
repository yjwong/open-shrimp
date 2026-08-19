"""Windows pseudo-console transport — ConPTY.

Windows' pseudo-console is a handle pair plus a process-creation
attribute: ``CreatePseudoConsole`` takes the read end of one pipe and
the write end of another, and ``CreateProcessW`` binds it to the child
through ``PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE``.  The child then sees a
real console, and the terminal stream is whatever crosses the pipes.

Bound with plain ``ctypes`` rather than the ``win32more`` bindings that
:mod:`open_shrimp.sandbox.hcs_win` uses: those ship in the optional
``hcs`` extra, and a pty must not depend on the sandbox backend.

``CreatePseudoConsole`` arrived in Windows 10 1809.  Where it is absent
:class:`~open_shrimp.terminal.pty_transport.PtyUnavailable` is raised
rather than a bare ``AttributeError``.

Imported only on Windows — see
:mod:`open_shrimp.terminal.pty_transport`.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

from open_shrimp.terminal.pty_transport import PtyProcess, PtyUnavailable

if sys.platform != "win32":  # pragma: no cover - import guard
    raise ImportError("open_shrimp.terminal.conpty is Windows-only")

logger = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF
ERROR_BROKEN_PIPE = 109
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
STARTF_USESTDHANDLES = 0x00000100

_READ_SIZE = 4096

# How long the child gets to exit after TerminateProcess, and the budget
# for the whole teardown.  The inner wait must stay under the outer one
# or a healthy teardown reports itself as having overrun.
_TERMINATE_GRACE = 3.0
_TEARDOWN_TIMEOUT = 5.0

# One thread each for the pending read, the exit watch, and teardown.
# Teardown needs a thread of its own: it is what unblocks the other two,
# so it can never be left queued behind them.  A private pool rather
# than the default one, whose threads are shared with the rest of the
# app and would be held for as long as a session lives.
_POOL_SIZE = 3

HPCON = wintypes.HANDLE
LPPROC_THREAD_ATTRIBUTE_LIST = ctypes.c_void_p


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", LPPROC_THREAD_ATTRIBUTE_LIST),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _bind() -> None:
    """Declare the kernel32 signatures this module calls.

    Runs once at import.  Without argtypes, ctypes truncates pointer
    arguments to 32 bits, so every entry point below must be declared
    before any of them is called.
    """
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL

    # Absent before Windows 10 1809; spawn reports that as PtyUnavailable.
    if _CREATE_PSEUDO_CONSOLE is not None:
        _CREATE_PSEUDO_CONSOLE.argtypes = [
            COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(HPCON),
        ]
        _CREATE_PSEUDO_CONSOLE.restype = ctypes.c_long  # HRESULT
        kernel32.ResizePseudoConsole.argtypes = [HPCON, COORD]
        kernel32.ResizePseudoConsole.restype = ctypes.c_long
        kernel32.ClosePseudoConsole.argtypes = [HPCON]
        kernel32.ClosePseudoConsole.restype = None

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        LPPROC_THREAD_ATTRIBUTE_LIST,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        LPPROC_THREAD_ATTRIBUTE_LIST,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [
        LPPROC_THREAD_ATTRIBUTE_LIST
    ]
    kernel32.DeleteProcThreadAttributeList.restype = None

    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL

    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL

    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


_CREATE_PSEUDO_CONSOLE = getattr(kernel32, "CreatePseudoConsole", None)
_bind()


def _raise_last_error(call: str) -> None:
    code = ctypes.get_last_error()
    raise PtyUnavailable(f"{call} failed: {ctypes.WinError(code)}")


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    """Pack *env* as a CreateProcessW unicode environment block.

    CreateProcessW documents the block as sorted by name, case-insensitive.
    """
    entries = sorted(env.items(), key=lambda kv: kv[0].upper())
    packed = "".join(f"{k}={v}\0" for k, v in entries) + "\0"
    return ctypes.create_unicode_buffer(packed)


def _command_line(argv: list[str]) -> str:
    """Render *argv* for CreateProcessW.

    npm installs CLIs as a ``.cmd`` shim, which is a script rather than
    an image — only the command interpreter can start it.
    """
    if os.path.splitext(argv[0])[1].lower() in (".cmd", ".bat"):
        argv = ["cmd.exe", "/c", *argv]
    return subprocess.list2cmdline(argv)


class ConPtyProcess(PtyProcess):
    """A child bound to a Windows pseudo-console."""

    def __init__(
        self,
        hpc: HPCON,
        stdin_write: wintypes.HANDLE,
        stdout_read: wintypes.HANDLE,
        proc_info: PROCESS_INFORMATION,
    ) -> None:
        super().__init__()
        self._hpc = hpc
        self._stdin_write = stdin_write
        self._stdout_read = stdout_read
        self._proc = proc_info
        self._executor = ThreadPoolExecutor(
            max_workers=_POOL_SIZE, thread_name_prefix="conpty"
        )
        # Guards ClosePseudoConsole, which the exit watch and teardown
        # both reach, from different threads.
        self._hpc_lock = threading.Lock()
        self._hpc_closed = False
        self._exit_watch: asyncio.Task | None = None

    @property
    def pid(self) -> int:
        return int(self._proc.dwProcessId)

    def _is_running(self) -> bool:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(
            self._proc.hProcess, ctypes.byref(code)
        ):
            return False
        return code.value == STILL_ACTIVE

    # ── End of stream ──

    async def _watch_exit(self) -> None:
        """Turn the child's exit into an end of stream.

        A pty master reports EOF once the child drops the slave end.  A
        pseudo-console does not: the console host owns the output pipe
        and holds it open for as long as the ``HPCON`` lives, so a read
        outlives the child and blocks forever.  Closing the
        pseudo-console when the child exits is what produces the broken
        pipe that :meth:`_read` reports as end of stream.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor,
            kernel32.WaitForSingleObject,
            self._proc.hProcess,
            INFINITE,
        )
        await loop.run_in_executor(self._executor, self._close_pseudo_console)

    def _close_pseudo_console(self) -> None:
        """Close the pseudo-console once; unblocks a pending read."""
        with self._hpc_lock:
            if self._hpc_closed:
                return
            self._hpc_closed = True
        kernel32.ClosePseudoConsole(self._hpc)

    # ── Transport ──

    def _read_blocking(self) -> bytes:
        buf = ctypes.create_string_buffer(_READ_SIZE)
        read = wintypes.DWORD(0)
        ok = kernel32.ReadFile(
            self._stdout_read, buf, _READ_SIZE, ctypes.byref(read), None
        )
        if not ok:
            code = ctypes.get_last_error()
            if code != ERROR_BROKEN_PIPE:
                logger.debug("ConPTY read failed: %s", ctypes.WinError(code))
            # The console host closed its end — end of stream either way.
            return b""
        return bytes(memoryview(buf)[: read.value])

    async def _read(self) -> bytes:
        # Started here rather than at construction so there is no window
        # in which the object exists but a read would never end.
        if self._exit_watch is None:
            self._exit_watch = asyncio.create_task(self._watch_exit())
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._read_blocking)

    def _write(self, data: bytes) -> None:
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(
            self._stdin_write, data, len(data), ctypes.byref(written), None
        ):
            logger.debug(
                "ConPTY write failed: %s",
                ctypes.WinError(ctypes.get_last_error()),
            )

    def _resize(self, rows: int, cols: int) -> None:
        result = kernel32.ResizePseudoConsole(self._hpc, COORD(cols, rows))
        if result < 0:
            logger.debug(
                "ResizePseudoConsole failed: 0x%08x", result & 0xFFFFFFFF
            )

    # ── Teardown ──

    def _close_blocking(self) -> None:
        if self._is_running():
            kernel32.TerminateProcess(self._proc.hProcess, 1)
            kernel32.WaitForSingleObject(
                self._proc.hProcess, int(_TERMINATE_GRACE * 1000)
            )
        self._close_pseudo_console()
        kernel32.CloseHandle(self._stdin_write)
        kernel32.CloseHandle(self._stdout_read)
        kernel32.CloseHandle(self._proc.hThread)
        kernel32.CloseHandle(self._proc.hProcess)

    async def _close(self) -> None:
        if self._exit_watch is not None:
            self._exit_watch.cancel()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(_TEARDOWN_TIMEOUT):
                await loop.run_in_executor(self._executor, self._close_blocking)
        except TimeoutError:
            logger.warning(
                "ConPTY teardown (pid=%d) did not finish in %.0fs; "
                "handles released with the process",
                self.pid,
                _TEARDOWN_TIMEOUT,
            )
        self._executor.shutdown(wait=False)


def _spawn_blocking(
    argv: list[str], env: dict[str, str], rows: int, cols: int
) -> ConPtyProcess:
    if _CREATE_PSEUDO_CONSOLE is None:
        raise PtyUnavailable(
            "This Windows build has no ConPTY (CreatePseudoConsole arrived "
            "in Windows 10 1809), so no pseudo-terminal is available."
        )

    stdin_read = wintypes.HANDLE()
    stdin_write = wintypes.HANDLE()
    stdout_read = wintypes.HANDLE()
    stdout_write = wintypes.HANDLE()
    if not kernel32.CreatePipe(
        ctypes.byref(stdin_read), ctypes.byref(stdin_write), None, 0
    ):
        _raise_last_error("CreatePipe")
    if not kernel32.CreatePipe(
        ctypes.byref(stdout_read), ctypes.byref(stdout_write), None, 0
    ):
        _raise_last_error("CreatePipe")

    hpc = HPCON()
    result = _CREATE_PSEUDO_CONSOLE(
        COORD(cols, rows), stdin_read, stdout_write, 0, ctypes.byref(hpc)
    )
    # The pseudo-console duplicates both handles; ours are dead weight and
    # holding the write end open would keep the stream from ever ending.
    kernel32.CloseHandle(stdin_read)
    kernel32.CloseHandle(stdout_write)
    if result < 0:
        kernel32.CloseHandle(stdin_write)
        kernel32.CloseHandle(stdout_read)
        raise PtyUnavailable(
            f"CreatePseudoConsole failed: 0x{result & 0xFFFFFFFF:08x}"
        )

    # Size the attribute list, then fill it with the pseudo-console.
    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attr_buf = ctypes.create_string_buffer(size.value)
    attr_list = ctypes.cast(attr_buf, LPPROC_THREAD_ATTRIBUTE_LIST)
    if not kernel32.InitializeProcThreadAttributeList(
        attr_list, 1, 0, ctypes.byref(size)
    ):
        _raise_last_error("InitializeProcThreadAttributeList")
    if not kernel32.UpdateProcThreadAttribute(
        attr_list,
        0,
        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        hpc,
        ctypes.sizeof(HPCON),
        None,
        None,
    ):
        kernel32.DeleteProcThreadAttributeList(attr_list)
        _raise_last_error("UpdateProcThreadAttribute")

    startup = STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    startup.lpAttributeList = attr_list
    # Claim the standard handles but name none of them, which leaves the
    # console subsystem to fill in the pseudo-console's own.  Without
    # this the child inherits whatever the parent holds: a console-hosted
    # parent leaks its console, and a service leaks pipes, so the child
    # writes past the pty and reads EOF from a stdin nobody drives.  It
    # is attached to the pseudo-console either way; only the handles it
    # starts with are at stake.
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    startup.StartupInfo.hStdInput = None
    startup.StartupInfo.hStdOutput = None
    startup.StartupInfo.hStdError = None

    proc_info = PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(_command_line(argv))
    created = kernel32.CreateProcessW(
        None,
        command_line,
        None,
        None,
        False,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
        _environment_block(env),
        None,
        ctypes.byref(startup.StartupInfo),
        ctypes.byref(proc_info),
    )
    kernel32.DeleteProcThreadAttributeList(attr_list)
    if not created:
        code = ctypes.get_last_error()
        kernel32.CloseHandle(stdin_write)
        kernel32.CloseHandle(stdout_read)
        kernel32.ClosePseudoConsole(hpc)
        raise PtyUnavailable(
            f"CreateProcessW({argv[0]}) failed: {ctypes.WinError(code)}"
        )

    return ConPtyProcess(hpc, stdin_write, stdout_read, proc_info)


async def spawn(
    argv: list[str], env: dict[str, str], rows: int, cols: int
) -> PtyProcess:
    """Start *argv* attached to a new pseudo-console."""
    return await asyncio.to_thread(_spawn_blocking, argv, env, rows, cols)
