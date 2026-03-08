"""Entry point for OpenUdang Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from open_udang.bot import run_bot
from open_udang.config import DEFAULT_CONFIG_PATH, load_config
from open_udang.db import init_db

logger = logging.getLogger("open_udang")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenUdang - Telegram bot for remote Claude access")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


async def _async_main(config_path: str) -> None:
    config = load_config(config_path)
    logger.info("Config loaded from %s", config_path)
    logger.info("Contexts: %s", ", ".join(config.contexts.keys()))

    db = await init_db()

    # Set up graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    bot_task = asyncio.create_task(run_bot(config, db))

    await stop_event.wait()
    logger.info("Shutting down...")

    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass

    await db.close()
    logger.info("Shutdown complete")


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    args = _parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY not set — will use Claude Code OAuth if available"
        )

    try:
        asyncio.run(_async_main(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
