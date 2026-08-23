"""Whether the Claude CLI on this host can authenticate, and how to sign it in.

The terminal wizard, the two desktop wizards that shell out to ``openshrimp
auth``, and the readiness card at every boot all ask this one question, so they
all read it from here and cannot disagree.

The three sources are not equivalent.  Either environment variable
authenticates the CLI on its own and is how a headless install is set up, so
they are read first; the credential store on disk is what ``/login`` writes.
Reporting *which* one lets a UI stay quiet about a sign-in button for an
operator who authenticates by API key and has no OAuth session to show.

Sign-in runs as a plain child process on the caller's own terminal: no pty, no
pumping, no ``BROWSER`` override.  The Mini App path in
:mod:`open_shrimp.terminal.api` overrides the browser because a Mini App
session has no desktop to open a window on; here one does, and overriding it
would replace an opening window with a URL to copy by hand.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from open_shrimp.backend.claude_sdk.binary import find_claude_binary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthStatus:
    """Whether the CLI holds credentials, and which ones.

    ``how`` is ``"api_key"``, ``"env_token"`` or ``"oauth"`` when signed in and
    ``None`` when not, so a UI can switch on it without a sentinel string.
    """

    signed_in: bool
    how: str | None


def auth_status() -> AuthStatus:
    """What the Claude CLI would authenticate with, if it ran right now.

    Environment first: a variable set in the process the CLI is spawned from
    wins over anything on disk, so answering with the file would name a
    credential that is never the one used.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AuthStatus(True, "api_key")
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return AuthStatus(True, "env_token")

    # Imported here rather than at module scope: this module is reached from
    # the CLI's argument dispatch, and the watcher pulls in sandbox plumbing
    # that a one-shot status question never touches.
    from open_shrimp.backend.claude_sdk.cred_watcher import host_signed_in

    if host_signed_in():
        return AuthStatus(True, "oauth")
    return AuthStatus(False, None)


def run_interactive_login() -> bool:
    """Hand this terminal to ``claude /login``, and report what came of it.

    Blocking on purpose, and synchronous: the caller is a wizard or a CLI
    subcommand, the child owns the tty for as long as it runs, and there is
    nothing else for this process to be doing meanwhile.

    Stdio is inherited rather than piped, so the CLI sees a real terminal and
    draws its own prompts, and nothing is added to the environment.

    Ctrl-C is the user abandoning the sign-in: the child takes the signal, and
    this returns whatever the credential store says afterwards rather than
    unwinding a wizard that has not written its config yet.

    Returns:
        Whether credentials are present once the child has exited.  The child's
        own exit code is zero even for a sign-in that was backed out of.

    Raises:
        RuntimeError: No Claude CLI could be found on this host.
    """
    binary = find_claude_binary()
    try:
        subprocess.run([binary, "/login"], check=False)
    except KeyboardInterrupt:
        logger.debug("The Claude sign-in was interrupted")
    return auth_status().signed_in


__all__ = ["AuthStatus", "auth_status", "run_interactive_login"]
