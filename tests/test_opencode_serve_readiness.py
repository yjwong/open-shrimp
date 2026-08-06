"""Readiness of a sandboxed ``opencode serve``.

The stream is a real pipe rather than a socket, which is the case the reader
has to handle on every host: only sockets are selectable on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from open_shrimp.sandbox.opencode_runtime import (
    _drain_opencode_output,
    _wait_for_opencode_ready,
)


class _PipeProc:
    """A ``Popen`` stand-in whose stdout is one half of a real OS pipe."""

    def __init__(self):
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r")
        self._writer = os.fdopen(self._write_fd, "w")
        self.returncode: int | None = None

    def write(self, text: str) -> None:
        self._writer.write(text)
        self._writer.flush()

    def exit(self, code: int = 1) -> None:
        self.returncode = code
        self._writer.close()

    def poll(self):
        return self.returncode


def test_readiness_returns_on_the_listening_line():
    proc = _PipeProc()
    proc.write("boot\nserver listening on http://0.0.0.0:4096\n")

    _wait_for_opencode_ready(proc, timeout=5.0)


def test_readiness_is_bounded_while_the_process_stays_silent():
    proc = _PipeProc()
    proc.write("still starting\n")

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="did not become ready"):
        _wait_for_opencode_ready(proc, timeout=0.5)
    assert time.monotonic() - start < 4.0


def test_an_early_exit_is_reported_not_waited_out():
    proc = _PipeProc()
    proc.exit(1)

    with pytest.raises(RuntimeError, match="exited before readiness"):
        _wait_for_opencode_ready(proc, timeout=10.0)


def test_a_late_listening_line_still_arrives():
    proc = _PipeProc()

    def emit():
        time.sleep(0.2)
        proc.write("listening on http://0.0.0.0:4096\n")

    threading.Thread(target=emit, daemon=True).start()
    _wait_for_opencode_ready(proc, timeout=5.0)


def test_the_drain_picks_up_where_readiness_stopped():
    proc = _PipeProc()
    proc.write("listening on http://0.0.0.0:4096\nafter-one\nafter-two\n")

    _wait_for_opencode_ready(proc, timeout=5.0)
    proc.exit(0)
    # Nothing the reader buffered past the readiness line is lost: the drain
    # reads the same stream object.
    _drain_opencode_output(proc, None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only helper")
def test_readiness_over_a_real_subprocess_pipe():
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('listening on 127.0.0.1:4096')"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_for_opencode_ready(proc, timeout=10.0)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
