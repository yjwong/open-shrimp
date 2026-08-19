"""The reserved ``openshrimp`` context — the supervisor.

The supervisor answers questions about OpenShrimp itself and reports the
running configuration.  It is built here, in code, and never read from or
written to ``contexts:``; ``config.py`` and ``setup.py`` both refuse the
name so a user's project can never occupy it.

Two invariants hold this module together.

**The supervisor is reachable only by a user picking it.**  It is not a
``default_context`` candidate and is deliberately absent from
``config.contexts``, so every site that enumerates configured contexts —
the event pick-up keyboard, ``create_schedule``, ``ask_context``, the
review/VNC/port-relay Mini App APIs, the config Mini App — cannot see it.
Reaching the supervisor is opt-in at the two places that offer a picker
and resolve a binding, which means a site that forgets about it fails
closed.  ``handlers.messages.dispatch_from_registry`` closes the one
remaining route, ``dispatch_registry``, by raising
:class:`SupervisorDispatchRefused`.

**The supervisor has no filesystem and no shell.**  Leaving those tools
out of ``allowed_tools`` would not be enough: read-only file tools are
auto-approved inside the context directory and ``Edit``/``Write`` merely
prompt.  They are listed in ``disallowed_tools`` instead, which removes
them from the agent entirely — ``--disallowedTools`` on the Claude SDK
backend, last-position deny rules on OpenCode.  ``directory`` is
therefore only a label; no tool can read it.
"""

from __future__ import annotations

from pathlib import Path

from open_shrimp.config import (
    DEFAULT_CONFIG_PATH,
    RESERVED_CONTEXT_NAME,
    Config,
    ContextConfig,
)

# Pinned in code and repeated in the system prompt so the model never
# chooses the host it reads documentation from.
DOCS_BASE_URL = "https://shrimp.wong.place"

# The index of every documentation page, published by the Starlight site.
DOCS_INDEX_PATH = "/llms.txt"

# Removed from the agent outright.  An unsandboxed context with file tools
# and a directory anywhere near ``$HOME`` would be the strongest context on
# the machine, and ``config.yaml``'s comments only survive a write through
# ``write_raw_yaml``, never through ``Edit``.  ``Task`` is here because a
# subagent is a second way to reach the same tools.
SUPERVISOR_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "Task",
)

# ``WebFetch`` is the general reader for the documentation site; the
# supervisor's own ``read_docs`` tool covers the pinned host.  Both
# backends resolve the capitalised name (OpenCode lowercases it to
# ``webfetch``), so one entry serves both.
SUPERVISOR_ALLOWED_TOOLS: tuple[str, ...] = ("WebFetch",)

# The supervisor's read-only MCP tools.  Named here rather than in
# ``supervisor_tools`` because ``client_manager`` has to auto-approve them
# and must not import the tool module to learn what they are called; the
# builder asserts it produces exactly these.
#
# The write tools are deliberately not in this list.  Auto-approval is
# spelled by adding a tool to the backend's ``allowed_tools``, which is
# also what stops it ever reaching ``can_use_tool`` — so absence here is
# not a preference about how the write tools are handled, it is the
# mechanism by which they are handled at all.
SUPERVISOR_TOOL_NAMES: tuple[str, ...] = (
    "read_docs",
    "list_contexts",
    "validate_directory",
)

# The two tools that can change ``config.yaml``.  A context is a
# directory plus a tool policy, so writing one is a privilege escalation:
# a context with a broad directory and a shell in ``allowed_tools`` is a
# host shell that never passed a ``host_bash`` approval.  Every call
# therefore renders a diff and asks, in the same early slot in
# ``hooks.py`` that ``is_host_escape`` occupies, ahead of every rule that
# could otherwise answer for the user.
SUPERVISOR_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "write_context",
    "remove_context",
)

_DESCRIPTION = "OpenShrimp itself — docs and configuration"


class SupervisorDispatchRefused(RuntimeError):
    """A non-user source tried to start a turn in the supervisor scope.

    Schedules, ``host_monitor`` and event pick-up all deliver through
    ``dispatch_registry``, and event pick-up carries text OpenShrimp did
    not write.  Only a user message may start a supervisor turn.
    """


def is_supervisor_context(name: str | None) -> bool:
    """Whether *name* is the reserved supervisor context."""
    return name == RESERVED_CONTEXT_NAME


def _supervisor_directory() -> str:
    """The directory the supervisor's session runs in.

    The config directory, which is a label only — no file tool survives
    :data:`SUPERVISOR_DISALLOWED_TOOLS` to read it.  It still has to
    *exist*, because the agent CLI refuses to start in a working
    directory that does not, and an install driven entirely by
    ``--config`` elsewhere may never have created the default one.  The
    home directory is the fallback for exactly that case.
    """
    config_dir = DEFAULT_CONFIG_PATH.parent
    if config_dir.is_dir():
        return str(config_dir)
    return str(Path.home())


def supervisor_context() -> ContextConfig:
    """Build the supervisor's context config.

    ``model``, ``effort`` and ``backend`` stay unset so the supervisor
    follows the install's defaults.
    """
    return ContextConfig(
        directory=_supervisor_directory(),
        description=_DESCRIPTION,
        allowed_tools=list(SUPERVISOR_ALLOWED_TOOLS),
        disallowed_tools=list(SUPERVISOR_DISALLOWED_TOOLS),
    )


def selectable_contexts(config: Config) -> dict[str, ContextConfig]:
    """The contexts a user may pick, the supervisor last.

    The only enumeration that includes the supervisor.  Everything that
    acts on a context without a user choosing it reads
    ``config.contexts`` instead and therefore cannot reach it.
    """
    contexts = dict(config.contexts)
    contexts[RESERVED_CONTEXT_NAME] = supervisor_context()
    return contexts


def resolve_context(config: Config, name: str | None) -> ContextConfig | None:
    """The context *name* refers to, or ``None`` if it names nothing."""
    if name is None:
        return None
    if is_supervisor_context(name):
        return supervisor_context()
    return config.contexts.get(name)


def system_prompt() -> str:
    """The block appended to the supervisor's system prompt.

    Pins the documentation host: the model is told where the docs are
    rather than asked to work it out.
    """
    return (
        "You are the OpenShrimp supervisor context. You answer questions "
        "about OpenShrimp — how it works, how it is configured, what a "
        "setting does — and report the running configuration.\n\n"
        f"The documentation is at {DOCS_BASE_URL}. That host is the only "
        "source of truth for how OpenShrimp behaves; do not answer from "
        "memory and do not read documentation from anywhere else.\n\n"
        f"Use the read_docs tool. Start with the page index at "
        f"{DOCS_INDEX_PATH}, which lists every page, then fetch the pages "
        "you need. /llms-full.txt is the whole site in one response — "
        "reach for it only when a question genuinely spans the "
        "documentation, because it is large.\n\n"
        "Use list_contexts to report what is configured, and "
        "validate_directory to check whether a path the user names "
        "exists.\n\n"
        "You have no shell and no file tools, by design. You cannot run "
        "commands, and the only file you can change is config.yaml, "
        "through write_context and remove_context.\n\n"
        "Both take the state_token that list_contexts returns, and both "
        "refuse if config.yaml has changed since. When one refuses, call "
        "list_contexts again and propose the change against the token it "
        "returns. Every write shows the user a diff and waits for them to "
        "approve it, so describe what you are about to do before you call "
        "the tool, and never say a change is made until the tool tells you "
        "it is.\n\n"
        "You can set a project's directory, description, model, effort and "
        "sandbox. You cannot set which tools it may run, which extra "
        "directories it may touch, or whether it can escape its sandbox — "
        "those decide what a project is allowed to do, and they are set by "
        "the user in the configuration Mini App, which they open with "
        "/config. Say so plainly when asked; do not work around it."
    )


__all__ = [
    "DOCS_BASE_URL",
    "DOCS_INDEX_PATH",
    "SUPERVISOR_TOOL_NAMES",
    "SUPERVISOR_WRITE_TOOL_NAMES",
    "SupervisorDispatchRefused",
    "is_supervisor_context",
    "resolve_context",
    "selectable_contexts",
    "supervisor_context",
    "system_prompt",
]
