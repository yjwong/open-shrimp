#!/usr/bin/env python3
"""A core that serves the control channel and nothing else.

Exists so the supervisor can be driven through every state it has without a
Telegram token.  A real core needs one, and a second consumer of the same token
takes ``409 Conflict`` forever — so verifying the supervisor against the real
thing would mean taking the running bot down for the length of the test.

The control server, the method handlers and the framing are the real ones,
imported from ``src/``.  What is stubbed is only what sits *around* them: the
bot, the database, the sandbox.  The sequence below mirrors ``run_bot_async``,
including the order that matters — the channel opens first, before anything
that can be slow — and the re-exec that ``/restart`` ends in.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import types
from pathlib import Path

VERSION = "0.0.0-stand-in"


def _install_stubs(src: Path) -> None:
    """Put ``src/`` on the path with the two modules the handlers reach for.

    ``control/methods.py`` imports them lazily from inside its handlers, so
    stubbing them keeps the import graph to the control package alone.
    """
    sys.path.insert(0, str(src))

    main = types.ModuleType("open_shrimp.main")
    main.request_shutdown = _request_shutdown  # type: ignore[attr-defined]
    main.request_restart = _request_restart  # type: ignore[attr-defined]
    sys.modules["open_shrimp.main"] = main

    updater = types.ModuleType("open_shrimp.updater")
    updater.get_current_version = lambda: VERSION  # type: ignore[attr-defined]
    sys.modules["open_shrimp.updater"] = updater


_stop_event: asyncio.Event | None = None
_restart_requested = False


def _request_shutdown() -> None:
    if _stop_event is not None:
        _stop_event.set()


def _request_restart() -> None:
    global _restart_requested
    _restart_requested = True


async def _serve(args: argparse.Namespace) -> None:
    global _stop_event

    from open_shrimp.control import ControlServer, CoreStatus, build_methods

    _stop_event = asyncio.Event()

    status = CoreStatus(
        state="starting",
        config_path=args.config or "(none)",
        instance_name=args.instance,
        contexts=["default"],
    )
    control = ControlServer(build_methods(status), instance_name=args.instance)
    await control.start()
    print(f"[stand-in {os.getpid()}] listening on {control.address}", flush=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown)

    # Stands in for the bot's own startup.  Anything the supervisor should see
    # as "starting" belongs before this line.
    await asyncio.sleep(args.boot_delay)
    status.state = "running"
    status.bot_username = args.bot_username
    control.broadcast("state", {"state": "running", "bot_username": args.bot_username})
    print(f"[stand-in {os.getpid()}] running as @{args.bot_username}", flush=True)

    await _stop_event.wait()

    # Tell the supervisor before the process goes away, so a stop it did not
    # initiate reads as a restart rather than a crash.
    status.state = "stopping"
    control.broadcast("stopping", {"restarting": _restart_requested})
    print(f"[stand-in {os.getpid()}] stopping (restart={_restart_requested})", flush=True)

    # Stands in for the sandbox and tunnel teardown that makes a graceful stop
    # worth waiting for.
    await asyncio.sleep(args.teardown_delay)
    await control.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=None, help="path to the repo's src/ directory")
    parser.add_argument("--config", default=None)
    parser.add_argument("--instance", default=None, help="instance_name to scope the endpoint")
    parser.add_argument("--bot-username", default="standinbot")
    parser.add_argument("--boot-delay", type=float, default=0.0)
    parser.add_argument("--teardown-delay", type=float, default=0.0)
    args = parser.parse_args()

    src = Path(args.src) if args.src else Path(__file__).resolve().parents[2] / "src"
    _install_stubs(src)

    asyncio.run(_serve(args))

    if _restart_requested:
        # ``os.execv`` replaces the image and keeps the pid, so a supervisor
        # that judged liveness by pid would see nothing happen at all.  The
        # endpoint is rebound, which is the signal that does arrive.
        print(f"[stand-in {os.getpid()}] re-executing", flush=True)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # The line that distinguishes a graceful stop from a kill: reaching it
    # means the teardown ran to completion.
    print(f"[stand-in {os.getpid()}] exited cleanly", flush=True)


if __name__ == "__main__":
    main()
