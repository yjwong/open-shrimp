"""Local control channel: a UI process drives the bot over a per-user endpoint.

The endpoint is a Windows named pipe or a Unix domain socket.  Both are
authorised by the operating system — the pipe's default DACL and the socket's
``0600`` mode inside a ``0700`` directory — so no token is exchanged.  That is
the point of choosing them over a loopback port: unlike the MCP proxy, which
binds ``127.0.0.1`` precisely so sandboxes can reach it, nothing but the
owning user may drive process control.  This channel is never mounted on the
main Starlette app, which is published through a tunnel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from platformdirs import user_runtime_path

from open_shrimp.control import protocol

logger = logging.getLogger(__name__)

MethodHandler = Callable[[dict[str, Any]], Awaitable[Any]]


def _endpoint_name(instance_name: str | None) -> str:
    return f"openshrimp-{instance_name}" if instance_name else "openshrimp"


def endpoint_address(instance_name: str | None = None) -> str:
    """The address a client connects to, derived only from the instance name.

    Deterministic and computable without starting the bot, so a UI can find a
    running core without a discovery file.
    """
    name = _endpoint_name(instance_name)
    if sys.platform == "win32":
        return rf"\\.\pipe\{name}-control"
    return str(user_runtime_path("openshrimp") / f"{name}.sock")


class ControlServer:
    """Serves control methods on the per-user endpoint."""

    def __init__(
        self,
        methods: Mapping[str, MethodHandler],
        *,
        instance_name: str | None = None,
    ) -> None:
        self._methods = dict(methods)
        self._address = endpoint_address(instance_name)
        self._clients: set[asyncio.StreamWriter] = set()
        self._unix_server: asyncio.AbstractServer | None = None
        self._pipe_servers: list[Any] = []

    @property
    def address(self) -> str:
        return self._address

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if sys.platform == "win32":
            await self._start_pipe()
        else:
            await self._start_unix()
        logger.info("Control channel listening on %s", self._address)

    async def shutdown(self) -> None:
        for server in self._pipe_servers:
            server.close()
        self._pipe_servers.clear()

        # Drop the clients first: a connected client's handler is parked in a
        # read, and `wait_closed` waits on handlers.  Shutdown is the one path
        # that must not stall — everything downstream of it releases the
        # proxy, the tunnel and the sandbox guests.
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()

        if self._unix_server is not None:
            self._unix_server.close()
            await self._unix_server.wait_closed()
            self._unix_server = None
            # start_unix_server unlinks on close only when it created the
            # socket; do it unconditionally so a later start doesn't see a
            # stale node.
            Path(self._address).unlink(missing_ok=True)

    # -- Transports ----------------------------------------------------------

    async def _start_unix(self) -> None:
        path = Path(self._address)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)

        if path.exists():
            if await _unix_socket_is_live(path):
                raise RuntimeError(_IN_USE.format(address=self._address))
            # Left behind by a core that was killed rather than stopped.
            # missing_ok because the owner may be unlinking it concurrently.
            path.unlink(missing_ok=True)

        self._unix_server = await asyncio.start_unix_server(
            self._handle_client, path=str(path), limit=protocol.MAX_FRAME_BYTES
        )
        os.chmod(path, 0o600)

    async def _start_pipe(self) -> None:
        # Unlike a bound socket, a second named-pipe server with the same name
        # succeeds — asyncio creates pipes with PIPE_UNLIMITED_INSTANCES, so
        # two cores sharing an instance name would both answer and a UI would
        # reach whichever won the race.  Refuse instead.
        if _pipe_is_live(self._address):
            raise RuntimeError(_IN_USE.format(address=self._address))

        loop = asyncio.get_running_loop()
        start_serving_pipe = getattr(loop, "start_serving_pipe", None)
        if start_serving_pipe is None:
            raise RuntimeError(
                "named-pipe transport needs a ProactorEventLoop; "
                f"got {type(loop).__name__}"
            )

        def factory() -> asyncio.Protocol:
            reader = asyncio.StreamReader(limit=protocol.MAX_FRAME_BYTES)
            return asyncio.StreamReaderProtocol(reader, self._handle_client)

        self._pipe_servers = list(await start_serving_pipe(factory, self._address))

    # -- Dispatch ------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients.add(writer)
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    return
                except asyncio.LimitOverrunError:
                    # The reader's limit is the frame cap, so this is the one
                    # place an oversized frame surfaces.  Say so rather than
                    # dropping the connection, which a client cannot tell
                    # apart from the core dying.
                    logger.warning("Control frame exceeded %d bytes", protocol.MAX_FRAME_BYTES)
                    await self._send(
                        writer,
                        protocol.error(
                            None,
                            protocol.ERR_BAD_REQUEST,
                            f"frame exceeds {protocol.MAX_FRAME_BYTES} bytes",
                        ),
                    )
                    return
                await self._dispatch(line, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _dispatch(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            request = protocol.decode(line)
        except ValueError as exc:
            await self._send(writer, protocol.error(None, protocol.ERR_BAD_REQUEST, str(exc)))
            return

        request_id = request.get("id")
        method = request.get("method")
        handler = self._methods.get(method) if isinstance(method, str) else None
        if handler is None:
            await self._send(
                writer,
                protocol.error(
                    request_id, protocol.ERR_UNKNOWN_METHOD, f"unknown method: {method!r}"
                ),
            )
            return

        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            await self._send(
                writer,
                protocol.error(request_id, protocol.ERR_BAD_REQUEST, "params must be an object"),
            )
            return

        try:
            result = await handler(params)
        except Exception as exc:
            logger.exception("Control method %s failed", method)
            await self._send(
                writer, protocol.error(request_id, protocol.ERR_INTERNAL, str(exc))
            )
            return

        await self._send(writer, protocol.response(request_id, result))

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        try:
            writer.write(protocol.encode(message))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self._clients.discard(writer)

    # -- Events --------------------------------------------------------------

    def broadcast(self, name: str, data: Any = None) -> None:
        """Push an event to every connected client.

        Fire-and-forget: a UI that has gone away must never hold up shutdown,
        which is the very thing most events describe.
        """
        frame = protocol.encode(protocol.event(name, data))
        for writer in list(self._clients):
            try:
                writer.write(frame)
            except (ConnectionResetError, BrokenPipeError):
                self._clients.discard(writer)


_IN_USE = (
    "another OpenShrimp core is already serving the control channel at "
    "{address} — give this one a distinct instance_name"
)


def _pipe_is_live(address: str) -> bool:
    """True when a named-pipe server already owns *address*.

    Probes as a client: the pipe exists if it opens, and a busy pipe (every
    instance connected) is still owned by someone.
    """
    import ctypes
    from ctypes import wintypes

    ERROR_FILE_NOT_FOUND = 2
    ERROR_PIPE_BUSY = 231
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        address, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None
    )
    if handle != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(handle)
        return True

    err = ctypes.get_last_error()
    if err == ERROR_FILE_NOT_FOUND:
        return False
    if err == ERROR_PIPE_BUSY:
        return True
    # Anything else (access denied, say) — assume owned rather than stomp on
    # another process's endpoint.
    logger.warning("Could not probe control pipe %s (error %d)", address, err)
    return True


async def _unix_socket_is_live(path: Path) -> bool:
    """True when something is accepting on *path*.

    A socket file left behind by a killed process is indistinguishable from a
    live one by ``stat`` alone; only a connect attempt tells them apart.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=0.5
        )
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except (OSError, asyncio.TimeoutError):
        # Anything else (permissions, an unresponsive peer) — assume live and
        # let the bind fail loudly rather than unlinking someone else's
        # endpoint.
        return True

    writer.close()
    return True
