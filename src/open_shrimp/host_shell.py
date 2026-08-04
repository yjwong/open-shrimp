"""Portable host shell execution for host_bash and host_monitor.

POSIX runs commands via ``/bin/sh -c`` in a new session so the whole
process group can be killed at once.  Windows runs them via Windows
PowerShell's ``-EncodedCommand`` (base64 UTF-16LE): the command string is
never re-parsed by cmd.exe's quoting rules, so metacharacters in a
model-authored (user-approved) command cannot be reinterpreted as extra
arguments (CVE-2024-24576 class), and the process tree is killed with
``taskkill /T``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import sys

WINDOWS = sys.platform == "win32"

# Human-readable name of the shell commands run under, for tool schemas.
SHELL_DESCRIPTION = "PowerShell" if WINDOWS else "/bin/sh -c"


async def spawn_host_shell(
    command: str,
    *,
    cwd: str | None = None,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
) -> asyncio.subprocess.Process:
    """Spawn *command* under the platform shell, killable as a tree."""
    if WINDOWS:
        encoded = base64.b64encode(
            command.encode("utf-16-le")
        ).decode("ascii")
        return await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
        )
    return await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )


async def kill_host_shell_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the shell and every process it spawned; never raises."""
    if proc.returncode is not None:
        return
    if WINDOWS:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
