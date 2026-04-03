"""Entry point for OpenShrimp Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from open_shrimp.bot import run_bot
from open_shrimp.config import DEFAULT_CONFIG_PATH, load_config
from open_shrimp.db import init_db
from open_shrimp.sandbox import SandboxManager, create_sandbox_manager

logger = logging.getLogger("open_shrimp")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenShrimp - Telegram bot for remote Claude access")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    sub_install = subparsers.add_parser(
        "install",
        help="Install OpenShrimp as a system service (systemd/launchd)",
    )
    sub_install.add_argument(
        "--config",
        dest="config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers.add_parser(
        "uninstall",
        help="Remove the OpenShrimp system service",
    )

    return parser.parse_args()


async def _run_http_server(
    config: "Config",  # noqa: F821
    db: "aiosqlite.Connection",  # noqa: F821
    sandbox_manager: SandboxManager | None = None,
) -> None:
    """Run the review API HTTP server."""
    import uvicorn

    from open_shrimp.review.api import create_review_app

    app = create_review_app(config, db, sandbox_manager=sandbox_manager)

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
    await server.serve()


async def run_bot_async(config_path: str, stop_event: asyncio.Event | None = None) -> None:
    """Run the bot and HTTP server until *stop_event* is set.

    This is the shared async entry point used by both the CLI (``main()``)
    and the macOS menu-bar app.  When *stop_event* is ``None`` (the CLI
    path), SIGTERM/SIGINT handlers are installed automatically.
    """
    config = load_config(config_path)
    logger.info("Config loaded from %s", config_path)
    logger.info("Contexts: %s", ", ".join(config.contexts.keys()))

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

    # Set up graceful shutdown
    if stop_event is None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

    sandbox_mgr = create_sandbox_manager(config)

    bot_task = asyncio.create_task(
        run_bot(config, db, config_path=config_path, sandbox_manager=sandbox_mgr)
    )
    http_task = asyncio.create_task(
        _run_http_server(config, db, sandbox_manager=sandbox_mgr)
    )

    await stop_event.wait()
    logger.info("Shutting down...")

    bot_task.cancel()
    http_task.cancel()
    for task in (bot_task, http_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Stop the tunnel if we started one.
    if tunnel_proc is not None:
        from open_shrimp.tunnel import stop_tunnel

        await stop_tunnel(tunnel_proc)

    await db.close()
    logger.info("Shutdown complete")


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

    # Handle install/uninstall subcommands
    if args.subcommand == "install":
        from open_shrimp.service import install_service

        install_service(args.config)
        return

    if args.subcommand == "uninstall":
        from open_shrimp.service import uninstall_service

        uninstall_service()
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY not set — will use Claude Code OAuth if available"
        )

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

    try:
        asyncio.run(_async_main(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
