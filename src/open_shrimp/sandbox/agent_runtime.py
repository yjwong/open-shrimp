"""Agent-runtime profiles: *what* agent a sandbox launches, decoupled from
*where* it runs.

A :class:`Sandbox` (``base.py``) owns the *where* — Docker, libvirt, Lima.
An :class:`AgentRuntime` owns the *what* — today only Claude, launched as a
wrapped CLI.  The two meet at :meth:`Sandbox.start_agent`, which takes an
``AgentRuntime`` and dispatches on its :attr:`AgentRuntime.launch` strategy.

This module is intentionally import-light: it must not pull in
``claude_agent_sdk`` or any backend.  It describes data and hooks; it owns no
control flow.  The control-plane ``Backend`` protocol is a separate, later
concern.

Today the only launch strategy is :class:`WrappedCLI` — the runtime tells the
sandbox "launch me by generating a wrapped-CLI script; you know how", and the
sandbox runs its existing per-backend ``build_cli_wrapper`` body.  A
``ServedEndpoint`` flavour (serve-and-reach, for OpenCode) slots in later
without touching the call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


@dataclass(frozen=True)
class WrappedCLI:
    """Launch strategy: the sandbox generates a wrapper script and returns its
    ``cli_path``.

    This is a **marker** — it carries no argv.  Each backend generates its own
    wrapper (``docker exec … claude "$@"``, ``ssh … claude``,
    ``limactl shell … claude``) via a backend-specific ``build_cli_wrapper``
    helper; that generation is correctly a ``(sandbox × agent)`` cell and stays
    per-sandbox.  The argv-passing form only becomes meaningful if a
    wrapped-CLI agent other than Claude appears (PTY+JSONL), which can extend
    this dataclass then.
    """

    kind: Literal["wrapped_cli"] = "wrapped_cli"


# A launch strategy.  Today only WrappedCLI exists; ServedEndpoint lands later.
LaunchStrategy = WrappedCLI  # | ServedEndpoint


@dataclass(frozen=True)
class HomeMount:
    """The agent-data home directory: host dir ⇄ guest dir, and whether session
    state (the ``/resume`` corpus) lives under it.

    For Claude, ``guest_dir`` is ``/home/<user>/.claude`` and
    ``holds_session_state`` is ``True`` — the resumable ``.jsonl`` files live
    under ``host_dir/projects``.
    """

    host_dir: Path
    guest_dir: str
    holds_session_state: bool


@dataclass
class AgentRuntime:
    """A data-and-hooks profile describing one agent.  Owns no control flow.

    ``inject`` writes creds+config into the home dir; ``env`` is merged into the
    guest env.  For Claude these are *declared* here so a future agent can fill
    them differently, but their live bodies stay where they execute today (the
    credential copy-in inside each backend, the ``ANTHROPIC_API_KEY`` forwarding
    inside each wrapper, and the cred-sync watcher in ``client_manager``).  This
    object names the hooks; it does not re-route the implementations.
    """

    name: str
    home_mount: HomeMount
    inject: Callable[[Path], None]
    env: dict[str, str]
    launch: LaunchStrategy


@dataclass(frozen=True)
class AgentHandle:
    """What :meth:`Sandbox.start_agent` returns.

    The :class:`WrappedCLI` flavour fills ``cli_path`` (and ``cleanup_paths``
    for the temp files to delete on session end); a ``ServedEndpoint`` flavour
    would fill an endpoint instead.
    """

    cli_path: str | None = None
    cleanup_paths: list[str] = field(default_factory=list)


def claude_runtime(home_dir: Path, *, guest_dir: str = "/home/claude/.claude") -> AgentRuntime:
    """Build the Claude :class:`AgentRuntime`.

    ``home_dir`` is the host-side agent home from
    :meth:`SandboxManager.agent_home_dir`.  For Claude the resumable session
    corpus lives under ``home_dir/projects`` (``holds_session_state=True``).

    The ``inject`` hook copies the host ``~/.claude/.credentials.json`` into the
    home dir — the same copy-in the backends perform inline today.  It is
    declared here as the agent's contribution; the backends and the cred-sync
    watcher remain the live path, so this hook is a no-op-safe convenience the
    served-endpoint work can repurpose.  ``env`` declares the
    ``ANTHROPIC_API_KEY`` forwarding contract; the wrappers do the actual
    forwarding.
    """
    import os

    def inject(target_home: Path) -> None:
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        if host_credentials.exists():
            import shutil

            target_home.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                str(host_credentials),
                str(target_home / ".credentials.json"),
            )

    env: dict[str, str] = {}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

    return AgentRuntime(
        name="claude",
        home_mount=HomeMount(
            host_dir=home_dir,
            guest_dir=guest_dir,
            holds_session_state=True,
        ),
        inject=inject,
        env=env,
        launch=WrappedCLI(),
    )
