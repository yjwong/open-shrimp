"""HTTP API routes for the terminal Mini App.

Provides an SSE endpoint for tailing log sources (background task output,
container build logs, etc.), a REST endpoint for reading their content,
and a WebSocket PTY endpoint for interactive ``claude auth login``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_shrimp.backend.claude_sdk.binary import find_claude_binary
from open_shrimp.backend.claude_sdk.login import login_workspace
from open_shrimp.config import Config
from open_shrimp.handlers.state import is_task_active
from open_shrimp.review.auth import AuthError, authenticate, validate_token_param
from open_shrimp.terminal.transcript import parse_transcript, parse_transcript_lines
from open_shrimp.terminal.log_source import (
    AGENT_TASK_TYPES,
    LogSource,
    list_workflow_agents,
    resolve,
)
from open_shrimp.terminal.pty_transport import (
    PtyProcess,
    PtyUnavailable,
    spawn_pty,
)

logger = logging.getLogger(__name__)


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID."""
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await authenticate(
        authorization, config.telegram.token, config.allowed_users
    )


def _missing_params() -> JSONResponse:
    return JSONResponse({"error": "type and id are required"}, status_code=400)


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "Log source not found"}, status_code=404)


# How often a tail re-scans for the output file of a task that has not
# written any yet.
_PENDING_POLL_INTERVAL = 2.0


def _resolve_source(request: Request) -> LogSource | None:
    """Resolve the ``type``, ``id`` and optional ``task_type`` and ``agent``
    query params to a ``LogSource``, or ``None`` if nothing is on disk for
    them."""
    sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
    return resolve(
        request.query_params["type"],
        request.query_params["id"],
        task_type=request.query_params.get("task_type"),
        sandbox_managers=sandbox_managers,
        agent_id=request.query_params.get("agent"),
    )


def _has_source_params(request: Request) -> bool:
    return bool(request.query_params.get("type")) and bool(
        request.query_params.get("id")
    )


def _is_pending_task(request: Request) -> bool:
    """True for a running task whose output file does not exist yet.

    The Claude CLI opens a task's ``.output`` file on the first write, so
    a Monitor whose script has printed nothing has no file to resolve.  A
    workflow agent's transcript likewise appears only once the agent's
    first message is written.
    """
    return request.query_params["type"] in (
        "task", "workflow_agent",
    ) and is_task_active(request.query_params["id"])


def _is_pending_agent(request: Request) -> bool:
    """Whether a pending source will be an agent transcript once it appears."""
    return (
        request.query_params["type"] == "workflow_agent"
        or request.query_params.get("task_type") in AGENT_TASK_TYPES
    )


async def _wait_for_task_output(
    request: Request, stop_event: asyncio.Event,
) -> LogSource | None:
    """Re-resolve a pending task until its output file appears.

    Returns ``None`` once the task ends without writing, or when
    *stop_event* is set.  The file's session directory is unknown until
    it exists, so this rescans rather than watching one directory.
    """
    task_id = request.query_params["id"]
    while not stop_event.is_set():
        source = await asyncio.to_thread(_resolve_source, request)
        if source is not None:
            return source
        if not is_task_active(task_id):
            return None
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_PENDING_POLL_INTERVAL):
                await stop_event.wait()
    return None


async def tail_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """GET /api/terminal/tail — SSE stream tailing a log source.

    Query params:
        type: Log source type (``"task"``, ``"workflow_agent"`` or
            ``"container_build"``).
        id: Source identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
        agent: Agent ID within the workflow task *id* (only for
            ``type=workflow_agent``).
        offset: Byte offset to start reading from (default 0).

    A running task with no output file yet streams nothing until the
    file appears, then tails it like any other.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    if not _has_source_params(request):
        return _missing_params()
    initial_source = _resolve_source(request)
    if initial_source is None and not _is_pending_task(request):
        return _not_found()

    offset = int(request.query_params.get("offset", "0"))
    if offset < 0:
        offset = 0

    async def event_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events as the file grows.

        Uses ``watchfiles.awatch()`` to react to file changes via native
        OS mechanisms (FSEvents on macOS, inotify on Linux) instead of
        polling with ``stat()``.
        """
        from watchfiles import awatch

        source = initial_source
        is_agent = source is not None and source.render == "jsonl"
        pos = offset
        line_buffer = ""  # carries incomplete JSONL lines (agent only)
        DRAIN_TIMEOUT = 3.0  # seconds to drain after source becomes inactive
        GLOBAL_TIMEOUT = 300.0  # hard cap for orphaned connections

        def _flush_and_done(completed: bool) -> str:
            """Flush remaining agent buffer and return done SSE events."""
            parts = ""
            if is_agent and line_buffer.strip():
                events, _ = parse_transcript_lines(line_buffer + "\n")
                if events:
                    payload = json.dumps({"events": events, "offset": pos})
                    parts += f"data: {payload}\n\n"
            done_data = json.dumps({"completed": completed})
            parts += f"event: done\ndata: {done_data}\n\n"
            return parts

        def _read_new_data() -> tuple[str | None, int]:
            """Read any new bytes past *pos*. Returns (chunk, new_size)."""
            try:
                size = source.path.stat().st_size
            except FileNotFoundError:
                return None, -1
            if size <= pos:
                return "", size
            chunk = _read_chunk(source.path, pos, size)
            return chunk, size

        def _yield_chunk(chunk: str, file_size: int) -> str | None:
            """Render a chunk and return an SSE payload string (or None)."""
            nonlocal line_buffer, pos
            if not chunk:
                return None
            if is_agent:
                events, line_buffer = parse_transcript_lines(line_buffer + chunk)
                pos = file_size
                if not events:
                    return None
                payload = json.dumps({"events": events, "offset": file_size})
                return f"data: {payload}\n\n"
            else:
                pos = file_size
                payload = json.dumps({
                    "text": chunk, "offset": file_size,
                })
                return f"data: {payload}\n\n"

        # -- Coordinating event: signals all watchers to stop --
        stop_event = asyncio.Event()

        async def watch_disconnect() -> None:
            """Detect SSE client disconnect."""
            while not stop_event.is_set():
                if await request.is_disconnected():
                    stop_event.set()
                    return
                await asyncio.sleep(1)

        async def check_completion() -> None:
            """Detect when the source task finishes."""
            await asyncio.sleep(2)  # let the task start producing output
            while not stop_event.is_set():
                if not source.is_active():
                    # Drain period: wait for final writes to land.
                    await asyncio.sleep(DRAIN_TIMEOUT)
                    if not source.is_active():
                        stop_event.set()
                        return
                await asyncio.sleep(2)

        disconnect_task = asyncio.create_task(watch_disconnect())
        completion_task: asyncio.Task[None] | None = None

        try:
            async with asyncio.timeout(GLOBAL_TIMEOUT):
                if source is None:
                    source = await _wait_for_task_output(request, stop_event)
                    if source is None:
                        yield _flush_and_done(
                            completed=not is_task_active(
                                request.query_params["id"]
                            )
                        )
                        return
                    is_agent = source.render == "jsonl"

                completion_task = asyncio.create_task(check_completion())
                parent = source.path.parent

                # Wait for the parent directory to appear (rare race).
                if not parent.exists():
                    for _ in range(20):
                        if parent.exists() or stop_event.is_set():
                            break
                        await asyncio.sleep(0.5)
                    if not parent.exists():
                        yield _flush_and_done(completed=False)
                        return

                # Wait for the file itself to appear.
                if not source.path.exists():
                    async for changes in awatch(
                        parent, stop_event=stop_event
                    ):
                        if source.path.exists():
                            break
                    if stop_event.is_set() and not source.path.exists():
                        yield _flush_and_done(
                            completed=not source.is_active()
                        )
                        return

                # For symlinks (e.g. agent task .output -> .jsonl),
                # resolve to the real file so we watch the correct
                # directory where writes actually happen.
                watch_path = source.path.resolve()
                watch_parent = watch_path.parent

                # Catch-up: read any data already present.
                chunk, file_size = await asyncio.to_thread(_read_new_data)
                if file_size == -1:
                    yield _flush_and_done(completed=True)
                    return
                if chunk:
                    sse = _yield_chunk(chunk, file_size)
                    if sse:
                        yield sse

                # Main watch loop — react to file changes via OS events.
                async for changes in awatch(
                    watch_parent, stop_event=stop_event
                ):
                    if stop_event.is_set():
                        break

                    # Only care about changes to our target file.
                    if not any(
                        Path(p) == watch_path for _, p in changes
                    ):
                        continue

                    chunk, file_size = await asyncio.to_thread(
                        _read_new_data
                    )
                    if file_size == -1:
                        # File deleted.
                        break
                    if chunk:
                        sse = _yield_chunk(chunk, file_size)
                        if sse:
                            yield sse

                # Final read: pick up any bytes written after the last
                # change event but before the stop_event fired.
                chunk, file_size = await asyncio.to_thread(_read_new_data)
                if chunk and file_size > 0:
                    sse = _yield_chunk(chunk, file_size)
                    if sse:
                        yield sse

                yield _flush_and_done(
                    completed=not source.is_active()
                )

        except TimeoutError:
            yield _flush_and_done(completed=False)

        finally:
            stop_event.set()
            for task in (disconnect_task, completion_task):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
        type: Log source type (``"task"``, ``"workflow_agent"`` or
            ``"container_build"``).
        id: Source identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
        agent: Agent ID within the workflow task *id* (only for
            ``type=workflow_agent``).

    The response's ``render`` says how to show it: ``raw`` sources return
    their text as ``content``; agent transcripts (``jsonl``) return
    ``events`` parsed from every complete line, and ``size`` stops at the
    last newline so a tail from there picks up a line still being written.
    ``active`` is whether the source is still being written to.
    A running task with no output file yet reads as empty.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    if not _has_source_params(request):
        return _missing_params()
    source_id = request.query_params["id"]
    source = _resolve_source(request)
    if source is None:
        if not _is_pending_task(request):
            return _not_found()
        if _is_pending_agent(request):
            return JSONResponse({
                "id": source_id, "render": "jsonl", "events": [], "size": 0,
                "active": True,
            })
        return JSONResponse({
            "id": source_id, "render": "raw", "content": "", "size": 0,
            "active": True,
        })

    try:
        data = await asyncio.to_thread(source.path.read_bytes)
    except FileNotFoundError:
        return _not_found()

    if source.render == "jsonl":
        complete = data[: data.rfind(b"\n") + 1]
        return JSONResponse({
            "id": source_id,
            "render": "jsonl",
            "events": parse_transcript(complete.decode("utf-8", "replace")),
            "size": len(complete),
            "active": source.is_active(),
        })
    return JSONResponse({
        "id": source_id,
        "render": "raw",
        "content": data.decode("utf-8", "replace"),
        "size": len(data),
        "active": source.is_active(),
    })


async def workflow_agents_endpoint(request: Request) -> JSONResponse:
    """GET /api/terminal/workflow — the agents a workflow task has started.

    Query params:
        id: The workflow's task ID.

    Each agent's transcript tails as ``type=workflow_agent&id=<task
    id>&agent=<agent_id>``.  A running workflow whose transcripts are not
    found yet lists no agents rather than 404ing, since it may not have
    started any.
    """
    try:
        await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    task_id = request.query_params.get("id")
    if not task_id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    sandbox_managers = getattr(request.app.state, "sandbox_managers", None)
    agents = await asyncio.to_thread(
        list_workflow_agents, task_id, sandbox_managers,
    )
    active = is_task_active(task_id)
    if agents is None:
        if not active:
            return _not_found()
        agents = []
    return JSONResponse({
        "active": active,
        "agents": [
            {
                "agent_id": a.agent_id,
                "label": a.label,
                "phase": a.phase,
                "state": a.state,
            }
            for a in agents
        ],
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


class _MarkerScanner:
    """Reports the first appearance of a marker in a chunked stream.

    Keeps the last few characters of what it has seen so a marker split
    across two reads still matches.
    """

    def __init__(self, marker: str) -> None:
        self._marker = marker
        self._tail = ""
        self._seen = False

    def feed(self, text: str) -> bool:
        """True on the read that completes the marker, once."""
        if self._seen:
            return False
        combined = self._tail + text
        self._tail = combined[-(len(self._marker) - 1):]
        self._seen = self._marker in combined
        return self._seen


class _LoginSession:
    """Background PTY session for ``claude`` TUI login."""

    # Marker printed by ConsoleOAuthFlow when OAuth completes successfully
    # ("Login successful. Press Enter to continue…"). ``/login`` is a slash
    # command that only dismisses its Dialog on Enter — the REPL then sits
    # at the prompt indefinitely. When we see this, we drive the REPL to
    # exit so the subprocess actually terminates.
    _SUCCESS_MARKER = "Login successful"

    # Tail of terminal output replayed to a client that reconnects.
    _MAX_BUFFER = 64 * 1024

    # Let the post-login refreshes settle before asking the REPL to go,
    # then stop waiting for it to agree.
    _SETTLE_AFTER_LOGIN = 2.0
    _EXIT_DEADLINE = 15.0

    def __init__(self, pty: PtyProcess) -> None:
        self._pty = pty
        self._output = ""
        self._websocket: WebSocket | None = None
        self._reader_task: asyncio.Task | None = None
        self._exit_task: asyncio.Task | None = None
        self._done = asyncio.Event()
        self._scanner = _MarkerScanner(self._SUCCESS_MARKER)
        self._succeeded = False

    def start_background(self) -> None:
        self._reader_task = asyncio.create_task(self._pty_reader())

    # ── Attach / detach WebSocket ──

    async def attach(self, websocket: WebSocket) -> None:
        """Send buffered output, then start forwarding."""
        self._websocket = websocket
        if self._output:
            await websocket.send_text(self._output)

    def detach(self) -> None:
        self._websocket = None

    @property
    def alive(self) -> bool:
        return self._pty.alive

    @property
    def reusable(self) -> bool:
        """Whether a new ``/login`` should attach here rather than start over.

        A sign-in the user walked away from mid-flow is worth coming back
        to — that is what outliving the WebSocket is for.  One that has
        already succeeded is spent: attaching would show whatever the CLI
        moved on to instead of a login screen.
        """
        return self._pty.alive and not self._succeeded

    async def wait(self) -> None:
        await self._done.wait()

    # ── Terminal input ──

    def send(self, data: bytes) -> None:
        """Write to the session's terminal."""
        self._pty.write(data)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the session's terminal."""
        self._pty.resize(rows, cols)

    # ── Background tasks ──

    async def _forward(self, text: str) -> None:
        ws = self._websocket
        if ws is None:
            return
        try:
            await ws.send_text(text)
        except Exception:
            self._websocket = None

    async def _pty_reader(self) -> None:
        try:
            while True:
                data = await self._pty.read()
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                self._output = (self._output + text)[-self._MAX_BUFFER:]
                await self._forward(text)
                if self._scanner.feed(text):
                    self._succeeded = True
                    self._exit_task = asyncio.create_task(self._graceful_exit())
        finally:
            self._done.set()

    async def _graceful_exit(self) -> None:
        """End the session once ``/login`` has completed.

        The ``/login`` slash command's ``onDone`` only dismisses the Login
        Dialog — it does not terminate the process. We send:

        1. ``\\r`` to fire the "Press Enter to continue…" confirmation,
           which dismisses the dialog and lets the post-login
           fire-and-forget refreshes (``enrollTrustedDevice``,
           ``refreshPolicyLimits``, etc.) start.
        2. A short delay so those in-flight network calls settle.
        3. ``/exit\\r``, which routes through ``gracefulShutdown`` in the
           Claude Code REPL and terminates the subprocess cleanly.

        Step 3 only lands if the REPL is at its prompt to receive it.  A
        profile signing in for the first time gets a workspace-trust
        dialog instead, which swallows the command, and nothing else
        reaps the session: the next ``/login`` finds it alive and
        attaches to it. So the REPL is asked to leave, not trusted to —
        past the deadline the pty goes regardless. The credentials are
        on disk before the marker prints, so there is nothing left to
        lose by then.
        """
        self.send(b"\r")
        await asyncio.sleep(self._SETTLE_AFTER_LOGIN)
        self.send(b"/exit\r")
        try:
            async with asyncio.timeout(self._EXIT_DEADLINE):
                await self._done.wait()
        except TimeoutError:
            logger.info(
                "Login REPL (pid=%d) did not take /exit; closing the pty",
                self._pty.pid,
            )
            await self._teardown()

    # ── Cleanup ──

    async def _teardown(self) -> None:
        """Stop reading and release the pty.

        The reader is cancelled first: closing the pty under a parked
        read is the ordering that leaves a transport reading a fd that
        is already gone.
        """
        pid = self._pty.pid
        if self._reader_task:
            self._reader_task.cancel()
        await self._pty.close()
        logger.info("Login session destroyed: pid=%d", pid)

    async def destroy(self) -> None:
        """Tear the session down from outside, whatever it was doing."""
        if self._exit_task:
            self._exit_task.cancel()
        await self._teardown()


# The single active login session (if any).
_login_session: _LoginSession | None = None


async def shutdown_login_session() -> None:
    """Destroy any live ``claude /login`` PTY session.

    Called from the bot shutdown path so the long-lived login subprocess
    doesn't outlive the openshrimp service.  Without this, a stale
    ``claude /login`` child sits in the systemd cgroup waiting to be
    reaped by SIGTERM during the next restart, slowing things down.
    """
    global _login_session
    if _login_session is None:
        return
    try:
        await _login_session.destroy()
    except Exception:
        logger.warning("Error destroying login session", exc_info=True)
    _login_session = None


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

    # If a sign-in is still in flight, reattach to it.
    if _login_session is not None and _login_session.reusable:
        logger.info("Login WS: reattaching to existing session")
        await _pump_ws_input(websocket, _login_session)
        return

    # Clean up any session that is finished or dead.
    if _login_session is not None:
        await _login_session.destroy()
        _login_session = None

    # ── Start a new session ──

    try:
        claude_bin = find_claude_binary()
    except RuntimeError as e:
        await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
        await websocket.close()
        return

    # BROWSER=echo: on POSIX, openBrowser() "succeeds" without opening
    # anything, and the TUI falls back to showing the paste prompt after
    # 3s.  Windows ignores it and may hand the URL to the shell — nothing
    # renders on a headless session, and the TUI prints the URL either
    # way, which is what the Mini App scrapes into a tappable button.
    # TERM/COLORTERM: enable 256-color and truecolor output in the TUI.
    env = {
        **os.environ,
        "BROWSER": "echo",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }

    try:
        pty = await spawn_pty(
            [claude_bin, "/login"], env, cwd=str(login_workspace())
        )
    except (PtyUnavailable, OSError) as e:
        logger.warning("Login PTY could not start", exc_info=True)
        await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
        await websocket.close()
        return

    logger.info("Login PTY started: pid=%d", pty.pid)

    session = _LoginSession(pty)
    _login_session = session
    session.start_background()

    # Don't destroy the session when the socket goes — it stays alive for
    # reconnection, and is cleaned up when the process exits and the next
    # connect finds a dead session, or on a fresh /login.
    await _pump_ws_input(websocket, session)


async def _pump_ws_input(
    websocket: WebSocket, session: _LoginSession
) -> None:
    """Replay buffered output, then feed client input to the session."""
    await session.attach(websocket)
    try:
        while True:
            _handle_ws_input(await websocket.receive_text(), session)
    except WebSocketDisconnect:
        logger.info("Login WS: client detached (session stays alive)")
    except Exception:
        logger.exception("Login WS error")
    finally:
        session.detach()


def _handle_ws_input(raw: str, session: _LoginSession) -> None:
    """Parse a WebSocket message and write to the PTY or resize."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        session.send(raw.encode("utf-8"))
        return

    msg_type = msg.get("type")
    if msg_type == "stdin":
        session.send(msg["data"].encode("utf-8"))
    elif msg_type == "resize":
        session.resize(msg.get("rows", 24), msg.get("cols", 80))


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
        Route(
            "/api/terminal/workflow", workflow_agents_endpoint, methods=["GET"],
        ),
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
