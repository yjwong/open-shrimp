"""Connecting a provider to opencode, on whatever terminal the caller owns.

The credential goes through ``opencode auth login`` and OpenShrimp never sees
it: the CLI writes ``auth.json``, which is already what the sandbox runtime
injects into a guest and what the config Mini App lists models against.

One implementation for three wizards.  The terminal wizard calls this in
process; the two GUI wizards reach it through ``openshrimp auth login
--provider``, which they run in a console they spawn.  So the download, the
flags and the sentence a failed login leaves behind are written once.
"""

from __future__ import annotations

import logging
import subprocess

from open_shrimp.backend.opencode.auth import connected_providers
from open_shrimp.backend.opencode.release import OPENCODE_VERSION

logger = logging.getLogger(__name__)


def relogin_hint(provider_id: str) -> str:
    """The command to re-run a login by hand, with the binary named in full.

    Never a bare ``opencode``: whatever is on ``$PATH`` carries a version and
    an update policy this project does not control, so it is deliberately not
    the binary OpenShrimp runs (see :mod:`open_shrimp.backend.opencode.binary`)
    and telling somebody to type it points them at the wrong credential store.
    """
    from open_shrimp.backend.opencode.binary import managed_opencode, opencode_path

    binary = managed_opencode() or str(opencode_path())
    return f"{binary} auth login --provider {provider_id}"


def run_provider_login(provider_id: str) -> bool:
    """Hand this terminal to ``opencode auth login``, and report what came of it.

    Blocking and synchronous, like the Claude sign-in it mirrors: the caller is
    a wizard or a CLI subcommand, the child owns the tty while it runs, and
    there is nothing else for this process to do meanwhile.  Stdio is
    inherited, so the CLI draws its own prompts.

    ``--provider`` skips the CLI's provider picker.  ``--method`` would skip
    the method picker too, but which methods a provider offers differs per
    provider and that is a choice the user should see.

    Ctrl-C is somebody abandoning the login: the child takes the signal, and
    this answers from the credential file rather than unwinding a wizard that
    has not written its config yet.

    Returns:
        Whether *provider_id* is in the credential file once the child has
        exited.  The child's exit code is zero for a login backed out of.

    Raises:
        RuntimeError: opencode publishes no build for this platform, so there
            is nothing to fetch and no later moment that recovers it.
    """
    binary = _fetch_cli()
    try:
        subprocess.run([binary, "auth", "login", "--provider", provider_id], check=False)
    except KeyboardInterrupt:
        logger.debug("The %s login was interrupted", provider_id)
    return provider_id in connected_providers()


def _fetch_cli() -> str:
    """The pinned opencode, saying so while it downloads.

    Said before the wait rather than during it, and only on a host that has
    nothing cached: a machine that already has the binary says nothing, and one
    that does not is looking at a bar with no idea why.
    """
    from open_shrimp.backend.opencode.install import (
        ensure_opencode_binary,
        opencode_ready,
    )
    from open_shrimp.sandbox.prefetch import describe_bytes, throttled

    if opencode_ready():
        return ensure_opencode_binary()

    # No size in this line.  ``describe_bytes`` prints the one the server
    # declares as soon as the first chunk lands, and a figure written down here
    # is one that drifts against the release it claims to describe.
    print(f"  Fetching the OpenCode CLI ({OPENCODE_VERSION}). This takes a minute.")

    def report(done: int, total: int | None) -> None:
        # Redrawn in place: the throttle fires a few times a second, and a line
        # each would bury the login prompt that follows under a wall of them.
        # Padded, because a shrinking string leaves the tail of the last one.
        print(f"\r  {describe_bytes(done, total):<40}", end="", flush=True)

    binary = ensure_opencode_binary(progress=throttled(report))
    print()
    return binary


__all__ = ["relogin_hint", "run_provider_login"]
