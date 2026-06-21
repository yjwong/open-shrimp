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
import select
import stat
import subprocess
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
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                raise RuntimeError("sandboxed opencode serve exited before readiness")
            continue
        line = proc.stdout.readline()
        if line:
            stripped = line.rstrip()
            if stripped:
                logger.info("[sandbox opencode] %s", stripped)
                _append_log(log_file, stripped)
            if "listening on" in stripped:
                return
            continue
        if proc.poll() is not None:
            raise RuntimeError("sandboxed opencode serve exited before readiness")
        time.sleep(0.05)
    raise RuntimeError("sandboxed opencode serve did not become ready in time")


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


