"""macOS menu bar application for OpenShrimp.

Provides a lightweight GUI wrapper around the existing bot using ``rumps``.
The bot runs in-process on a background thread with its own asyncio event
loop.  Quitting the app stops the bot.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
from pathlib import Path
from textwrap import dedent

import rumps

from open_shrimp.config import DEFAULT_CONFIG_PATH

logger = logging.getLogger("open_shrimp.app")

_LAUNCHD_LABEL = "com.openshrimp.app"
_LAUNCHD_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
)
_LOG_DIR = Path.home() / "Library" / "Logs" / "OpenShrimp"


class OpenShrimpApp(rumps.App):
    """Menu bar application that manages the OpenShrimp Telegram bot."""

    @staticmethod
    def _find_icon(name: str) -> Path | None:
        """Locate a resource file, handling both source and .app bundle layouts."""
        # In a py2app bundle, resources land in Contents/Resources/ directly.
        # In source, they're under platform/macos/resources/.
        for directory in (
            Path(__file__).parent / "resources",  # source tree
            Path(__file__).parent,  # .app bundle
        ):
            path = directory / name
            if path.exists():
                return path
        return None

    @staticmethod
    def _make_menubar_icon() -> "NSImage | None":  # type: ignore[name-defined]  # noqa: F821
        """Build an NSImage with 1x and 2x representations for retina support."""
        from AppKit import NSImage, NSImageRep  # type: ignore[import-not-found]

        icon_1x = OpenShrimpApp._find_icon("menubar-icon.png")
        if icon_1x is None:
            return None

        image = NSImage.alloc().initWithSize_((20, 20))
        image.setTemplate_(True)

        # Add the 1x representation
        rep_1x = NSImageRep.imageRepWithContentsOfFile_(str(icon_1x))
        if rep_1x:
            rep_1x.setSize_((20, 20))
            image.addRepresentation_(rep_1x)

        # Add the 2x representation if available
        icon_2x = OpenShrimpApp._find_icon("menubar-icon@2x.png")
        if icon_2x:
            rep_2x = NSImageRep.imageRepWithContentsOfFile_(str(icon_2x))
            if rep_2x:
                rep_2x.setSize_((20, 20))
                image.addRepresentation_(rep_2x)

        return image

    def __init__(self) -> None:
        super().__init__(
            "OpenShrimp",
            icon=None,
            template=True,
            quit_button=None,  # We add our own Quit item for cleanup
        )

        self._bot_thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._bot_loop: asyncio.AbstractEventLoop | None = None
        self._bot_error: str | None = None

        # Menu items
        self._status_item = rumps.MenuItem("Status: Stopped", callback=None)
        self._status_item.set_callback(None)

        self._start_stop_item = rumps.MenuItem("Start", callback=self._toggle_bot)
        self._open_config_item = rumps.MenuItem("Open Config\u2026", callback=self._open_config)
        self._open_logs_item = rumps.MenuItem("Open Logs\u2026", callback=self._open_logs)
        self._login_item = rumps.MenuItem(
            "Start at Login",
            callback=self._toggle_login,
        )
        self._login_item.state = _LAUNCHD_PLIST_PATH.exists()

        self._quit_item = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self._status_item,
            None,  # separator
            self._start_stop_item,
            None,
            self._open_config_item,
            self._open_logs_item,
            None,
            self._login_item,
            None,
            self._quit_item,
        ]

    # ── Lifecycle ──

    def _did_finish_launching(self) -> None:
        """Called once the run loop is active.  Auto-start if config exists."""
        # Apply the retina-aware menu bar icon now that the NSStatusItem exists.
        icon = self._make_menubar_icon()
        if icon is not None:
            self._icon_nsimage = icon
            self._nsapp.setStatusBarIcon()
            self.title = None

        if Path(DEFAULT_CONFIG_PATH).exists():
            self._start_bot()
        else:
            self._run_setup_wizard()

    def _run_setup_wizard(self) -> None:
        """Launch the first-run setup wizard and start the bot on success."""
        from open_shrimp.platform.macos.app_setup import run_setup_wizard

        self._set_status("Setup…")
        run_setup_wizard(
            on_complete=lambda: self._start_bot(),
            on_cancel=lambda: self._set_status("No config"),
        )

    # ── Bot lifecycle ──

    def _start_bot(self) -> None:
        if self._bot_thread and self._bot_thread.is_alive():
            return

        self._bot_error = None
        self._stop_event = asyncio.Event()
        self._bot_thread = threading.Thread(
            target=self._bot_thread_main,
            name="openshrimp-bot",
            daemon=True,
        )
        self._bot_thread.start()
        self._set_status("Running")
        self._start_stop_item.title = "Stop"

    def _stop_bot(self) -> None:
        if self._stop_event and self._bot_loop:
            self._bot_loop.call_soon_threadsafe(self._stop_event.set)
        if self._bot_thread:
            self._bot_thread.join(timeout=10)
            self._bot_thread = None
        self._set_status("Stopped")
        self._start_stop_item.title = "Start"

    def _bot_thread_main(self) -> None:
        """Run the bot's async entry point in a dedicated event loop."""
        loop = asyncio.new_event_loop()
        self._bot_loop = loop
        asyncio.set_event_loop(loop)

        try:
            from open_shrimp.main import run_bot_async

            loop.run_until_complete(
                run_bot_async(str(DEFAULT_CONFIG_PATH), self._stop_event)
            )
        except Exception as exc:
            self._bot_error = str(exc)
            logger.exception("Bot thread crashed")
            # Schedule status update on the main (rumps) thread
            rumps.Timer(lambda _: self._set_status(f"Error: {self._bot_error}"), 0).start()
        finally:
            loop.close()
            self._bot_loop = None

    # ── Menu callbacks ──

    def _toggle_bot(self, _sender: rumps.MenuItem) -> None:
        if self._bot_thread and self._bot_thread.is_alive():
            self._stop_bot()
        else:
            if not Path(DEFAULT_CONFIG_PATH).exists():
                self._run_setup_wizard()
                return
            self._start_bot()

    def _open_config(self, _sender: rumps.MenuItem) -> None:
        config_path = Path(DEFAULT_CONFIG_PATH)
        if config_path.exists():
            subprocess.Popen(["open", str(config_path)])
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            rumps.notification(
                "OpenShrimp",
                "No config file",
                f"Expected at {config_path}",
            )

    def _open_logs(self, _sender: rumps.MenuItem) -> None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(_LOG_DIR)])

    def _toggle_login(self, sender: rumps.MenuItem) -> None:
        if sender.state:
            # Remove LaunchAgent
            _remove_launch_agent()
            sender.state = False
        else:
            # Install LaunchAgent
            _install_launch_agent()
            sender.state = True

    def _quit(self, _sender: rumps.MenuItem) -> None:
        self._stop_bot()
        rumps.quit_application()

    # ── Helpers ──

    def _set_status(self, status: str) -> None:
        self._status_item.title = f"Status: {status}"
        # Clear the title so only the icon is shown in the menu bar.
        # rumps displays the title *next to* the icon; setting it to None
        # keeps the menu bar clean.
        self.title = None


# ── LaunchAgent management ──


def _get_app_executable() -> Path:
    """Return the executable path for the LaunchAgent plist.

    Inside a ``.app`` bundle, ``sys.argv[0]`` points to the Python script
    under ``Contents/Resources/``, which isn't directly executable by launchd.
    Detect the bundle and return the ``Contents/MacOS/<name>`` binary instead.
    Outside a bundle (e.g. running via the console script), fall back to
    ``sys.executable``.
    """
    script = Path(sys.argv[0]).resolve()
    # py2app layout: <Name>.app/Contents/Resources/<script>.py
    resources_dir = script.parent
    contents_dir = resources_dir.parent
    if (
        resources_dir.name == "Resources"
        and contents_dir.name == "Contents"
        and contents_dir.parent.suffix == ".app"
    ):
        # The bundle executable name matches CFBundleName
        bundle_name = contents_dir.parent.stem  # e.g. "OpenShrimp"
        macos_bin = contents_dir / "MacOS" / bundle_name
        if macos_bin.exists():
            return macos_bin
    # Not in a bundle — use the Python executable directly
    return Path(sys.executable).resolve()


def _install_launch_agent() -> None:
    """Write a LaunchAgent plist that launches the .app at login."""
    app_path = _get_app_executable()

    plist = dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{app_path}</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{_LOG_DIR}/openshrimp-app.stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{_LOG_DIR}/openshrimp-app.stderr.log</string>
        </dict>
        </plist>
    """)
    _LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist)
    logger.info("Installed LaunchAgent at %s", _LAUNCHD_PLIST_PATH)


def _remove_launch_agent() -> None:
    """Remove the LaunchAgent plist."""
    if _LAUNCHD_PLIST_PATH.exists():
        # Unload first (ignore errors if not loaded)
        subprocess.run(
            ["launchctl", "unload", str(_LAUNCHD_PLIST_PATH)],
            capture_output=True,
            check=False,
        )
        _LAUNCHD_PLIST_PATH.unlink()
        logger.info("Removed LaunchAgent at %s", _LAUNCHD_PLIST_PATH)


def main() -> None:
    """Entry point for the macOS menu bar app."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    app = OpenShrimpApp()
    # rumps doesn't expose applicationDidFinishLaunching directly;
    # use a one-shot timer to fire once the run loop is active.
    def _once(timer: rumps.Timer) -> None:
        timer.stop()
        app._did_finish_launching()
    rumps.Timer(_once, 0).start()
    app.run()


if __name__ == "__main__":
    main()
