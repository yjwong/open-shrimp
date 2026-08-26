"""Interactive setup wizard for first-time OpenShrimp configuration."""

from __future__ import annotations

import asyncio
import glob as globmod
import logging
import os
import random
import sys
import textwrap
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

from open_shrimp.backend.factory import default_model_label, provider_id_for_model
from open_shrimp.backend.claude_sdk.projects import name_directory
from open_shrimp.backend.setup_models import (
    connect_copy,
    menu_provider,
    setup_models,
)
from open_shrimp.config import (
    RESERVED_CONTEXT_NAME,
    _validate_context_name,
    build_context_dict,
    check_directory,
)


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


# The menu, with the entry that pins nothing at its head — that one names
# whichever agent's default it defers to rather than a model, so it is not one
# of the rows the shared catalog carries.  "Custom model name" is the entry one
# past the end of this tuple.
_MODELS: tuple[tuple[str | None, str | None, str], ...] = (
    (None, None, "pin nothing, and let Claude Code decide — recommended"),
    *((m.alias, menu_provider(m.alias), m.description) for m in setup_models()),
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
    """Validate a directory path exists, as every other surface does."""
    checked = check_directory(value)
    if not checked["exists"]:
        return f"Directory does not exist: {checked['path']}"
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


def _prompt_choice(label: str, count: int, *, default: str | None = None) -> int:
    """Ask for one of *count* numbered options, and return the number typed.

    Every numbered list in the wizard is bounded the same way, so the bound and
    the sentence that states it are written once: a list whose refusal names a
    range it does not enforce is the way an off-by-one reaches a user.
    """

    def _check(value: str) -> str | None:
        problem = f"Enter a number between 1 and {count}."
        try:
            choice = int(value)
        except ValueError:
            return problem
        return None if 1 <= choice <= count else problem

    return int(_prompt(label, default=default, validator=_check))


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
            print(f"  Open Telegram, search for {handle} (the bot you just")
            print("  created) and press START.")
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
            print("  Nothing was written. Message the bot again to get a new code.")
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


@dataclass
class _Project:
    """One row of the import list.

    ``name`` is what the context will be called and ``label`` is the folder
    it came from.  They differ whenever a folder name is not a legal context
    name, which is often enough that the two cannot be one field.
    """

    name: str
    directory: str
    label: str
    chosen: bool


def _show_projects(projects: list[_Project]) -> None:
    """Draw the tick list, numbered from one."""
    print()
    for index, project in enumerate(projects, 1):
        tick = "x" if project.chosen else " "
        print(f"  {index:>2}. [{tick}] {project.name:<24} {project.directory}")
    if not projects:
        print("  (nothing yet)")


def _toggle(projects: list[_Project], answer: str) -> None:
    """Flip whatever the user just numbered; say so when they numbered nothing."""
    picked = 0
    for word in answer.replace(",", " ").split():
        try:
            index = int(word)
        except ValueError:
            print(f"  I don't know what '{word}' means.")
            continue
        if not 1 <= index <= len(projects):
            print(f"  There's no project {index}.")
            continue
        projects[index - 1].chosen = not projects[index - 1].chosen
        picked += 1
    if picked == 0 and not projects:
        print("  There's nothing to tick yet — press 'a' to add a folder.")


def _prompt_manual_project(taken: set[str]) -> _Project:
    """Ask for one folder by path, for a machine with nothing to import."""
    with _path_completion() as completes:
        hint = ", tab to autocomplete" if completes else ""
        directory = _prompt(
            f"Folder (absolute path{hint})",
            validator=_validate_directory,
        )
    # Named by the same code that names a discovered folder, so a folder typed
    # here and the same folder found by discovery cannot come out differently.
    named = name_directory(directory, taken)

    def _check(value: str) -> str | None:
        error = _validate_context_name(value)
        if error:
            return error
        if value in taken:
            return f"Another project is already called '{value}'."
        return None

    # Suggested rather than demanded: the folder name is what the user thinks
    # of this project as, and a name it cannot legally take is not a reason to
    # make them invent one.
    name = _prompt(
        "Call it (this is the name you'll type after /context)",
        default=named.context_name,
        validator=_check,
    )
    return _Project(name, named.directory, named.name, True)


def _prompt_sandbox() -> str | None:
    """Ask, once for the whole import, whether to keep projects off this computer.

    One yes-or-no about isolation, never a choice of hypervisor: which backend
    provides it is ``doctor.blessed_backend``'s answer, and a user who needs
    the difference explained is not the user who should be picking.

    A host that cannot start the blessed backend is told so with the remedy
    and asked nothing, because the only honest answer to a question whose yes
    cannot be honoured is not to ask it.
    """
    from open_shrimp.doctor import blessed_offer, sandbox_note
    from open_shrimp.paths import init_paths

    # The wizard writes no instance name, so the unscoped install is the one
    # these prerequisites belong to.
    init_paths(None)
    print()
    print("Now, the sandbox.")
    print("  Checking what this machine can run…")
    offer = blessed_offer(None)

    print()
    print(textwrap.fill(sandbox_note(offer), width=68))

    # Not asked where the yes cannot be honoured.  The note above has already
    # said why, which is the whole reason it is printed before this returns
    # rather than instead of a question nobody was going to be asked.
    if offer is None or not offer.available:
        # Only where there is something to come back to.  A platform with no
        # backend at all has nothing to turn on.
        if offer is None:
            return None
        if offer.remedies:
            # Nothing to install: what is missing is administrator rights, and
            # this wizard has no way to ask for them.  The tray does.
            print("Setting that up needs administrator rights, which this")
            print("wizard cannot ask for. Run the OpenShrimp app on this")
            print("computer and it will offer to.")
        else:
            print("Install that, then turn the sandbox on from /context")
            print("in Telegram.")
        return None

    # What "off" costs is in the note, so it is not said twice here: the three
    # wizards share one sentence precisely so the terminal cannot make a
    # promise the GUIs do not.
    print()
    return offer.backend if _prompt_yes_no("Enable sandbox") else None


def _offer_credentials(model: str | None) -> None:
    """Offer whatever credential the picked model needs, where it is missing.

    Which one that is follows from the model and nothing else: a Claude alias,
    or nothing pinned, is the Claude Code sign-in, and a provider-qualified
    name is a login with that provider.  Nobody is asked both.
    """
    provider_id = provider_id_for_model(model)
    if provider_id is None:
        _offer_sign_in()
    else:
        _offer_provider_login(provider_id)


def _offer_provider_login(provider_id: str) -> None:
    """Offer to connect *provider_id* to opencode, where it is not already.

    A provider already in the credential file is asked nothing and told
    nothing, matching what the Claude branch does for a host already signed in.

    Nothing here is fatal.  A download that failed, a login declined, or a CLI
    that would not start all leave the config written and the wizard finishing
    normally: the user finds out on their first message, and the login is one
    command away by then.
    """
    from open_shrimp.backend.opencode.auth import connected_providers
    from open_shrimp.backend.opencode.login import relogin_hint, run_provider_login
    from open_shrimp.backend.opencode.models import provider_label

    if provider_id in connected_providers():
        return

    # The intro is said in this wizard's own voice rather than read off
    # ``connect_copy``: that copy is what the two GUIs render, and they speak
    # about OpenShrimp while everything printed here speaks as it.  The outcome
    # lines are the shared ones, because there is nothing to say differently
    # about a login that worked.
    copy = connect_copy(provider_id)
    name = provider_label(provider_id)

    print()
    print(f"Now, connecting {name}.")
    print(f"I answer your messages by running that model through OpenCode on")
    print(f"this computer, under your own {name} account. OpenCode asks for")
    print(f"whatever {name} accepts — an API key, or a sign-in in your browser")
    print("— and keeps it; I never see it.")
    print()
    if not _prompt_yes_no(f"Connect {name} now?"):
        print(f"  Skipped. Run this when you want it: {relogin_hint(provider_id)}")
        return

    print()
    try:
        connected = run_provider_login(provider_id)
    except Exception as e:
        logger.warning("Could not start the %s login", provider_id, exc_info=True)
        print(f"\n  I couldn't start the login: {e}")
        connected = False

    print()
    if connected:
        print(f"  {copy.done}")
    else:
        print("  Not connected yet. Run this to try again:")
        print(f"  {relogin_hint(provider_id)}")


def _offer_sign_in() -> None:
    """Offer to sign Claude in, where it is not signed in already.

    A host that is signed in already is asked nothing and told nothing.

    Run inline rather than in a window of its own: this wizard already owns a
    terminal, and the CLI wants one.  No branch here is fatal — the readiness
    card asks again on a machine that needs it, and stays quiet on one whose
    contexts all run on another backend, so the copy promises no reminder it
    cannot keep.
    """
    from open_shrimp.backend.claude_sdk.login import auth_status, run_interactive_login

    try:
        if auth_status().signed_in:
            return
    except Exception:
        # A probe that cannot answer must not cost a config that is still
        # unwritten, so an unreadable credential store offers the step rather
        # than skipping it.  Offering costs a question the user can decline.
        logger.warning("Could not read the Claude sign-in state", exc_info=True)

    print()
    print("Now, signing in.")
    print("I answer your messages by running Claude Code on this computer,")
    print("under your own Claude Code login. Signing in happens in your")
    print("browser; you'll choose there between a Claude subscription and")
    print("API billing.")
    print("Say no only if you'd rather set ANTHROPIC_API_KEY in my")
    print("environment, which signs me in just as well.")
    print()
    if not _prompt_yes_no("Sign in now?"):
        print("  Skipped. Send /login in Telegram if you want it later.")
        return

    print()
    try:
        signed_in = run_interactive_login()
    except Exception as e:
        logger.warning("Could not start the Claude sign-in", exc_info=True)
        print(f"\n  I couldn't start the sign-in: {e}")
        signed_in = False

    print()
    if signed_in:
        print("  Signed in.")
    else:
        print("  Not signed in yet. Send /login in Telegram to try again.")


def _prompt_model() -> str | None:
    """Ask which model the imported projects run on, once for all of them.

    Every row names its lab, because "gpt-5.6-sol" without "OpenAI" does not
    tell somebody which account they are about to be asked to log into.  No row
    names a backend: which one serves the turn follows from the model, and
    nobody choosing a model is choosing it.
    """
    print()
    print("Which model should they use?")
    for i, (model_name, provider, model_desc) in enumerate(_MODELS, 1):
        display = model_name or default_model_label()
        lab = f" — {provider}" if provider else ""
        print(f"  {i}. {display}{lab} ({model_desc})")
    print(f"  {len(_MODELS) + 1}. Enter a custom model name")

    choice = _prompt_choice("Model", len(_MODELS) + 1, default="1")
    if choice <= len(_MODELS):
        return _MODELS[choice - 1][0]
    return _prompt("Custom model name")


def _prompt_projects() -> tuple[list[_Project], str | None]:
    """Import the projects the user already works in, and the model they run on.

    Discovery runs before anything is drawn, because what this step asks
    depends on what it found: a machine with candidates is shown a list to
    prune, and a machine with none is shown a path prompt.  Finishing with
    nothing is a supported outcome, not a failed step — the user reaches the
    OpenShrimp context and adds projects by chat.

    The chosen projects come back unbuilt rather than as config entries: the
    sandbox belongs to the last step, and a context that has been written
    down already cannot be given one there.
    """
    from open_shrimp.backend.claude_sdk.projects import discover_claude_projects

    print()
    print("Now your projects.")
    print("A project is a folder I work in. In Telegram you switch between")
    print("them with /context.")
    print()
    print("  Looking for projects you've already opened in Claude Code…")

    projects = [
        _Project(found.context_name, found.directory, found.name, True)
        for found in discover_claude_projects()
    ]
    if projects:
        print(f"  Found {len(projects)}. Everything is ticked — untick what you")
        print("  don't want.")
    else:
        print("  None found on this machine.")

    while True:
        _show_projects(projects)
        print()
        print("  Enter — continue      a — add a folder by path")
        print("  a number — tick or untick it")
        print("  s — skip, and add projects later by chatting with me")
        answer = _prompt("Projects", default="").strip().lower()

        if answer == "s":
            for project in projects:
                project.chosen = False
            break
        if answer == "a":
            projects.append(_prompt_manual_project({p.name for p in projects}))
            continue
        if not answer:
            break
        _toggle(projects, answer)

    chosen = [project for project in projects if project.chosen]
    if not chosen:
        print()
        print("  No projects, then. Open /context in Telegram, pick OpenShrimp,")
        print("  and tell it which folder to add whenever you're ready.")
        return [], None

    return chosen, _prompt_model()


def build_config_dict(
    token: str,
    user_id: int,
    contexts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the full config dictionary for YAML serialization.

    Takes however many contexts setup gathered, including none: a user with
    nothing to import reaches the end of setup with the OpenShrimp context
    and adds projects by chat afterwards.

    No ``default_context``.  Setup cannot know which of several imported
    projects a topic should mean, and guessing binds every unbound scope to
    that guess on its first message.  Absent, an unbound scope is shown the
    picker instead.
    """
    return {
        "telegram": {"token": token},
        "allowed_users": [user_id],
        "contexts": contexts,
        "review": {
            "port": random.randint(49152, 65535),
            "tunnel": "cloudflared",
        },
    }


def _offer_autostart(config_path: Path) -> None:
    """Offer to keep the bot running once this window is gone.

    Asked only where the answer is the core's to give: a supervisor above it
    already owns restarting it, and an autostart already registered has
    nothing to add — so both are skipped without a prompt rather than asked
    and ignored.

    Registered for the next login, never started: the wizard hands straight
    over to a core that already owns this login's Telegram poll.
    """
    from open_shrimp import readiness, service

    if os.environ.get(readiness.SUPERVISED_ENV):
        return
    # The wizard writes no instance name, so the unscoped install is the one
    # this config would be served by.
    if service.is_service_installed(None):
        return

    print()
    if not _prompt_yes_no("Keep OpenShrimp running after you close this window?"):
        print(f"  {readiness.NO_AUTOSTART}")
        return

    print()
    try:
        service.install_service(str(config_path), start=False)
    except (Exception, SystemExit):
        # The config is written and the core is about to boot, so an autostart
        # that could not be registered is a downgrade and not a setup failure.
        # SystemExit as well as Exception because refusing to install is
        # spelled sys.exit, and that would take the config's news down with it.
        logger.warning("Could not register autostart", exc_info=True)
        print("\n  I could not set that up.")
        print(f"  {readiness.NO_AUTOSTART}")


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
    chosen, model = _prompt_projects()

    # What is left is asked only where it has something to settle: nothing
    # imported means nothing to sandbox, a host already signed in is asked
    # nothing about signing in, and the autostart question skips itself where
    # a supervisor already owns restarting the core.
    sandbox = _prompt_sandbox() if chosen else None
    contexts = {
        project.name: build_context_dict(
            project.directory, project.label, model, sandbox
        )
        for project in chosen
    }

    # Before the config is written, so a wizard that runs to its last line
    # leaves nothing for the first readiness card to report.
    _offer_credentials(model)

    config_dict = build_config_dict(token, enrolled.user_id, contexts)

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
    print("To add another project later, open /context in Telegram and pick")
    print("OpenShrimp — it edits this file for you.")

    # Installing demands a config the core can start from, so the offer comes
    # after the file it would register against exists.
    _offer_autostart(config_path)

    print("\nStarting OpenShrimp...\n")
