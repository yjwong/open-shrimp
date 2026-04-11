"""Auto-update checker for OpenShrimp PyApp binaries.

Periodically checks GitHub Releases for newer versions, notifies allowed
users via Telegram, and -- upon confirmation -- downloads the new binary,
replaces the running executable in-place, and restarts the process.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import platform
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ── Constants ──

_REPO = "yjwong/open-shrimp"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_CHECK_INTERVAL = 6 * 3600  # 6 hours
_FIRST_CHECK_DELAY = 10  # seconds after startup
_CONFIRM_TIMEOUT = 24 * 3600  # 24 hours

# Map (system, machine) to the release asset name.
_ASSET_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "openshrimp-linux-x86_64",
    ("Linux", "aarch64"): "openshrimp-linux-aarch64",
    ("Darwin", "arm64"): "openshrimp-macos-aarch64",
}


# ── Data ──


@dataclass
class UpdateInfo:
    """Information about an available update."""

    version: str
    download_url: str  # browser_download_url from the asset
    release_url: str  # html_url of the release
    release_notes: str
    asset_name: str


# ── State ──

# Version that was skipped by the user (don't re-notify until a newer one).
_skipped_version: str | None = None

# Pending confirmation future: version -> Future[bool]
_confirm_futures: dict[str, asyncio.Future[bool]] = {}

# Notification messages sent (for cleanup): version -> list of (chat_id, message_id)
_notification_messages: dict[str, list[tuple[int, int]]] = {}

# Lock to prevent concurrent update checks from racing.
_check_lock = asyncio.Lock()


# ── Version helpers ──


def get_current_version() -> str:
    """Return the current installed version."""
    try:
        return importlib.metadata.version("open-shrimp")
    except importlib.metadata.PackageNotFoundError:
        # Fallback: read VERSION file (development mode).
        version_file = Path(__file__).resolve().parent.parent.parent / "VERSION"
        if version_file.is_file():
            return version_file.read_text().strip()
        return "0.0.0"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '1.2.3' into a comparable tuple."""
    v = v.lstrip("v")
    parts: list[int] = []
    for part in v.split("."):
        # Strip pre-release suffixes (e.g. '1.0.0rc1' -> 1, 0, 0).
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def get_platform_asset_name() -> str | None:
    """Return the expected release asset name for this platform, or None."""
    return _ASSET_MAP.get((platform.system(), platform.machine()))


def is_pyapp_binary() -> bool:
    """Check if we're running as a PyApp binary (not from source via uv/python)."""
    exe = os.path.realpath(sys.executable)
    basename = os.path.basename(exe).lower()
    # Running from source: sys.executable points at a python interpreter.
    if basename.startswith("python"):
        return False
    # PyApp sets PYAPP=1 in the environment.
    if os.environ.get("PYAPP"):
        return True
    # Heuristic: if the executable name matches our asset pattern, it's PyApp.
    asset = get_platform_asset_name()
    if asset and asset in exe:
        return True
    # If executable is not python and is a single binary, assume PyApp.
    return not basename.startswith("python")


# ── GitHub API ──


async def check_for_update() -> UpdateInfo | None:
    """Check the GitHub Releases API for a newer version.

    Returns an UpdateInfo if a newer version is available, None otherwise.
    """
    import httpx

    asset_name = get_platform_asset_name()
    if asset_name is None:
        return None

    current = get_current_version()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0
        ) as client:
            resp = await client.get(
                _API_URL,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
    except Exception:
        logger.warning("Failed to check for updates", exc_info=True)
        return None

    data = resp.json()
    tag = data.get("tag_name", "")
    release_version = tag.lstrip("v")

    if _parse_version(release_version) <= _parse_version(current):
        return None

    # Find matching asset.
    for asset in data.get("assets", []):
        if asset.get("name") == asset_name:
            return UpdateInfo(
                version=release_version,
                download_url=asset["browser_download_url"],
                release_url=data.get("html_url", ""),
                release_notes=data.get("body", "") or "",
                asset_name=asset_name,
            )

    logger.warning(
        "Update %s available but no matching asset '%s' found in release",
        release_version,
        asset_name,
    )
    return None


# ── Download and replace ──


async def download_and_replace(update_info: UpdateInfo) -> None:
    """Download the new binary and atomically replace the running one.

    Raises on failure (permission error, disk space, network, etc.).
    """
    import httpx

    target = Path(os.path.realpath(sys.executable))
    tmp = target.with_name(f".{target.name}.update.tmp")

    logger.info(
        "Downloading update %s from %s -> %s",
        update_info.version,
        update_info.download_url,
        target,
    )

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=300.0
        ) as client:
            async with client.stream("GET", update_info.download_url) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)

        # Copy permissions from old binary.
        old_mode = target.stat().st_mode
        tmp.chmod(old_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Atomic replace.
        tmp.rename(target)
        logger.info("Binary updated successfully to %s", update_info.version)

    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ── Telegram notification and confirmation ──


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def _send_update_notification(
    bot: Bot,
    user_ids: list[int],
    update_info: UpdateInfo,
) -> None:
    """Send update notification to all allowed users."""
    current = get_current_version()

    # Truncate release notes.
    notes = update_info.release_notes
    if len(notes) > 500:
        notes = notes[:497] + "..."

    text = (
        f"*Update available*\n\n"
        f"`{_escape_md(current)}` \\-\\> `{_escape_md(update_info.version)}`\n\n"
    )
    if notes.strip():
        text += f"{_escape_md(notes)}\n\n"
    text += f"[View release]({update_info.release_url})"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Update now",
                    callback_data=f"update_confirm:{update_info.version}",
                ),
                InlineKeyboardButton(
                    "Skip",
                    callback_data=f"update_skip:{update_info.version}",
                ),
            ]
        ]
    )

    messages: list[tuple[int, int]] = []
    for uid in user_ids:
        try:
            msg = await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
            messages.append((uid, msg.message_id))
        except Exception:
            logger.warning("Failed to send update notification to %d", uid, exc_info=True)

    _notification_messages[update_info.version] = messages


async def _cleanup_notifications(bot: Bot, version: str) -> None:
    """Remove inline keyboards from sent notifications."""
    for chat_id, message_id in _notification_messages.pop(version, []):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception:
            pass


async def handle_update_callback(
    query: "CallbackQuery",  # noqa: F821
    data: str,
    config: "Config",  # noqa: F821
) -> bool:
    """Handle update_confirm / update_skip callback queries.

    Returns True if the callback was handled.
    """
    if data.startswith("update_confirm:"):
        version = data[len("update_confirm:"):]
        fut = _confirm_futures.get(version)
        if fut and not fut.done():
            fut.set_result(True)
            await query.answer("Downloading update...")
        else:
            await query.answer("Update expired or already handled.")
        return True

    if data.startswith("update_skip:"):
        version = data[len("update_skip:"):]
        fut = _confirm_futures.get(version)
        if fut and not fut.done():
            fut.set_result(False)
            await query.answer("Update skipped.")
        else:
            await query.answer("Already handled.")
        return True

    return False


async def _notify_and_wait(
    bot: Bot,
    config: "Config",  # noqa: F821
    update_info: UpdateInfo,
) -> bool:
    """Notify users and wait for confirmation.

    Returns True if confirmed, False if skipped or timed out.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()
    _confirm_futures[update_info.version] = fut

    await _send_update_notification(
        bot, config.allowed_users, update_info
    )

    try:
        async with asyncio.timeout(_CONFIRM_TIMEOUT):
            result = await fut
    except TimeoutError:
        result = False
    finally:
        _confirm_futures.pop(update_info.version, None)
        await _cleanup_notifications(bot, update_info.version)

    return result


# ── Apply update ──


async def apply_update(
    bot: Bot, config: "Config", update_info: UpdateInfo  # noqa: F821
) -> None:
    """Download the new binary, notify users, and restart.

    Called after the user has confirmed the update (either via the periodic
    check or the manual ``/update`` command).
    """
    try:
        await download_and_replace(update_info)
    except PermissionError:
        logger.error(
            "Permission denied replacing binary. "
            "The bot may need to run as a user with write access to %s",
            sys.executable,
        )
        for uid in config.allowed_users:
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=(
                        f"Update failed: permission denied writing to "
                        f"`{_escape_md(sys.executable)}`\\. "
                        f"Check file ownership/permissions\\."
                    ),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        return
    except Exception:
        logger.exception("Update download/replace failed")
        for uid in config.allowed_users:
            try:
                await bot.send_message(
                    chat_id=uid,
                    text="Update failed\\. Check the bot logs for details\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
        return

    # Notify and restart.
    for uid in config.allowed_users:
        try:
            await bot.send_message(
                chat_id=uid,
                text=f"Update to `{_escape_md(update_info.version)}` downloaded\\. Restarting\\.\\.\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass

    # Set env var so post-restart message shows the new version.
    os.environ["OPENSHRIMP_UPDATE_VERSION"] = update_info.version

    # Trigger restart via the existing mechanism.
    from open_shrimp.main import request_restart

    request_restart()
    os.kill(os.getpid(), __import__("signal").SIGTERM)


# ── Update check job ──


async def _update_check_job(context: "ContextTypes.DEFAULT_TYPE") -> None:  # noqa: F821
    """Periodic job callback: check for updates and notify."""
    global _skipped_version

    if _check_lock.locked():
        return  # Another check is already running.

    async with _check_lock:
        bot = context.bot
        config = context.bot_data["config"]

        update_info = await check_for_update()
        if update_info is None:
            return

        if _skipped_version == update_info.version:
            logger.debug(
                "Skipping notification for already-skipped version %s",
                update_info.version,
            )
            return

        logger.info("Update available: %s", update_info.version)

        confirmed = await _notify_and_wait(bot, config, update_info)
        if not confirmed:
            _skipped_version = update_info.version
            logger.info("Update %s skipped by user", update_info.version)
            return

        await apply_update(bot, config, update_info)


# ── Registration ──


def register_update_checker(app: "Application") -> None:  # noqa: F821
    """Register the periodic update check job on the application's JobQueue.

    Silently skips registration if:
    - Not running as a PyApp binary
    - Platform has no matching asset
    - JobQueue is not available
    """
    config = app.bot_data["config"]
    if not config.auto_update:
        logger.info("Auto-update disabled via config")
        return

    if not is_pyapp_binary():
        logger.debug("Not a PyApp binary — auto-update disabled")
        return

    if get_platform_asset_name() is None:
        logger.debug("Unsupported platform for auto-update")
        return

    job_queue = app.job_queue
    if job_queue is None:
        logger.warning("JobQueue not available — auto-update disabled")
        return

    job_queue.run_repeating(
        _update_check_job,
        interval=_CHECK_INTERVAL,
        first=_FIRST_CHECK_DELAY,
        name="update_checker",
    )
    logger.info(
        "Auto-update checker registered (every %d hours, first check in %ds)",
        _CHECK_INTERVAL // 3600,
        _FIRST_CHECK_DELAY,
    )


# ── CLI update command ──


async def run_update_cli() -> int:
    """Check for updates and apply if available. Returns exit code."""
    current = get_current_version()
    print(f"Current version: {current}")

    asset_name = get_platform_asset_name()
    if asset_name is None:
        print(f"No binary available for this platform ({platform.system()} {platform.machine()}).")
        return 1

    print("Checking for updates...")
    update_info = await check_for_update()

    if update_info is None:
        print("You are up to date.")
        return 0

    print(f"Update available: {update_info.version}")
    if update_info.release_notes:
        notes = update_info.release_notes
        if len(notes) > 500:
            notes = notes[:497] + "..."
        print(f"\n{notes}\n")

    if not is_pyapp_binary():
        print(
            "Auto-update is only supported for PyApp binaries.\n"
            f"Download manually: {update_info.release_url}"
        )
        return 1

    # Prompt for confirmation.
    try:
        answer = input(f"Update to {update_info.version}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if answer not in ("y", "yes"):
        print("Skipped.")
        return 0

    print(f"Downloading {update_info.asset_name}...")
    await download_and_replace(update_info)
    print(f"Updated to {update_info.version}. Restart the bot to use the new version.")
