"""Interactive setup wizard for first-time OpenShrimp configuration."""

from __future__ import annotations

import asyncio
import glob as globmod
import logging
import os
import random
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx

from open_shrimp import enrollment

logger = logging.getLogger(__name__)

# readline is a Unix-only stdlib extension.  Windows ships no equivalent, so
# path completion is absent there and the prompts fall back to plain input().
try:
    import readline
except ImportError:
    readline = None  # type: ignore[assignment]

from open_shrimp.backend.claude_sdk.models import MODEL_CHOICES


def _path_completer(text: str, state: int) -> str | None:
    """Readline completer for filesystem paths."""
    # Expand ~ before globbing
    expanded = os.path.expanduser(text)
    # Add trailing wildcard for globbing, append * only if not already there
    pattern = expanded + "*" if not expanded.endswith("*") else expanded
    matches = globmod.glob(pattern)
    # Append / to directories so the user can keep tabbing
    matches = [m + "/" if os.path.isdir(m) else m for m in matches]
    # If the original text started with ~, keep the ~ prefix in completions
    if text.startswith("~") and not expanded.startswith("~"):
        home = os.path.expanduser("~")
        matches = ["~" + m[len(home):] if m.startswith(home) else m for m in matches]
    return matches[state] if state < len(matches) else None


@contextmanager
def _path_completion() -> Iterator[bool]:
    """Bind tab to filesystem path completion, restoring the old completer after.

    Yields:
        Whether completion is actually active, so callers can word their
        prompts accordingly.
    """
    if readline is None:
        yield False
        return

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(_path_completer)
    readline.set_completer_delims(" \t\n")
    # libedit (macOS) uses different syntax from GNU readline (Linux).
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        yield True
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


# The wizard writes a Claude SDK config, so it offers that backend's catalog.
# "Custom model name" is the entry one past the end of this tuple.
_MODELS: tuple[tuple[str | None, str], ...] = (
    (None, "use CLI default (recommended)"),
    *((c.alias, c.description) for c in MODEL_CHOICES),
)


def _prompt(
    label: str,
    *,
    default: str | None = None,
    validator: Callable[[str], str | None] | None = None,
) -> str:
    """Prompt the user for input with optional default and validation.

    Args:
        label: The prompt text shown to the user.
        default: Default value if the user presses Enter without typing.
        validator: A callable that returns an error message string if the
            value is invalid, or None if it's valid.

    Returns:
        The validated user input.

    Raises:
        SystemExit: If the user presses Ctrl-C or Ctrl-D.
    """
    suffix = f" [{default}]: " if default else ": "
    while True:
        try:
            value = input(f"{label}{suffix}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nSetup cancelled.")
            raise SystemExit(0)

        if not value:
            if default is not None:
                return default
            print("  This field is required.")
            continue

        if validator:
            error = validator(value)
            if error:
                print(f"  {error}")
                continue

        return value


def _validate_token(value: str) -> str | None:
    """Validate a Telegram bot token has the expected format."""
    if ":" not in value:
        return "Token should look like '123456:ABC-DEF...' — get one from @BotFather."
    return None


def _validate_user_id(value: str) -> str | None:
    """Validate a Telegram user ID is a positive integer."""
    try:
        uid = int(value)
    except ValueError:
        return "Must be an integer."
    if uid <= 0:
        return "Must be a positive integer."
    return None


def _validate_directory(value: str) -> str | None:
    """Validate a directory path exists."""
    p = Path(value).expanduser()
    if not p.is_dir():
        return f"Directory does not exist: {p}"
    return None


def _validate_context_name(value: str) -> str | None:
    """Validate a context name is a simple identifier."""
    if not value.replace("-", "").replace("_", "").isalnum():
        return "Use only letters, numbers, hyphens, and underscores."
    return None


def _prompt_yes_no(label: str, *, default: bool = True) -> bool:
    """Ask a yes/no question, defaulting on a bare Enter."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            value = input(f"{label} {suffix}: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n\nSetup cancelled.")
            raise SystemExit(0)
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("  Please answer y or n.")


def _open_client() -> httpx.AsyncClient:
    """The HTTP client every Telegram call in the wizard runs on.

    One seam, so a test can drive the whole handshake — backlog drain, poll,
    code delivery, offset confirmation — against a fake transport.
    """
    return httpx.AsyncClient()


_T = TypeVar("_T")


def _telegram(work: Callable[[httpx.AsyncClient], Awaitable[_T]]) -> _T:
    """Run one piece of Telegram work, on a client of its own.

    The wizard is a synchronous prompt loop, so each of its network steps is its
    own event loop rather than one loop the whole wizard lives inside.  Funnelled
    through here so ``_open_client`` stays the single seam a test replaces.
    """

    async def _go() -> _T:
        async with _open_client() as client:
            return await work(client)

    return asyncio.run(_go())


def _prompt_token() -> tuple[str, enrollment.BotIdentity]:
    """Prompt for the bot token and verify it before going any further.

    Verifying here rather than at boot means a well-formed typo is caught
    while the person who typed it is still looking at the prompt, and it is
    what yields the username the enrollment step tells them to search for.
    """
    print("  Create a bot via @BotFather on Telegram and paste the token here.")
    token: str | None = None

    while True:
        if token is None:
            token = _prompt("Telegram bot token", validator=_validate_token)
        candidate_token = token

        print("  Checking the token with Telegram…")
        try:
            identity = _telegram(lambda c: enrollment.get_me(c, candidate_token))
        except enrollment.TokenRejected as exc:
            print(f"  Telegram rejected that token: {exc}")
            token = None
            continue
        except enrollment.EnrollmentError as exc:
            print(f"  Could not reach Telegram: {exc}")
            # Keeping the token across a network failure means the operator
            # retypes it only when it is the token that was wrong.
            if not _prompt_yes_no("  Try again with the same token?"):
                token = None
            continue

        print(f"  Connected to @{identity.username}.")
        return token, identity


def _describe_conflict() -> None:
    """What to say when another client already owns this token's poll."""
    print()
    print("  Another OpenShrimp is already connected to this bot.")
    print("  Stop it first — quit the running bot, or close its tray/menu-bar")
    print("  app — then try again.  Two of them cannot share one token.")


def _prompt_manual_user_id() -> int:
    """The escape hatch for an operator who is not holding the phone.

    Kept because authorising an ID for a device you do not have in front of
    you is a real case; it is no longer the only way a headless install works,
    because the code handshake needs no rendering at all.
    """
    print()
    print("  A wrong number here produces a bot that ignores you, with no")
    print("  error anywhere.  Only paste an ID you are sure of.")
    return int(_prompt("Telegram user ID", validator=_validate_user_id))


def _confirm_candidate(candidate: enrollment.Candidate) -> bool:
    """Name the person *and* the consequence before anything is written."""
    print()
    print(f"  {candidate.label} messaged your bot.")
    print("  Adding them lets them read and change files on this computer.")
    return _prompt_yes_no("  Yes, that's me?", default=False)


@dataclass(frozen=True)
class _Enrolled:
    """Who the handshake settled on, and what is owed to Telegram afterwards.

    ``offset`` is carried out of the wizard rather than confirmed inside it,
    because confirming updates the core will never see is only correct once the
    config those updates enrolled somebody into is actually on disk.
    """

    user_id: int
    chat_id: int | None
    thread_id: int | None
    offset: int


def _prompt_operator(token: str, identity: enrollment.BotIdentity) -> _Enrolled:
    """Run the enrollment handshake and return who to write into the allowlist.

    The bot sends a six-digit code to whoever messages it; the operator types
    that code here.  Nothing but a message the bot can already send has to
    reach the phone, and the only typing happens on this keyboard — which is
    why this works unchanged over ssh.
    """
    handle = f"@{identity.username}" if identity.username else "your bot"

    # A loop rather than recursion: "start a new window" is a normal outcome,
    # and each retry would otherwise leave a frame behind.
    while True:
        try:
            offset = _telegram(lambda c: enrollment.drain_backlog(c, token))
        except enrollment.PollConflict:
            _describe_conflict()
            if _prompt_yes_no("  Try again?"):
                continue
            return _Enrolled(_prompt_manual_user_id(), None, None, 0)
        except enrollment.EnrollmentError as exc:
            print(f"  Could not reach Telegram: {exc}")
            return _Enrolled(_prompt_manual_user_id(), None, None, 0)

        window = enrollment.EnrollmentWindow()

        def _announce(candidate: enrollment.Candidate) -> None:
            if candidate.authenticated:
                print(f"\n  Link opened by {candidate.label} — press Enter to continue.")
            else:
                print(f"\n  Sent a setup code to {candidate.label}.")

        def _flood() -> None:
            print("\n  Several people have messaged this bot. That's unusual during")
            print("  setup — check the name before you continue.")

        def _closed() -> None:
            # Said as it happens, not on the next keystroke.  `input()` cannot
            # be interrupted, so an operator waiting on a code that will never
            # come would otherwise face a prompt that has quietly stopped
            # meaning anything.
            print("\n  The setup window closed — press Enter.")

        poller = enrollment.EnrollmentPoller(
            token,
            window,
            on_candidate=_announce,
            on_flood=_flood,
            on_close=_closed,
            offset=offset,
            client_factory=_open_client,
        )

        try:
            poller.start()
            print()
            print(f"  Open Telegram, search for {handle} — the bot you just")
            print("  created — and press START.")
            if identity.username:
                print(
                    "  Already have Telegram on this machine? "
                    + window.deep_link(identity.username)
                )
            print()
            print("  The bot will reply with a setup code. Type it below.")
            print("  (Or type 'id' to paste a user ID instead.)")

            outcome = _await_code(window, poller)
        finally:
            poller.stop()

        if outcome is not None:
            return outcome
        # The window closed and the operator asked for a fresh one.


def _await_code(
    window: enrollment.EnrollmentWindow, poller: enrollment.EnrollmentPoller
) -> _Enrolled | None:
    """Read codes until one enrolls somebody.

    Every rule about who may be enrolled is asked of ``window`` directly; the
    poller is only asked for what running a thread produced — an error, and the
    offset consumed so far.

    Returns ``None`` when the window closed and the operator wants another —
    the caller owns opening it, so that the old poll is always stopped before a
    new one starts and the two can never collide on the token.
    """
    while True:
        if poller.error is not None:
            print(f"\n  The setup poll stopped: {poller.error}")
            return _Enrolled(_prompt_manual_user_id(), None, None, poller.offset)

        if window.closed:
            print("\n  The setup window closed. Every code it issued is now dead.")
            if _prompt_yes_no("  Start a new one?"):
                return None
            return _Enrolled(_prompt_manual_user_id(), None, None, poller.offset)

        entered = _prompt("Setup code", default="").strip()
        if entered.lower() == "id":
            return _Enrolled(_prompt_manual_user_id(), None, None, poller.offset)

        candidate = window.submit(entered) if entered else window.authenticated_candidate()

        if candidate is None:
            if not entered or window.closed:
                continue
            left = enrollment.MAX_WRONG_CODES - window.wrong_attempts
            if left > 0:
                print(f"  That code doesn't match. {left} attempt(s) left.")
            continue

        if not _confirm_candidate(candidate):
            # The code is spent either way, so a decline reopens the window for
            # a different person rather than for a retype of the same digits.
            window.take(candidate)
            print("  Nothing was written. Have the right account message the bot.")
            continue

        # Re-checked after the confirmation, not only before it: a prompt left
        # answered in the morning must not enroll on last night's window.
        if window.closed:
            print("  The window closed while that sat unanswered. Nothing was written.")
            continue

        poller.stop()
        window.take(candidate)
        return _Enrolled(
            candidate.user_id, candidate.chat_id, candidate.thread_id, poller.offset
        )


def _close_out(token: str, enrolled: _Enrolled) -> None:
    """Hand the poll back to the core cleanly, and say one last thing.

    Confirming the offset matters: without it the core's first ``getUpdates``
    replays everything the wizard consumed.  The message is deliberately the
    wizard's last word in Telegram — orientation belongs on first boot, when
    the bot can actually act on it.
    """

    async def _finish(client: httpx.AsyncClient) -> None:
        if enrolled.chat_id is not None:
            try:
                await enrollment.send_message(
                    client,
                    token,
                    enrolled.chat_id,
                    enrollment.ALL_SET_MESSAGE,
                    enrolled.thread_id,
                )
            except enrollment.EnrollmentError:
                logger.warning("Could not send the enrollment confirmation")
        await enrollment.confirm_offset(client, token, enrolled.offset)

    try:
        _telegram(_finish)
    except enrollment.EnrollmentError:
        logger.warning("Could not hand the Telegram poll back cleanly")


def _prompt_context() -> tuple[str, dict[str, Any]]:
    """Prompt for the first context configuration.

    Returns:
        A tuple of (context_name, context_dict) ready for the config YAML.
    """
    print("\nSet up your first context.")
    print("A context is a named shortcut for a project. You'll use the name with")
    print("/context to switch between projects in Telegram.\n")

    name = _prompt("Context name (e.g. my-project)", default="default", validator=_validate_context_name)

    with _path_completion() as completes:
        hint = ", tab to autocomplete" if completes else ""
        directory = _prompt(
            f"Project directory (absolute path{hint})",
            validator=_validate_directory,
        )
    description = _prompt("Short description", default="Default context")

    # Model selection
    print("\nSelect a model:")
    for i, (model_name, model_desc) in enumerate(_MODELS, 1):
        display = model_name or "CLI default"
        print(f"  {i}. {display} ({model_desc})")
    print(f"  {len(_MODELS) + 1}. Enter a custom model name")

    def _validate_model_choice(value: str) -> str | None:
        try:
            choice = int(value)
        except ValueError:
            return f"Enter a number between 1 and {len(_MODELS) + 1}."
        if choice < 1 or choice > len(_MODELS) + 1:
            return f"Enter a number between 1 and {len(_MODELS) + 1}."
        return None

    choice = int(_prompt("Choice", default="1", validator=_validate_model_choice))
    if choice <= len(_MODELS):
        model = _MODELS[choice - 1][0]
    else:
        model = _prompt("Custom model name")

    return name, build_context_dict(directory, description, model)


def build_context_dict(
    directory: str,
    description: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Assemble one context entry.

    Shared by every front end that can create a first config, so the shape
    cannot drift between them.
    """
    context: dict[str, Any] = {
        "directory": str(Path(directory).expanduser().resolve()),
        "description": description,
        "allowed_tools": ["LSP", "AskUserQuestion"],
    }
    if model is not None:
        context["model"] = model
    return context


def build_config_dict(
    token: str,
    user_id: int,
    context_name: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the full config dictionary for YAML serialization."""
    return {
        "telegram": {"token": token},
        "allowed_users": [user_id],
        "contexts": {context_name: context},
        "default_context": context_name,
        "review": {
            "port": random.randint(49152, 65535),
            "tunnel": "cloudflared",
        },
    }


def run_setup_wizard(config_path: Path) -> None:
    """Run the interactive setup wizard.

    Prompts the user for essential configuration and writes the config file.

    Args:
        config_path: Destination path for the config YAML file.

    Raises:
        SystemExit: If the user cancels the wizard.
    """
    print()
    print("Welcome to OpenShrimp!")
    print("No config file found — let's set one up.")
    print("Press Ctrl-C at any time to cancel.")
    print()

    token, identity = _prompt_token()
    enrolled = _prompt_operator(token, identity)
    context_name, context = _prompt_context()

    config_dict = build_config_dict(token, enrolled.user_id, context_name, context)

    from open_shrimp.config import write_config

    try:
        write_config(config_path, config_dict)
    except OSError as e:
        print(f"\nFailed to write config file: {e}", file=sys.stderr)
        raise SystemExit(1)

    # After the config exists, not before: telling somebody they are set up is
    # only true once the file that says so is on disk.
    _close_out(token, enrolled)

    print(f"\nConfig written to {config_path}")
    print("You can edit it later to add more contexts, tools, or users.")
    print("Starting OpenShrimp...\n")
