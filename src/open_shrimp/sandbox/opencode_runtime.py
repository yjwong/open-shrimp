"""OpenCode served-endpoint helpers consumed by the OpenCode runtime factory.

These helpers describe per-context host-side state (the data-home and the
plugin-config dir), the host→guest auth-file sync used by the runtime's
``inject`` hook, and the ``opencode serve`` readiness/drain bodies wired
into :class:`~open_shrimp.sandbox.agent_runtime.ServedEndpoint`.  The
sandbox layer consumes only the served-endpoint hooks the runtime
constructor wires up.

Public surface:
- ``OPENCODE_GUEST_PORT`` — fixed in-guest port for sandbox-owned servers.
- ``get_opencode_home_dir`` — per-context host dir mounted as the served home.
- ``get_openshrimp_data_dir`` — per-context host dir for the managed plugin config.
- ``_sync_opencode_auth`` — inject provider-filtered host auth into the sandbox.
- ``_wait_for_opencode_ready`` — block until ``opencode serve`` is listening.
- ``_drain_opencode_output`` — background drain of serve stdout to a log.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import stat
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Fixed in-guest port for sandbox-owned OpenCode servers.
OPENCODE_GUEST_PORT = 4096


def get_opencode_home_dir(context_name: str) -> Path:
    """Return the host-side opencode-home state directory for a context.

    Bind-mounted as ``{SANDBOX_HOME}/.local/share/opencode`` inside the served
    container; holds the resumable session corpus and the synced ``auth.json``.
    """
    from open_shrimp.sandbox.docker_helpers import _ensure_state_dir

    path = _ensure_state_dir(context_name) / "opencode-home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_openshrimp_data_dir(context_name: str) -> Path:
    """Return the host-side OpenShrimp data directory for a context.

    Bind-mounted as ``{SANDBOX_HOME}/.local/share/openshrimp`` inside the served
    container; holds the managed plugin config.
    """
    from open_shrimp.sandbox.docker_helpers import _ensure_state_dir

    path = _ensure_state_dir(context_name) / "openshrimp-data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _host_opencode_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _sync_opencode_auth(provider_id: str | None, opencode_home: Path) -> None:
    if not provider_id:
        return
    host_auth = _host_opencode_auth_path()
    if not host_auth.is_file():
        logger.debug("No host OpenCode auth file found at %s", host_auth)
        return
    try:
        data = json.loads(host_auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Failed to read host OpenCode auth file %s",
            host_auth,
            exc_info=True,
        )
        return
    if not isinstance(data, dict):
        logger.warning(
            "Ignoring host OpenCode auth file with non-object root: %s",
            host_auth,
        )
        return
    provider_auth = data.get(provider_id) or data.get(provider_id.rstrip("/"))
    if provider_auth is None:
        logger.debug(
            "Host OpenCode auth file has no entry for provider %s",
            provider_id,
        )
        return
    opencode_home.mkdir(parents=True, exist_ok=True)
    target = opencode_home / "auth.json"
    content = json.dumps(
        {provider_id.rstrip("/"): provider_auth},
        separators=(",", ":"),
    )
    target.write_text(content, encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _append_log(log_file: Path | None, line: str) -> None:
    if log_file is None:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except OSError:
        logger.debug("Failed to append OpenCode sandbox log", exc_info=True)


def _wait_for_opencode_ready(
    proc: subprocess.Popen[str], *, log_file: Path | None = None,
    timeout: float = 20.0,
) -> None:
    """Block until the serve process announces it is listening.

    The wait is bounded by *timeout* even while the process is alive and
    silent, which rules out a blocking read on the calling thread.  A reader
    thread supplies that bound on every platform: only sockets are selectable
    on Windows, so a ``select`` over the stdout pipe would work on the VM
    backends and fail on the HCS one.  The reader stops at the readiness line,
    and the buffering it stops with belongs to the stream object the drain
    thread goes on to read — so nothing already read is lost.
    """
    assert proc.stdout is not None
    stream = proc.stdout
    lines: queue.Queue[str | None] = queue.Queue()

    def read_until_ready() -> None:
        try:
            for line in iter(stream.readline, ""):
                lines.put(line)
                if "listening on" in line:
                    return
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=read_until_ready, daemon=True).start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("sandboxed opencode serve did not become ready in time")
        try:
            line = lines.get(timeout=min(remaining, 0.2))
        except queue.Empty:
            if proc.poll() is not None:
                raise RuntimeError(
                    "sandboxed opencode serve exited before readiness"
                ) from None
            continue
        if line is None:
            raise RuntimeError("sandboxed opencode serve exited before readiness")
        stripped = line.rstrip()
        if stripped:
            logger.info("[sandbox opencode] %s", stripped)
            _append_log(log_file, stripped)
        if "listening on" in stripped:
            return


def _drain_opencode_output(
    proc: subprocess.Popen[str], log_file: Path | None,
) -> None:
    stream = proc.stdout
    if stream is None:
        return
    for line in stream:
        stripped = line.rstrip()
        if stripped:
            logger.debug("[sandbox opencode] %s", stripped)
            _append_log(log_file, stripped)


