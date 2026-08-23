"""Entry point for OpenShrimp Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_shrimp.bot import run_bot
from open_shrimp.config import DEFAULT_CONFIG_PATH, load_config
from open_shrimp.db import init_db
from open_shrimp.paths import init_paths, log_dir
from open_shrimp.sandbox import SandboxManager, create_sandbox_managers

if TYPE_CHECKING:
    from open_shrimp.backend.claude_sdk.projects import ClaudeProject

logger = logging.getLogger("open_shrimp")

_restart_requested = False

# The stop event (and its loop) of the currently running bot, so
# request_shutdown() can reach it from any thread.
_active_stop_event: asyncio.Event | None = None
_active_stop_event_loop: asyncio.AbstractEventLoop | None = None


def _dump_debug_info() -> None:
    """Dump asyncio tasks and thread stacks to stderr on SIGUSR1/SIGBREAK."""
    import faulthandler

    logger.warning("=== debug-dump signal received — dumping debug info ===")

    # Dump all thread stacks via faulthandler (writes to stderr)
    logger.warning("--- Thread stacks ---")
    faulthandler.dump_traceback(file=sys.stderr)

    # Dump all asyncio tasks
    try:
        loop = asyncio.get_running_loop()
        tasks = asyncio.all_tasks(loop)
        logger.warning("--- Asyncio tasks (%d) ---", len(tasks))
        for task in sorted(tasks, key=lambda t: t.get_name()):
            coro = task.get_coro()
            logger.warning(
                "  Task %s: state=%s coro=%s",
                task.get_name(),
                task._state,
                coro,
            )
            # Print the task's stack frames if available
            frames = task.get_stack()
            for frame in frames:
                logger.warning(
                    "    File %s:%d in %s",
                    frame.f_code.co_filename,
                    frame.f_lineno,
                    frame.f_code.co_name,
                )
    except RuntimeError:
        logger.warning("No running event loop — skipping asyncio task dump")

    logger.warning("=== End debug dump ===")


def request_restart() -> None:
    """Signal that the process should re-exec after shutdown."""
    global _restart_requested
    _restart_requested = True


def request_shutdown() -> None:
    """Trigger the same graceful shutdown a SIGTERM would.

    Safe to call from any thread; a no-op if the bot is not running.
    Exists so in-process callers (e.g. the /restart handler) don't have
    to deliver a signal — ``os.kill(pid, SIGTERM)`` on Windows is an
    unconditional ``TerminateProcess`` that skips shutdown entirely.
    """
    event = _active_stop_event
    if event is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        event.set()
    else:
        # Called from a non-loop thread (signal fallback path).
        _active_loop = _active_stop_event_loop
        if _active_loop is not None:
            _active_loop.call_soon_threadsafe(event.set)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> None:
    """Install SIGTERM/SIGINT shutdown (and debug-dump) handlers portably.

    ``loop.add_signal_handler`` raises :class:`NotImplementedError` on
    Windows (Proactor), so fall back to :func:`signal.signal` there and
    marshal back onto the loop with ``call_soon_threadsafe``.  The debug
    dump binds to SIGUSR1 where it exists and Ctrl+Break (SIGBREAK)
    on Windows.
    """
    global _active_stop_event_loop
    _active_stop_event_loop = loop
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
        loop.add_signal_handler(signal.SIGUSR1, _dump_debug_info)
    except NotImplementedError:
        def _stop(signum: int, frame: object) -> None:
            loop.call_soon_threadsafe(stop_event.set)

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(
                signal.SIGBREAK,
                lambda signum, frame: _dump_debug_info(),
            )


def _defer_windows_shutdown() -> None:
    """Ask Windows to close this process late in a session end.

    A session end closes every process, and it does so in order of shutdown
    level, highest first.  Everything starts at ``0x280``, and this process has
    no window of its own — so on a shutdown it is closed in the same early pass
    as the rest, *before* the tray is asked whether the session may end.  The
    tray then answers, holds the shutdown open, and drains a core that is
    already gone: measured, its stop reports the process exited and writes into
    a broken pipe.

    ``0x100`` is the bottom of the range reserved for applications, which puts
    this process after every ordinary one — including the tray, left at the
    default so that it is still asked first.  That ordering is the whole point:
    it is what buys the sandbox guest the drain the tray is holding the session
    open for.

    Best effort.  Failing to reorder costs a stranded guest on shutdown, which
    is what happens today; it must never cost a start.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # SHUTDOWN_NORETRY is deliberately not set: without it Windows will
        # offer to wait on this process rather than assuming it is hung, which
        # is the same bargain the tray's block reason strikes with the user.
        if not ctypes.WinDLL("kernel32", use_last_error=True).SetProcessShutdownParameters(
            0x100, 0
        ):
            logger.warning(
                "Could not defer this process in the shutdown order (error %d); "
                "a system shutdown may close it before the sandbox is stopped",
                ctypes.get_last_error(),
            )
    except Exception:
        logger.exception("Could not defer this process in the shutdown order")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenShrimp - Telegram bot for remote Claude access")
    _add_config_arg(parser, default=str(DEFAULT_CONFIG_PATH))

    # The cheapest command that still proves the interpreter can import the
    # package: it reads no config, opens no socket, and touches no state.  A
    # supervisor runs it to force a self-installing binary to unpack itself
    # before anything starts timing the boot.
    from open_shrimp.updater import get_current_version

    parser.add_argument(
        "--version",
        action="version",
        version=get_current_version(),
    )

    # Every subcommand accepts --config on either side of the subcommand name.
    # The shared parent suppresses its default so an unset subcommand value
    # cannot overwrite one given before the subcommand name.
    common = argparse.ArgumentParser(add_help=False)
    _add_config_arg(common, default=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser(
        "install",
        parents=[common],
        help=(
            "Install OpenShrimp as a system service "
            "(systemd/launchd/Windows logon task)"
        ),
    )

    subparsers.add_parser(
        "uninstall",
        parents=[common],
        help="Remove the OpenShrimp system service",
    )

    subparsers.add_parser(
        "doctor",
        parents=[common],
        help="Check optional component availability",
    )

    subparsers.add_parser(
        "update",
        parents=[common],
        help="Check for and apply updates",
    )

    sub_models = subparsers.add_parser(
        "models",
        parents=[common],
        help="List the models a context may be pinned to",
    )
    sub_models.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for a setup UI)",
    )

    sub_auth = subparsers.add_parser(
        "auth",
        parents=[common],
        help="Ask whether Claude Code is signed in here, or sign it in",
    )
    auth_subs = sub_auth.add_subparsers(dest="auth_command")
    sub_auth_status = auth_subs.add_parser(
        "status",
        parents=[common],
        help="Report whether Claude Code can authenticate on this host",
    )
    sub_auth_status.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for a setup UI)",
    )
    sub_auth_login = auth_subs.add_parser(
        "login",
        parents=[common],
        help="Run the Claude Code sign-in on this terminal (for a setup UI)",
    )
    sub_auth_login.add_argument(
        "--hold",
        action="store_true",
        help="Wait for Enter before exiting, so a spawned console can be read",
    )

    sub_projects = subparsers.add_parser(
        "projects",
        parents=[common],
        help="Find projects a setup UI can offer to import",
    )
    projects_subs = sub_projects.add_subparsers(dest="projects_command")
    sub_projects_discover = projects_subs.add_parser(
        "discover",
        parents=[common],
        help="List the projects already opened in Claude Code",
    )
    sub_projects_discover.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for a setup UI)",
    )
    sub_projects_name = projects_subs.add_parser(
        "name",
        parents=[common],
        help="What one folder should be called as a context",
    )
    sub_projects_name.add_argument(
        "--path",
        action="append",
        required=True,
        metavar="PATH",
        help="A folder to name; repeat to name a whole selection at once",
    )
    sub_projects_name.add_argument(
        "--taken",
        action="append",
        default=[],
        metavar="NAME",
        help="A name already spoken for, so the answer is unique; repeat per name",
    )
    sub_projects_name.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for a setup UI)",
    )

    sub_sandboxes = subparsers.add_parser(
        "sandboxes",
        parents=[common],
        help="List the sandbox backends this host can run a context in",
    )
    sub_sandboxes.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for a setup UI)",
    )

    sub_sandbox = subparsers.add_parser(
        "sandbox",
        parents=[common],
        help="Act on a sandbox backend itself, rather than on a context",
    )
    sandbox_subs = sub_sandbox.add_subparsers(dest="sandbox_command")
    sub_prefetch = sandbox_subs.add_parser(
        "prefetch",
        parents=[common],
        help="Download the shared assets a backend needs before its first use",
    )
    sub_prefetch.add_argument(
        "--backend",
        metavar="NAME",
        help=(
            "Backend to fetch for; defaults to the one this host would be "
            "offered by 'sandboxes'"
        ),
    )
    sub_prefetch.add_argument(
        "--json",
        action="store_true",
        help="Emit one NDJSON progress object per line (for a setup UI)",
    )

    sub_config = subparsers.add_parser(
        "config",
        parents=[common],
        help="Inspect or write the config file",
    )
    config_subs = sub_config.add_subparsers(dest="config_command")
    sub_config_write = config_subs.add_parser(
        "write",
        parents=[common],
        help="Write a fresh config from a JSON description on stdin",
    )
    sub_config_write.add_argument(
        "--json",
        metavar="PATH",
        default="-",
        help="File to read the JSON description from ('-' for stdin, the default)",
    )
    sub_config_write.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file",
    )
    sub_config_show = config_subs.add_parser(
        "show",
        parents=[common],
        help="Report the settings a front end acts on (no secrets)",
    )
    sub_config_show.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for a front end)",
    )

    return parser.parse_args()


def _add_config_arg(parser: argparse.ArgumentParser, *, default: object) -> None:
    parser.add_argument(
        "--config",
        dest="config",
        default=default,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )


def _create_http_server(
    config: "Config",  # noqa: F821
    db: "aiosqlite.Connection",  # noqa: F821
    sandbox_managers: dict[str, SandboxManager] | None = None,
    config_path: str | None = None,
    security_key_registry: object | None = None,
    port_relay_registry: object | None = None,
) -> "uvicorn.Server":  # noqa: F821
    """Create the review API HTTP server (call ``server.serve()`` to run)."""
    import uvicorn

    from open_shrimp.review.api import create_review_app

    app = create_review_app(
        config,
        db,
        sandbox_managers=sandbox_managers,
        config_path=config_path,
        security_key_registry=security_key_registry,
        port_relay_registry=port_relay_registry,
    )

    server_config = uvicorn.Config(
        app,
        host=config.review.host,
        port=config.review.port,
        log_level="info",
    )
    server = uvicorn.Server(server_config)
    logger.info(
        "Starting review API server on %s:%d",
        config.review.host,
        config.review.port,
    )
    return server


async def _release_resources(
    control: Any,
    mcp_proxy: Any,
    tunnel_proc: "asyncio.subprocess.Process | None",
    db: Any,
) -> None:
    """Release everything the core holds, control channel last.

    A supervising UI treats the endpoint going quiet as the core being down, and
    reaps the process handle it holds.  Release it before the rest and the core
    is killed partway through this function, leaving the tunnel, the proxy's
    stdio servers and the database open behind it.

    Each step is isolated: a failure tearing one down must not skip the rest,
    and must not replace the failure that actually killed the bot — an operator
    debugging a rejected token should not be shown a tunnel error instead.
    """

    async def release(what: str, coro: "Awaitable[Any]") -> None:
        try:
            await coro
        except Exception:
            logger.exception("Failed to stop %s during shutdown", what)

    if mcp_proxy is not None:
        await release("the MCP proxy", mcp_proxy.shutdown())

    if tunnel_proc is not None:
        from open_shrimp.tunnel import stop_tunnel

        await release("the tunnel", stop_tunnel(tunnel_proc))

    await release("the database", db.close())

    if control is not None:
        await release("the control channel", control.shutdown())


async def run_bot_async(config_path: str, stop_event: asyncio.Event | None = None) -> None:
    """Run the bot and HTTP server until *stop_event* is set.

    This is the shared async entry point used by both the CLI (``main()``)
    and the macOS menu-bar app.  When *stop_event* is ``None`` (the CLI
    path), SIGTERM/SIGINT handlers are installed automatically.
    """
    # Before anything that could hold a sandbox guest, because the ordering it
    # asks for only applies to a session end that starts after it.
    _defer_windows_shutdown()

    config = load_config(config_path)
    logger.info("Config loaded from %s", config_path)
    logger.info("Contexts: %s", ", ".join(config.contexts.keys()))

    init_paths(config.instance_name)
    _attach_file_logging()

    # Set up graceful shutdown before anything a supervisor might have to
    # interrupt: ``request_shutdown`` is a no-op while ``_active_stop_event``
    # is None, so a control-channel shutdown arriving before this point would
    # be answered and then ignored.
    if stop_event is None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    global _active_stop_event
    _active_stop_event = stop_event

    # Open the control channel first, before any of the boot work below.  A
    # supervising UI judges liveness by this endpoint and gives up if it does
    # not appear within its handshake window; opening it last would spend that
    # window on a cloudflared download or database setup and get the core
    # killed for being slow rather than wedged.  Nothing here needs the bot to
    # exist — the status it reports is "starting" until it does.
    #
    # Degrade rather than abort: losing the channel costs the UI its controls,
    # not the user their bot.
    from open_shrimp.control import ControlServer, CoreStatus, build_methods

    core_status = CoreStatus(
        state="starting",
        config_path=config_path,
        instance_name=config.instance_name,
        contexts=list(config.contexts),
    )
    control = ControlServer(
        build_methods(core_status), instance_name=config.instance_name
    )
    try:
        await control.start()
    except Exception:
        logger.exception(
            "Control channel failed to start — a supervising UI will not be "
            "able to read status or stop this process gracefully.",
        )
        control = None

    db = await init_db()

    # Start tunnel if configured (before the bot, so public_url is ready).
    tunnel_proc = None
    if config.review.tunnel == "cloudflared" and not config.review.public_url:
        from open_shrimp.tunnel import start_tunnel

        try:
            tunnel_proc, tunnel_url = await start_tunnel(config.review.port)
            config.review.public_url = tunnel_url
            logger.info("Tunnel URL set as public_url: %s", tunnel_url)
        except RuntimeError as e:
            logger.error("Failed to start tunnel: %s", e)
            logger.error(
                "The review app will not be accessible externally. "
                "Set review.public_url manually or fix the tunnel issue."
            )

    sandbox_mgrs = create_sandbox_managers(config)

    # Start the MCP proxy unconditionally — it now serves OpenShrimp's own
    # tools (send_file, edit_topic, schedules, host_bash, computer use) over
    # a host-loopback HTTP endpoint for *every* context, in addition to
    # reverse-proxying external MCP servers for sandboxed contexts.  The
    # proxy runs on a separate listener so that sandboxes cannot reach the
    # main Starlette server (review-app, config-app, etc.).
    #
    # Do not abort boot if it fails: a self-hosted personal bot should keep
    # answering messages even if the local tool listener can't bind.  The
    # session layer treats ``mcp_proxy is None`` as "degraded; omit the
    # OpenShrimp tools and warn the user once".
    #
    # Resolve the backend here (rather than only in ``run_bot``) so its
    # OAuth-source provider can be wired into the proxy at construction.
    from open_shrimp.backend import get_backend
    from open_shrimp.mcp_proxy import McpProxy

    backend = get_backend(config)
    mcp_proxy = McpProxy(backend.mcp_oauth_source())
    try:
        await mcp_proxy.start()
    except Exception:
        logger.exception(
            "MCP proxy failed to start — OpenShrimp tools (send_file, "
            "edit_topic, schedules, host_bash, computer use) will be "
            "UNAVAILABLE this run. Chat still works; restart to retry.",
        )
        mcp_proxy = None

    from open_shrimp.security_key.sessions import SecurityKeySessionRegistry
    from open_shrimp.port_relay.sessions import PortRelaySessionRegistry

    security_key_registry = SecurityKeySessionRegistry()
    port_relay_registry = PortRelaySessionRegistry()

    http_server = _create_http_server(
        config,
        db,
        sandbox_managers=sandbox_mgrs,
        config_path=config_path,
        security_key_registry=security_key_registry,
        port_relay_registry=port_relay_registry,
    )

    def _bot_ready(username: str) -> None:
        core_status.state = "running"
        core_status.bot_username = username or None
        if control is not None:
            control.broadcast("state", {"state": "running", "bot_username": username})

    bot_task = asyncio.create_task(
        run_bot(
            config, db,
            config_path=config_path,
            sandbox_managers=sandbox_mgrs,
            mcp_proxy=mcp_proxy,
            security_key_registry=security_key_registry,
            port_relay_registry=port_relay_registry,
            on_ready=_bot_ready,
        )
    )
    def _bot_finished(task: asyncio.Task) -> None:
        # A bot that dies on its own — a rejected token, say — must unwind the
        # process the way a signal would.  Nothing else sets the stop event, so
        # without this the core sits alive indefinitely: HTTP still serving,
        # sandbox guests still up, and no exit code for whoever started it.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        core_status.state = "error"
        core_status.error = f"{type(exc).__name__}: {exc}"
        if control is not None:
            control.broadcast("state", {"state": "error", "error": core_status.error})
        stop_event.set()

    bot_task.add_done_callback(_bot_finished)

    http_task = asyncio.create_task(http_server.serve())

    await stop_event.wait()
    logger.info("Shutting down...")

    # Tell any supervising UI before the process goes away, so a stop it did
    # not initiate — /restart from Telegram, or an auto-update — reads as a
    # restart rather than a crash.  The re-exec changes the pid, so the UI
    # must reconnect to the endpoint rather than track the child.
    core_status.state = "stopping"
    if control is not None:
        control.broadcast(
            "stopping", {"restarting": _restart_requested}
        )

    # Signal uvicorn to exit gracefully (avoids CancelledError in lifespan).
    http_server.should_exit = True

    bot_task.cancel()
    # A task that died on its own rather than being cancelled — a rejected
    # token, say — must not skip the rest of teardown.  The proxy, the tunnel,
    # the sandbox guests and the control endpoint all still need releasing, so
    # hold the failure and re-raise once cleanup has run.
    task_failure: BaseException | None = None
    for task in (bot_task, http_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Task failed before shutdown")
            if task_failure is None:
                task_failure = exc

    await _release_resources(control, mcp_proxy, tunnel_proc, db)
    logger.info("Shutdown complete")

    # Surfaced only now, so the CLI still exits non-zero on a boot failure.
    if task_failure is not None:
        raise task_failure


def _attach_file_logging() -> None:
    """Log to a rotating file at a path we can name, on every platform.

    A core launched by a menu-bar or tray front end, or by a logon task, writes
    to a console nobody can read; the front end deliberately leaves its output
    streams alone, so without this the log has no home at all.  Owning the file
    here rather than in the supervisor means it survives however the core was
    started.

    Linux is no exception.  The systemd user unit sets neither StandardOutput
    nor StandardError, so stderr does reach the journal and the file is a
    second copy of it — that is accepted, not overlooked.  A journal is only
    readable to someone who will run ``journalctl --user -u open-shrimp``, and
    an operator who would not is exactly the one who needs to hand a
    post-mortem over.  The artifact has to already exist before anybody can be
    asked for it, so it is written unconditionally rather than where we guess
    stderr goes unread.
    """
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return

    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "openshrimp.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Could not open the log file — logging to stderr only")
        return

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    logger.info("Logging to %s", directory)


def _run_models(*, json_output: bool, config_path: str) -> int:
    """List the models a context may be pinned to.

    Exists so a setup UI outside Python can populate its model picker without
    hardcoding the catalog or importing the package.

    Resolved through the configured backend rather than a fixed catalog:
    OpenCode wants provider-qualified ids, so offering Claude aliases there
    would let a setup UI write a config the backend rejects on every turn.
    Falls back to the default backend's catalog when there is no config yet,
    which is the first-run case.
    """
    from open_shrimp.backend import get_backend
    from open_shrimp.config import load_config

    try:
        backend = get_backend(load_config(config_path))
    except Exception:
        from open_shrimp.backend.claude_sdk.models import MODEL_CHOICES
    else:
        MODEL_CHOICES = tuple(backend.model_catalog())

    if json_output:
        json.dump(
            {
                "models": [
                    {
                        "alias": choice.alias,
                        "model_id": choice.model_id,
                        "description": choice.description,
                    }
                    for choice in MODEL_CHOICES
                ]
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        for choice in MODEL_CHOICES:
            print(f"{choice.alias:<8} {choice.model_id:<20} {choice.description}")
    return 0


def _fail(message: str, *, json_output: bool) -> int:
    """Report a subcommand failure in whichever form the caller asked for.

    The GUI contract for every ``--json`` subcommand is one object carrying
    ``ok`` and ``error``, and exit 1; a terminal gets the message on stderr.
    Written once because both desktop wizards decode this shape, and a
    subcommand that spells it differently is one they cannot read.
    """
    if json_output:
        json.dump({"ok": False, "error": message}, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(message, file=sys.stderr)
    return 1


# How each credential is named to somebody reading a terminal.  A UI decodes
# the JSON form instead, so nothing parses these strings.
_HOW_SAID = {
    "api_key": "Signed in — ANTHROPIC_API_KEY is set in this environment.",
    "env_token": "Signed in — CLAUDE_CODE_OAUTH_TOKEN is set in this environment.",
    "oauth": "Signed in to Claude on this computer.",
    None: "Not signed in. Run 'openshrimp auth login' to sign in.",
}


def _run_auth_status(*, json_output: bool) -> int:
    """Report whether the Claude CLI on this host can authenticate.

    The two GUI wizards cannot call Python, so their sign-in step reads the
    answer from here.

    Only a check that could not run at all exits non-zero, so a caller can
    tell "no credentials" from "I could not look".  A wizard renders the first
    as the step it is about to offer.
    """
    from open_shrimp.backend.claude_sdk.login import auth_status

    try:
        status = auth_status()
    except Exception as exc:
        logger.debug("The sign-in check could not run", exc_info=True)
        return _fail(str(exc), json_output=json_output)

    if json_output:
        json.dump(
            {"ok": True, "signed_in": status.signed_in, "how": status.how},
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        print(_HOW_SAID[status.how])
    return 0


def _run_auth_login(*, hold: bool) -> int:
    """Run the Claude sign-in on whatever terminal this was started from.

    The CLI draws a prompt, a browser opens, and a person finishes it, so the
    output is written for a reader.  A GUI wizard spawns a console for it and
    reads the exit code.

    *hold* is for that console.  A window that closes the instant the child
    exits takes the last line with it, including the one saying the sign-in
    did not work.
    """
    from open_shrimp.backend.claude_sdk.login import run_interactive_login

    try:
        signed_in = run_interactive_login()
    except (RuntimeError, OSError) as exc:
        # find_claude_binary's message names every path it searched, which is
        # what a reader of this console needs; the traceback goes to the log.
        logger.debug("The Claude sign-in could not start", exc_info=True)
        print(f"{exc}", file=sys.stderr)
        signed_in = False
    else:
        print()
        print(
            "Signed in to Claude."
            if signed_in
            else "Not signed in. You can try again with /login in Telegram."
        )

    if hold:
        try:
            input("Press Enter to close this window. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0 if signed_in else 1


def _emit_projects(projects: list[ClaudeProject], *, json_output: bool) -> int:
    """Report importable projects in the one shape every front end decodes.

    ``discover`` and ``name`` answer the same question about a different number
    of folders, so they answer it in the same shape: a GUI that has a decoder
    for one has a decoder for both, and cannot end up naming a folder it picked
    differently from one the core found.

    The text listing carries both names, because they are different answers to
    different questions: ``talenthub.glints.com`` is the folder a person
    recognises and ``talenthub-glints-com`` is what they will type after
    ``/context``.  A listing with only one of them leaves the reader mapping it
    onto their own filesystem by eye.
    """
    if json_output:
        json.dump(
            {
                "projects": [
                    {
                        "directory": project.directory,
                        "name": project.name,
                        "context_name": project.context_name,
                        "last_start_time": project.last_start_time,
                    }
                    for project in projects
                ]
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        for project in projects:
            print(
                f"{project.context_name:<24} {project.name:<24} {project.directory}"
            )
    return 0


def _run_projects_discover(*, json_output: bool) -> int:
    """List the projects a setup UI can offer to import.

    The filter that decides what counts as a project lives in Python, and the
    two GUI wizards cannot call Python, so they read it from here.  A machine
    with no ``~/.claude.json`` reports an empty list and exits zero: "nothing
    to import" is an answer a wizard renders, not a failure it reports.
    """
    from open_shrimp.backend.claude_sdk.projects import discover_claude_projects

    return _emit_projects(discover_claude_projects(), json_output=json_output)


def _run_projects_name(*, paths: list[str], taken: list[str], json_output: bool) -> int:
    """What the folders the user picked by hand should be called.

    The rule for a legal context name has one implementation, and a folder name
    is under no obligation to obey it — so a front end with a folder picker
    asks here rather than offering the basename.  Without this a folder found
    by discovery and the same folder picked by hand are named differently by
    the same wizard.

    A whole selection is named in one call, so a picker that allows several
    folders pays one spawn and their uniqueness against each other is settled
    where uniqueness is already owned, rather than by the caller looping.
    *taken* is what the caller has already used, because uniqueness is also a
    property of the list being built and only the caller knows it.
    """
    from open_shrimp.backend.claude_sdk.projects import name_directory

    already = set(taken)
    named = []
    for path in paths:
        project = name_directory(path, already)
        already.add(project.context_name)
        named.append(project)

    return _emit_projects(named, json_output=json_output)


def _run_sandboxes(*, json_output: bool) -> int:
    """List the isolation choices a setup UI may offer for this host.

    The same reason ``projects discover`` exists: which backends this
    platform can run, and whether their prerequisites are met, is decided by
    ``doctor`` in Python, and the two GUI wizards cannot call Python.  A
    wizard offering a backend this host cannot start would write a config
    that fails on its first turn.

    ``sandbox`` is the answer to setup's one question, already resolved:
    which backend to write, whether it can be turned on, and the sentence to
    say either way.  A GUI renders it and branches on nothing — deciding any
    part of it per front end is how three wizards came to disagree about a
    host that can offer no sandbox at all.
    """
    from open_shrimp.doctor import (
        _load_config,
        blessed_offer,
        sandbox_note,
        sandbox_offers,
    )

    config = _load_config(None)
    # Several checks look under the managed data directory, which is only
    # locatable once the instance name has been read off the config — or
    # settled as the unscoped one, when there is no config to read.
    init_paths(config.instance_name if config is not None else None)
    offers = sandbox_offers(config)
    blessed = blessed_offer(config)

    if json_output:
        json.dump(
            {
                "sandbox": {
                    # Null where this platform has no sandbox at all, which is
                    # why it is not the empty string: a backend nobody can
                    # name is not a backend called "".
                    "backend": blessed.backend if blessed is not None else None,
                    "available": blessed is not None and blessed.available,
                    "note": sandbox_note(blessed),
                },
                "sandboxes": [
                    {
                        "backend": offer.backend,
                        "label": offer.label,
                        "summary": offer.summary,
                        "available": offer.available,
                        "detail": offer.detail,
                    }
                    for offer in offers
                ],
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        for offer in offers:
            state = "ready" if offer.available else offer.detail
            mark = "*" if blessed is not None and offer.backend == blessed.backend else " "
            print(f"{mark} {offer.backend:<10} {offer.label:<10} {state}")
    return 0


def _run_sandbox_prefetch(
    *, backend: str | None, json_output: bool, config_path: str | None = None,
) -> int:
    """Download the shared assets a backend needs, reporting as they arrive.

    A sandboxed context's first turn otherwise pays for a multi-gigabyte
    download with no way to say so — the user sends a message and gets
    silence.  Running this beforehand moves the wait somewhere it can be
    shown, which is the whole reason the output streams instead of arriving
    at the end.

    NDJSON on stdout, one object per line, flushed as each is written: a
    ``{"asset", "done", "total"}`` per progress tick, an ``{"asset",
    "state": "ready"}`` as each asset lands, and a closing ``{"state":
    "finished"}``.  ``total`` is absent — never zero — when the server sent
    no ``Content-Length``, so a front end renders it as indeterminate.
    Failure adds a closing ``{"state": "error", "reason": …}`` and exits
    non-zero; the reason a front end shows comes from there and not from
    stderr, which carries it surrounded by whatever the logging handlers
    wrote.
    """
    from open_shrimp.doctor import _load_config, blessed_offer
    from open_shrimp.sandbox.prefetch import prefetch

    config = _load_config(config_path)
    # Every asset is cached under the managed data directory, which is only
    # locatable once the instance name has been read off the config — and it
    # must be *this* config's, or a named instance's assets land in a tree its
    # core never looks at and the first turn fetches them all again.
    init_paths(config.instance_name if config is not None else None)

    if backend is None:
        blessed = blessed_offer(config)
        if blessed is None:
            print(
                "This host has no sandbox backend, so there is nothing to "
                "prefetch.",
                file=sys.stderr,
            )
            return 1
        backend = blessed.backend

    def emit_json(event: dict[str, object]) -> None:
        json.dump(event, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()

    def emit_text(event: dict[str, object]) -> None:
        name = event.get("asset")
        if event.get("state") == "ready":
            # Overwrites the carriage-returned percentage in place, padded so
            # no tail of the longer line it replaces survives.
            print(f"\r{name}: ready".ljust(40))
        elif event.get("state") == "finished":
            print("All assets ready.")
        else:
            done = int(event.get("done", 0))
            total = event.get("total")
            if isinstance(total, int) and total > 0:
                print(f"\r{name}: {done * 100 // total}%", end="", flush=True)
            else:
                print(f"\r{name}: {done // (1024 * 1024)} MiB", end="", flush=True)

    emit = emit_json if json_output else emit_text
    try:
        prefetch(backend, emit=emit)
    except Exception as exc:
        # The reason goes out on stdout as a final event, not on stderr.  A
        # front end shows it to a user verbatim, and stderr is where the
        # logging handlers write too — so anything read from there is the
        # reason with an unpredictable number of log lines around it, which
        # is how "limactl not found, attempting auto-download" ends up
        # presented to somebody as the explanation for a failure.
        #
        # Stderr still carries it for an operator reading a terminal, where
        # the surrounding log lines are the point rather than the problem.
        logger.debug(
            "Sandbox prefetch failed for backend '%s'", backend, exc_info=True,
        )
        if json_output:
            emit({"state": "error", "reason": str(exc)})
        print(f"{exc}", file=sys.stderr)
        return 1
    return 0


def _context_from_entry(entry: object, position: int) -> tuple[str, dict[str, Any]]:
    """Turn one entry of a JSON description into a named context.

    Everything the wire schema decides about a single context is decided
    here, so the command around it is left with reading stdin, refusing to
    clobber a config, and writing one.  A ``ValueError`` names the problem
    in the words the caller will report.

    Only what a first config may fairly settle is read.  A sandbox entry
    names a backend and nothing else, so a wizard cannot reach
    ``allow_host_escape`` through the one question it asks about isolation.
    """
    from open_shrimp.config import (
        _validate_context_name,
        build_context_dict,
        check_directory,
    )

    if not isinstance(entry, dict):
        raise ValueError(f"context {position} must be an object")

    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError(f"context {position} has no name")
    # Reserved names and stray punctuation are refused here rather than at the
    # next boot, while the UI that collected them can still correct it.
    name_error = _validate_context_name(name)
    if name_error:
        raise ValueError(f"context '{name}': {name_error}")

    # The terminal wizard checks this before writing; without it here a setup
    # UI reports success, the bot boots fine, and every agent turn then fails
    # on a working directory that does not exist — far from the UI that could
    # still have corrected it.
    if not str(entry.get("directory") or "").strip():
        raise ValueError(f"context '{name}' has no directory")
    directory = check_directory(str(entry["directory"]))
    if not directory["exists"]:
        raise ValueError(f"directory does not exist: {directory['path']}")

    return name, build_context_dict(
        directory["path"],
        str(entry.get("description") or name),
        str(entry.get("model") or "").strip() or None,
        str(entry.get("sandbox") or "").strip() or None,
    )


def _run_config_write(args: argparse.Namespace) -> int:
    """Write a fresh config from a JSON description.

    The first-run path for any front end that cannot call into Python — the
    terminal wizard is unreachable there because it needs a tty, and the
    config HTTP API needs a bot that is already running.  Keeping the schema
    on this side is what stops a second implementation from drifting.

    Reads ``{token, user_id, contexts: [{name, directory, description?,
    model?, sandbox?}]}`` and reports the outcome as JSON so a caller can
    parse a failure rather than scrape it.

    The context list may be empty.  That is what a wizard's "Skip" produces,
    and it is a config the core starts from: the user reaches the OpenShrimp
    context and adds projects by chat.
    """
    from open_shrimp.config import _validate_raw, write_config
    from open_shrimp.setup import build_config_dict

    def _fail(message: str) -> int:
        json.dump({"ok": False, "error": message}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    try:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text("utf-8")
    except OSError as exc:
        return _fail(f"could not read the JSON description: {exc}")
    except UnicodeDecodeError:
        # Windows PowerShell 5.1's Out-File writes UTF-16 by default, so this
        # is a realistic way for a GUI to hand us a file we cannot read.  It
        # must still come back as parsable JSON, not a traceback.
        return _fail("the JSON description must be UTF-8")

    try:
        spec = json.loads(raw)
    except ValueError as exc:
        return _fail(f"invalid JSON: {exc}")
    if not isinstance(spec, dict):
        return _fail("the JSON description must be an object")

    missing = [key for key in ("token", "user_id") if not spec.get(key)]
    if missing:
        return _fail(f"missing required field(s): {', '.join(missing)}")

    try:
        user_id = int(spec["user_id"])
    except (TypeError, ValueError):
        return _fail("user_id must be a number")

    raw_contexts = spec.get("contexts", [])
    if not isinstance(raw_contexts, list):
        return _fail("contexts must be a list")

    contexts: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(raw_contexts, 1):
        try:
            name, context = _context_from_entry(entry, position)
        except ValueError as exc:
            return _fail(str(exc))
        # A dict would keep the last one silently, and the wizard would report
        # an import of two projects that produced one.
        if name in contexts:
            return _fail(f"two contexts are both called '{name}'")
        contexts[name] = context

    config_path = Path(args.config)
    if config_path.exists() and not args.force:
        return _fail(f"{config_path} already exists — pass --force to overwrite")

    config_dict = build_config_dict(str(spec["token"]), user_id, contexts)

    # write_config does not validate, so a bad description would otherwise be
    # written out and only rejected at the next boot.
    try:
        _validate_raw(config_dict)
    except ValueError as exc:
        return _fail(str(exc))

    try:
        write_config(config_path, config_dict)
    except OSError as exc:
        return _fail(f"could not write {config_path}: {exc}")

    json.dump({"ok": True, "config_path": str(config_path)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _run_config_show(args: argparse.Namespace) -> int:
    """Report the settings a front end outside Python acts on.

    The control channel answers only while the core runs, and the caller this
    is for is a supervisor deciding whether to install a version over a core
    that is stopped.

    Only those settings, and never the file: it holds the bot token, and
    printing it leaves a copy wherever the caller sent stdout.  Read through
    the loader, so the defaults are the ones the running core would apply.
    """
    from open_shrimp.config import load_config

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        return _fail(str(exc), json_output=args.json)

    settings = {
        "instance_name": config.instance_name,
        "auto_update": config.auto_update,
    }

    if args.json:
        json.dump({"ok": True, "config": settings}, sys.stdout)
        sys.stdout.write("\n")
    else:
        for key, value in settings.items():
            print(f"{key}: {'null' if value is None else value}")
    return 0


async def _async_main(config_path: str) -> None:
    await run_bot_async(config_path)


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    args = _parse_args()

    # A previous update may have left the superseded binary on disk because
    # the process still running it had it mapped.  Nothing maps it now.
    from open_shrimp.updater import purge_displaced_binary

    purge_displaced_binary()

    # Handle install/uninstall subcommands
    if args.subcommand == "install":
        from open_shrimp.service import install_service

        install_service(args.config)
        return

    if args.subcommand == "uninstall":
        from open_shrimp.service import uninstall_service

        uninstall_service(args.config)
        return

    if args.subcommand == "doctor":
        from open_shrimp.doctor import run_doctor

        sys.exit(run_doctor(args.config))

    if args.subcommand == "update":
        from open_shrimp.updater import run_update_cli

        sys.exit(asyncio.run(run_update_cli()))

    if args.subcommand == "models":
        sys.exit(_run_models(json_output=args.json, config_path=args.config))

    if args.subcommand == "auth":
        if args.auth_command == "status":
            sys.exit(_run_auth_status(json_output=args.json))
        if args.auth_command == "login":
            sys.exit(_run_auth_login(hold=args.hold))
        print(
            "usage: openshrimp auth (status [--json] | login [--hold])",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.subcommand == "projects":
        if args.projects_command == "discover":
            sys.exit(_run_projects_discover(json_output=args.json))
        if args.projects_command == "name":
            sys.exit(
                _run_projects_name(
                    paths=args.path, taken=args.taken, json_output=args.json
                )
            )
        print(
            "usage: openshrimp projects (discover | name --path DIR) [--json]",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.subcommand == "sandboxes":
        sys.exit(_run_sandboxes(json_output=args.json))

    if args.subcommand == "sandbox":
        if args.sandbox_command == "prefetch":
            sys.exit(
                _run_sandbox_prefetch(
                    backend=args.backend,
                    json_output=args.json,
                    config_path=args.config,
                )
            )
        print(
            "usage: openshrimp sandbox prefetch [--backend NAME] [--json]",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.subcommand == "config":
        if args.config_command == "write":
            sys.exit(_run_config_write(args))
        if args.config_command == "show":
            sys.exit(_run_config_show(args))
        print(
            "usage: openshrimp config write [--json PATH] [--force]\n"
            "       openshrimp config show [--json]",
            file=sys.stderr,
        )
        sys.exit(2)

    # Offer guided setup when config is missing and running interactively.
    config_path = Path(args.config)
    if not config_path.exists():
        if sys.stdin.isatty():
            from open_shrimp.setup import run_setup_wizard

            try:
                run_setup_wizard(config_path)
            except SystemExit:
                return
            # Config file now exists; fall through to normal startup.
        else:
            logger.error(
                "Config file not found: %s — "
                "run interactively to use the setup wizard, "
                "or copy config.example.yaml and edit it manually.",
                config_path,
            )
            sys.exit(1)

    failed = False
    try:
        asyncio.run(_async_main(args.config))
    except KeyboardInterrupt:
        pass
    except Exception:
        # A failure must not swallow a pending restart.  /restart and the
        # auto-updater both request one before the core unwinds, so letting
        # this propagate would leave the bot down until someone started it
        # by hand — while still reporting the failure and the exit code.
        logger.exception("Bot exited with an error")
        failed = True

    if _restart_requested:
        logger.info("Re-executing process for restart...")
        import shutil

        from open_shrimp.updater import pyapp_binary_path

        pyapp = pyapp_binary_path()
        if pyapp:
            _reexec([str(pyapp)] + sys.argv[1:])
        else:
            uv = shutil.which("uv")
            if uv:
                # Re-exec via uv run so the venv is rebuilt if needed,
                # matching the systemd ExecStart invocation.
                _reexec([uv, "run", "openshrimp"] + sys.argv[1:])
            else:
                _reexec([sys.executable] + sys.argv)

    if failed:
        sys.exit(1)


def _reexec(argv: list[str]) -> None:
    """Replace this process with *argv*.

    ``os.execv`` on Windows does not replace the process image — it spawns
    a child with naive argument joining and returns the console to the
    shell while both run.  Spawn an ordinary child and exit instead.
    """
    if sys.platform == "win32":
        import subprocess

        subprocess.Popen(argv)
        sys.exit(0)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
