"""Process supervision for a single `opencode serve` subprocess."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
from dataclasses import dataclass

from open_shrimp.backend.errors import CLIConnectionError
from open_shrimp.backend.opencode.errors import OpenCodeNotFoundError
from open_shrimp.paths import data_dir
from open_shrimp.sandbox.opencode_plugins import ensure_opencode_plugin_config

logger = logging.getLogger("opencode.serve")


_LISTENING_RE = re.compile(r"listening on (http://\S+)")
_STARTUP_TIMEOUT = 20.0
_AUTH_USERNAME = "opencode"


@dataclass(frozen=True)
class OpenCodeEndpoint:
    """Host-reachable OpenCode server endpoint."""

    base_url: str
    auth_header: str
    owner: object | None = None


def _find_binary() -> str:
    # Single resolution policy lives in the backend's binary module; re-raise
    # as the backend's error type for the served-process spawn path.
    from open_shrimp.backend.opencode.binary import find_opencode_binary

    try:
        return find_opencode_binary()
    except RuntimeError as exc:
        raise OpenCodeNotFoundError(
            "Could not find the `opencode` binary. Set OPENCODE_BIN or install "
            "it at ~/.opencode/bin/opencode."
        ) from exc


class OpenCodeServer:
    _instance: "OpenCodeServer | None" = None
    _lock: asyncio.Lock | None = None

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        base_url: str,
        password: str,
        binary: str,
    ) -> None:
        self.proc = proc
        self.base_url = base_url
        self.password = password
        self.binary = binary
        self._drain_task: asyncio.Task[None] | None = None

    @property
    def auth_header(self) -> str:
        token = base64.b64encode(
            f"{_AUTH_USERNAME}:{self.password}".encode()
        ).decode("ascii")
        return f"Basic {token}"

    @classmethod
    async def get_or_start(cls) -> "OpenCodeServer":
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        async with cls._lock:
            inst = cls._instance
            if inst is not None and inst.proc.returncode is None:
                return inst
            inst = await cls._spawn()
            cls._instance = inst
            return inst

    @classmethod
    async def _spawn(cls) -> "OpenCodeServer":
        binary = _find_binary()
        password = secrets.token_hex(32)
        env = _build_env(password)

        logger.info("spawning %s serve", binary)
        proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            "--print-logs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None

        base_url: str | None = None
        try:
            base_url = await asyncio.wait_for(
                _read_until_listening(proc.stdout), timeout=_STARTUP_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            proc.terminate()
            raise CLIConnectionError(
                f"opencode serve did not print a listening URL within {_STARTUP_TIMEOUT}s"
            ) from exc

        if base_url is None or proc.returncode is not None:
            raise CLIConnectionError(
                "opencode serve exited before printing a listening URL"
            )

        server = cls(proc=proc, base_url=base_url, password=password, binary=binary)
        server._drain_task = asyncio.create_task(_drain(proc.stdout))
        logger.info("opencode serve up at %s", base_url)
        return server

    async def health(self) -> bool:
        return self.proc.returncode is None

    async def stop(self) -> None:
        if self.proc.returncode is not None:
            return
        try:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        finally:
            if self._drain_task is not None:
                self._drain_task.cancel()
            if type(self)._instance is self:
                type(self)._instance = None

    async def restart(self) -> "OpenCodeServer":
        await self.stop()
        return await type(self).get_or_start()


async def _read_until_listening(stream: asyncio.StreamReader) -> str | None:
    while True:
        raw = await stream.readline()
        if not raw:
            return None
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            logger.info("[serve] %s", line)
        m = _LISTENING_RE.search(line)
        if m:
            return m.group(1).rstrip("/")


def _build_env(password: str) -> dict[str, str]:
    env = dict(os.environ)
    env["OPENCODE_SERVER_PASSWORD"] = password
    _configure_plugin_guard(env)
    return env


def _configure_plugin_guard(env: dict[str, str]) -> None:
    if env.get("OPENCODE_CONFIG"):
        logger.warning(
            "OPENCODE_CONFIG is already set; skipping OpenShrimp OpenCode plugin config"
        )
        return

    try:
        config_path = ensure_opencode_plugin_config(data_dir())
    except Exception:
        logger.exception("failed to prepare OpenShrimp OpenCode plugin config")
        return

    env["OPENCODE_CONFIG"] = str(config_path)


async def _drain(stream: asyncio.StreamReader) -> None:
    try:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.debug("[serve] %s", line)
    except asyncio.CancelledError:
        return


__all__ = ["OpenCodeEndpoint", "OpenCodeServer"]
