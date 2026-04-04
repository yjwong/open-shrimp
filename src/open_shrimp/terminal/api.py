"""HTTP API routes for the terminal Mini App.

Provides an SSE endpoint for tailing log sources (background task output,
container build logs, etc.), a REST endpoint for reading their content,
and a WebSocket PTY endpoint for interactive ``claude auth login``.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import shutil
import struct
import termios
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_shrimp.config import Config
from open_shrimp.review.auth import AuthError, authenticate, validate_token_param
from open_shrimp.terminal.jsonl_render import render_jsonl_content, render_jsonl_lines
from open_shrimp.terminal.log_source import LogSource, resolve

logger = logging.getLogger(__name__)


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await authenticate(
        authorization, config.telegram.token, config.allowed_users
    )


def _resolve_source(request: Request) -> LogSource | None:
    """Extract ``type``, ``id``, and optional ``task_type`` from query
    params and resolve to a ``LogSource``."""
    source_type = request.query_params.get("type", "")
    source_id = request.query_params.get("id", "")
    task_type = request.query_params.get("task_type")
    if not source_type or not source_id:
        return None
    sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
    return resolve(
        source_type, source_id, task_type=task_type,
        sandbox_managers=sandbox_managers,
    )


async def tail_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """GET /api/terminal/tail — SSE stream tailing a log source.

    Query params:
        type: Log source type (``"task"`` or ``"container_build"``).
        id: Source identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
        offset: Byte offset to start reading from (default 0).
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    source = _resolve_source(request)
    if source is None:
        return JSONResponse(
            {"error": "type and id are required"}, status_code=400
        )

    offset = int(request.query_params.get("offset", "0"))
    if offset < 0:
        offset = 0

    is_agent = source.render == "jsonl"

    async def event_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events as the file grows."""
        pos = offset
        idle_count = 0
        line_buffer = ""  # carries incomplete JSONL lines (agent only)
        # Maximum idle iterations before we consider the source done.
        # At 0.5s intervals, 120 iterations = 60 seconds of no new output.
        max_idle = 120
        # After we detect the source is no longer active, allow a few more
        # iterations to drain any remaining output before sending done.
        finishing = False
        finish_countdown = 6  # 3 seconds (6 × 0.5s)

        def _flush_and_done(completed: bool) -> str:
            """Flush remaining agent buffer and return done SSE events."""
            parts = ""
            if is_agent and line_buffer.strip():
                rendered, _ = render_jsonl_lines(line_buffer + "\n")
                if rendered:
                    payload = json.dumps({
                        "text": rendered,
                        "offset": pos,
                    })
                    parts += f"data: {payload}\n\n"
            done_data = json.dumps({"completed": completed})
            parts += f"event: done\ndata: {done_data}\n\n"
            return parts

        while True:
            try:
                stat = source.path.stat()
                file_size = stat.st_size
            except FileNotFoundError:
                # File was deleted — source finished and cleaned up.
                yield _flush_and_done(completed=True)
                return

            if file_size > pos:
                # New data available.
                idle_count = 0
                chunk = await asyncio.to_thread(
                    _read_chunk, source.path, pos, file_size
                )
                if chunk:
                    if is_agent:
                        text_to_render = line_buffer + chunk
                        rendered, line_buffer = render_jsonl_lines(
                            text_to_render
                        )
                        if rendered:
                            payload = json.dumps({
                                "text": rendered,
                                "offset": file_size,
                            })
                            yield f"data: {payload}\n\n"
                    else:
                        payload = json.dumps({
                            "text": chunk,
                            "offset": file_size,
                        })
                        yield f"data: {payload}\n\n"
                    pos = file_size
            else:
                # No new data — check if the source has finished.
                if not finishing and not source.is_active():
                    finishing = True
                    finish_countdown = 6

                if finishing:
                    finish_countdown -= 1
                    if finish_countdown <= 0:
                        yield _flush_and_done(completed=True)
                        return

                idle_count += 1
                if idle_count >= max_idle:
                    yield _flush_and_done(completed=False)
                    return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _read_chunk(path: Path, start: int, end: int) -> str:
    """Read a file chunk from start to end byte positions."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    return data.decode("utf-8", errors="replace")


async def read_endpoint(request: Request) -> JSONResponse:
    """GET /api/terminal/read — read the full content of a log source.

    Query params:
        type: Log source type (``"task"`` or ``"container_build"``).
        id: Source identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    source = _resolve_source(request)
    if source is None:
        return JSONResponse(
            {"error": "type and id are required"}, status_code=400
        )

    is_agent = source.render == "jsonl"

    try:
        content = await asyncio.to_thread(source.path.read_text, "utf-8", "replace")
        size = source.path.stat().st_size
    except FileNotFoundError:
        return JSONResponse(
            {"error": "Log source not found"}, status_code=404
        )

    if is_agent:
        content = render_jsonl_content(content)

    return JSONResponse({
        "id": request.query_params.get("id", ""),
        "content": content,
        "size": size,
    })


# ── Login PTY WebSocket endpoint ──
#
# The PTY process is decoupled from the WebSocket lifetime so that the
# user can switch to a browser to complete OAuth and come back without
# the session being killed.  A single ``_LoginSession`` lives at module
# level; WebSocket clients attach/detach freely.
#
# Runs ``claude`` in TUI mode and sends ``/login\n`` to trigger the
# interactive OAuth flow.  The TUI has a built-in paste prompt that
# accepts ``code#state`` via stdin — no localhost callback proxy needed.


class _LoginSession:
    """Background PTY session for ``claude`` TUI login."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        master_fd: int,
    ) -> None:
        self.proc = proc
        self.master_fd = master_fd
        # Circular buffer of terminal output for replay on reconnect.
        self._output_chunks: list[str] = []
        self._output_bytes = 0
        self._MAX_BUFFER = 64 * 1024  # 64 KiB
        self._websocket: WebSocket | None = None
        self._reader_task: asyncio.Task | None = None
        self._done = asyncio.Event()

    def start_background(self) -> None:
        self._reader_task = asyncio.create_task(self._pty_reader())

    # ── Attach / detach WebSocket ──

    async def attach(self, websocket: WebSocket) -> None:
        """Send buffered output, then start forwarding."""
        self._websocket = websocket
        for chunk in self._output_chunks:
            await websocket.send_text(chunk)

    def detach(self) -> None:
        self._websocket = None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def wait(self) -> None:
        await self._done.wait()

    # ── Background tasks ──

    async def _pty_reader(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(
                    None, os.read, self.master_fd, 4096
                )
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                # Buffer for replay.
                self._output_chunks.append(text)
                self._output_bytes += len(text)
                while self._output_bytes > self._MAX_BUFFER:
                    removed = self._output_chunks.pop(0)
                    self._output_bytes -= len(removed)
                # Forward to attached WebSocket.
                ws = self._websocket
                if ws is not None:
                    try:
                        await ws.send_text(text)
                    except Exception:
                        self._websocket = None
        except OSError:
            pass
        finally:
            self._done.set()

    # ── Cleanup ──

    async def destroy(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
        logger.info("Login session destroyed: pid=%d", self.proc.pid or 0)


# The single active login session (if any).
_login_session: _LoginSession | None = None


async def login_ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket PTY: spawn or attach to ``claude`` TUI for /login.

    Runs ``claude`` in interactive TUI mode and sends ``/login`` to
    trigger the OAuth flow.  The TUI's built-in paste prompt accepts
    ``code#state`` via stdin — the user copies the code from the
    Anthropic callback page and pastes it in the mini app.

    The PTY process lives independently of the WebSocket so the user
    can switch to the browser and come back without losing the session.

    Query params:
        token: Telegram initData or HMAC token for authentication.
    """
    global _login_session

    config: Config = websocket.app.state.config
    token = websocket.query_params.get("token", "")

    try:
        await validate_token_param(
            token, config.telegram.token, config.allowed_users
        )
    except AuthError:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    # If there's an existing live session, reattach to it.
    if _login_session is not None and _login_session.alive:
        logger.info("Login WS: reattaching to existing session")
        await _login_session.attach(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                _handle_ws_input(raw, _login_session)
        except WebSocketDisconnect:
            _login_session.detach()
            logger.info("Login WS: client detached (session stays alive)")
        except Exception:
            _login_session.detach()
        return

    # Clean up any dead session.
    if _login_session is not None:
        await _login_session.destroy()
        _login_session = None

    # ── Start a new session ──

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        await websocket.send_text(
            "\x1b[31mError: claude CLI not found in PATH.\x1b[0m\r\n"
        )
        await websocket.close()
        return

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 80, 0, 0),
    )

    # BROWSER=echo: openBrowser() "succeeds" without opening anything,
    # and the TUI falls back to showing the paste prompt after 3s.
    # TERM/COLORTERM: enable 256-color and truecolor output in the TUI.
    env = {
        **os.environ,
        "BROWSER": "echo",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }

    proc = await asyncio.create_subprocess_exec(
        claude_bin, "/login",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    logger.info("Login PTY started: pid=%d", proc.pid or 0)

    session = _LoginSession(proc, master_fd)
    _login_session = session
    session.start_background()
    await session.attach(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            _handle_ws_input(raw, session)
    except WebSocketDisconnect:
        session.detach()
        logger.info("Login WS: client detached (session stays alive)")
    except Exception:
        logger.exception("Login WS error")
        session.detach()

    # Don't destroy the session here — it stays alive for reconnection.
    # It will be cleaned up when the process exits and the next connect
    # finds a dead session, or on a fresh /login.


def _handle_ws_input(raw: str, session: _LoginSession) -> None:
    """Parse a WebSocket message and write to the PTY or resize."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        os.write(session.master_fd, raw.encode("utf-8"))
        return

    msg_type = msg.get("type")
    if msg_type == "stdin":
        os.write(session.master_fd, msg["data"].encode("utf-8"))
    elif msg_type == "resize":
        cols = msg.get("cols", 80)
        rows = msg.get("rows", 24)
        fcntl.ioctl(
            session.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )


def create_terminal_routes() -> list[Route | Mount]:
    """Create the routes for the terminal API and Mini App frontend.

    Returns a list of routes to be added to the main Starlette app.
    """
    _pkg_static = Path(__file__).resolve().parent / "static"
    _dev_dist = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "web"
        / "terminal-app"
        / "dist"
    )
    _dist_dir = _pkg_static if _pkg_static.is_dir() else _dev_dist

    routes: list[Route | Mount] = [
        Route("/api/terminal/tail", tail_endpoint, methods=["GET"]),
        Route("/api/terminal/read", read_endpoint, methods=["GET"]),
        WebSocketRoute("/ws/terminal/login", login_ws_endpoint),
    ]

    if _dist_dir.is_dir():
        routes.append(
            Mount(
                "/terminal",
                app=StaticFiles(directory=str(_dist_dir), html=True),
                name="terminal-app",
            )
        )
        logger.info("Serving terminal Mini App from %s", _dist_dir)
    else:
        logger.warning(
            "Terminal Mini App dist directory not found at %s — "
            "run 'npm run build' in web/terminal-app/ to build the frontend",
            _dist_dir,
        )

    return routes
