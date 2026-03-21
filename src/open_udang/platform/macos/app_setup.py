"""First-run setup wizard for the macOS menu bar app.

Uses native macOS dialogs (``rumps.Window`` for text input, ``NSOpenPanel``
for folder selection) to collect the minimal configuration needed to start the
bot:

1. Telegram bot token
2. Allowed Telegram user IDs (optional — defaults to allow-all)
3. One context: folder + name

Writes ``config.yaml`` via :func:`open_udang.config.write_config`.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import rumps

from open_udang.config import DEFAULT_CONFIG_PATH, write_config


def _text_dialog(
    title: str,
    message: str,
    *,
    default_text: str = "",
    secure: bool = False,
) -> str | None:
    """Show a text-input dialog.  Returns the entered text, or ``None`` if cancelled."""
    window = rumps.Window(
        message=message,
        title=title,
        default_text=default_text,
        ok="Next",
        cancel="Cancel",
        secure=secure,
    )
    response = window.run()
    if response.clicked:  # OK / Next
        return response.text.strip()
    return None


def _folder_dialog(title: str = "Choose a project folder") -> str | None:
    """Show a native macOS folder picker.  Returns the selected path or ``None``."""
    try:
        from AppKit import NSOpenPanel
    except ImportError:
        # Fallback: use rumps text input if pyobjc is unavailable
        return _text_dialog(
            "Project Folder",
            "Enter the full path to your project folder:",
        )

    panel = NSOpenPanel.openPanel()
    panel.setTitle_(title)
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setCanCreateDirectories_(False)

    if panel.runModal():  # NSModalResponseOK == 1
        return str(panel.URLs()[0].path())
    return None


def _validate_token(token: str) -> str | None:
    """Return an error string if *token* doesn't look like a bot token."""
    if ":" not in token:
        return "Token should look like '123456:ABC-DEF…' — get one from @BotFather."
    return None


def _validate_user_ids(text: str) -> tuple[list[int], str | None]:
    """Parse comma-separated user IDs.  Returns (ids, error_or_None)."""
    ids: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            uid = int(part)
        except ValueError:
            return [], f"'{part}' is not a valid integer."
        if uid <= 0:
            return [], f"User IDs must be positive (got {uid})."
        ids.append(uid)
    if not ids:
        return [], "Enter at least one user ID."
    return ids, None


def _build_config_dict(
    token: str,
    user_ids: list[int],
    context_name: str,
    directory: str,
    description: str,
) -> dict[str, Any]:
    """Assemble the config dictionary for YAML serialisation."""
    return {
        "telegram": {"token": token},
        "allowed_users": user_ids,
        "contexts": {
            context_name: {
                "directory": directory,
                "description": description,
                "allowed_tools": ["LSP", "AskUserQuestion"],
            },
        },
        "default_context": context_name,
        "review": {
            "port": random.randint(49152, 65535),
            "tunnel": "cloudflared",
        },
    }


def run_setup_wizard() -> bool:
    """Run the first-run setup wizard.

    Returns ``True`` if the config was written successfully, ``False`` if the
    user cancelled at any step.
    """
    # ── Step 1: Bot token ──
    token = _text_dialog(
        "Telegram Bot Token",
        "Paste the bot token you received from @BotFather.\n\n"
        "It looks like '123456:ABC-DEF…'.",
    )
    if not token:
        return False

    err = _validate_token(token)
    if err:
        rumps.alert(title="Invalid Token", message=err)
        return False

    # ── Step 2: Allowed user IDs (optional) ──
    ids_text = _text_dialog(
        "Allowed Users",
        "Enter your Telegram user ID(s), comma-separated.\n\n"
        "Send /start to @userinfobot on Telegram to find yours.\n\n"
        "Leave blank to allow all users (not recommended).",
    )
    if ids_text is None:  # cancelled
        return False

    if ids_text:
        user_ids, err = _validate_user_ids(ids_text)
        if err:
            rumps.alert(title="Invalid User IDs", message=err)
            return False
    else:
        # Allow all — use an empty list; config validation requires at least
        # one entry, so we prompt again.
        rumps.alert(
            title="User ID Required",
            message="At least one Telegram user ID is required for security.\n\n"
            "Send /start to @userinfobot on Telegram to find yours.",
        )
        return False

    # ── Step 3: Context — folder picker + name ──
    directory = _folder_dialog("Choose a project folder for your first context")
    if not directory:
        return False

    context_name = _text_dialog(
        "Context Name",
        f"Give this context a short name (e.g. 'my-project').\n\n"
        f"Folder: {directory}",
        default_text="default",
    )
    if not context_name:
        return False

    description = _text_dialog(
        "Context Description",
        "A short description for this context (optional).",
        default_text="Default context",
    )
    if description is None:  # cancelled
        return False

    # ── Write config ──
    config_dict = _build_config_dict(
        token=token,
        user_ids=user_ids,
        context_name=context_name,
        directory=directory,
        description=description or "Default context",
    )

    config_path = Path(DEFAULT_CONFIG_PATH)
    try:
        write_config(config_path, config_dict)
    except OSError as exc:
        rumps.alert(
            title="Error",
            message=f"Failed to write config:\n{exc}",
        )
        return False

    rumps.alert(
        title="Setup Complete",
        message=f"Config saved to:\n{config_path}\n\n"
        "The bot will now start. You can edit the config later via the menu.",
    )
    return True
