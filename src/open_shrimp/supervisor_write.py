"""Proposing, rendering and applying the supervisor's config writes.

A config write is a privilege escalation.  A context with a broad
``directory`` and an ``allowed_tools`` list that names a shell is a host
shell that never passed a ``host_bash`` approval, so the rendered diff
and the approval card in front of it are the only things standing in the
way.  This module is the half that decides *what* would be written; the
approval card is the half that asks.

Three properties hold it together.

**One write path.**  The patch, the re-validation and the round-trip
dump are :mod:`open_shrimp.config`'s, the same three the config Mini App
saves through.  A second implementation would drift, and the one that
drifted would be the one an agent drives.

**A plan is applied, never re-derived at write time.**  A plan carries
the whole proposed file and :func:`apply_plan` writes that text, so
nothing can slip in between the last check and the bytes landing.  The
approval card and the tool each build their own plan from the same
arguments and the same token, which is what makes the two identical —
the plan is not carried across the approval, because a lock the user
holds by not tapping is worse than a check that runs twice.

**A plan is bound to the file it was read from.**  Every plan carries the
state token of the config it was built against, and both the tool and the
approval card check that token against disk before writing.  The agent
holds tool results across many turns and the user can edit the same file
in the Mini App meanwhile; staleness therefore surfaces as a refusal and
never as a wrong write.

What the tool may write is deliberately narrower than what a context can
hold.  See :data:`WRITABLE_FIELDS` and :data:`WITHHELD_FIELDS`.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml.error import YAMLError

from open_shrimp.config import (
    _SANDBOX_BACKENDS,
    _VALID_EFFORT_LEVELS,
    RESERVED_CONTEXT_NAME,
    _validate_context_name,
    _validate_raw,
    build_context_dict,
    check_directory,
    dump_raw_yaml,
    load_raw_yaml,
    patch_raw_yaml,
    to_plain,
)
from open_shrimp.supervisor import SUPERVISOR_WRITE_TOOL_NAMES

# The bare names of the two write tools.  The wire names differ by
# backend, which is why the policy — not this module — maps one to the
# other.  Unpacked from the one declaration rather than restated, so the
# tool builder and the planner cannot disagree about what exists.
WRITE_CONTEXT, REMOVE_CONTEXT = SUPERVISOR_WRITE_TOOL_NAMES
CONFIG_WRITE_TOOLS: tuple[str, ...] = SUPERVISOR_WRITE_TOOL_NAMES

# What ``write_context`` may put in a context.
WRITABLE_FIELDS: tuple[str, ...] = (
    "directory",
    "description",
    "model",
    "effort",
    "sandbox",
)

# What it may not, and why one sentence each.  These are the fields that
# decide what a context is *allowed to do* rather than what it points at,
# and a diff is a weak gate for them: the difference between an approved
# `allowed_tools: ["LSP"]` and a fatal `allowed_tools: ["Bash"]` is four
# characters in a file most people do not read closely.  They stay with
# the config Mini App, which is reached by a person tapping /config.
WITHHELD_FIELDS: dict[str, str] = {
    "allowed_tools": "which tools run without asking you first",
    "disallowed_tools": "which tools the agent is denied",
    "additional_directories": "which directories beyond its own it may touch",
    "allow_host_escape": "whether it can run commands outside its sandbox",
    "default_for_chats": "which chats it claims by default",
    "locked_for_chats": "which chats are pinned to it",
    "mcp": "which MCP servers it loads",
    "container": "its sandbox",
    "backend": "which agent backend it runs on",
}

# Long enough for a whole context, short enough for one Telegram message
# once the card's own text and MarkdownV2 escaping are accounted for.
_MAX_DIFF_CHARS = 2800

# A diff renders three lines of unchanged context around each change, so a
# small config can carry the bot token into a chat that does not need a
# second copy of it.
_SECRET_LINE = re.compile(
    r"^([-+ ]?\s*(?:\S*(?:token|secret|password|api_key|_json)\s*):\s*)\S.*$",
    re.IGNORECASE,
)


class ConfigWriteRefused(Exception):
    """Why the write did not happen, in words meant for the agent.

    Every refusal says what to do next, because the agent's only other
    option is to guess or to tell the user it succeeded.
    """


@dataclass(frozen=True)
class ConfigWritePlan:
    """A proposed edit to ``config.yaml``, ready to show and to apply."""

    #: One line naming the change, for the top of the approval card.
    headline: str
    #: Unified diff of the whole file, secrets redacted, truncated.
    diff: str
    #: The complete proposed file.  Applied verbatim.
    new_text: str
    #: The state token of the file this was built against.
    token: str


# ---------------------------------------------------------------------------
# The state token
# ---------------------------------------------------------------------------


def state_token(config_path: Path) -> str:
    """A short opaque handle for "the config as it is right now".

    The modification time and a digest of the file's bytes.  The two are
    compared as one string, so both must match, and any edit at all moves
    the time — which leaves the digest to answer only the case the time
    cannot: a write that preserved mtime, on a filesystem with coarse
    timestamps or after a restore.  Digesting the whole file rather than
    the ``contexts`` block alone is therefore free and strictly stricter,
    catching a moved ``default_context`` as well, and it costs no parse.
    """
    return _token_for(config_path.stat(), config_path.read_bytes())


def _token_for(stat: object, data: bytes) -> str:
    mtime_ns = getattr(stat, "st_mtime_ns", 0)
    return f"{mtime_ns}-{hashlib.sha256(data).hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _redact(line: str) -> str:
    return _SECRET_LINE.sub(r"\1…", line)


def _render_diff(old_text: str, new_text: str) -> str:
    """A unified diff of the whole file, secrets removed and bounded."""
    lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile="config.yaml",
        tofile="config.yaml (proposed)",
        lineterm="",
    )
    diff = "\n".join(_redact(line) for line in lines)
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS].rstrip() + "\n… (diff truncated)"
    return diff


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _refuse_withheld(args: dict[str, Any]) -> None:
    """Refuse loudly rather than dropping a field the caller asked for.

    Ignoring it silently would let the agent report a change it did not
    make, which is the failure this whole module exists to prevent.
    """
    named = [field for field in WITHHELD_FIELDS if field in args]
    if not named:
        return
    reasons = "; ".join(f"{f} sets {WITHHELD_FIELDS[f]}" for f in named)
    raise ConfigWriteRefused(
        f"write_context cannot set {', '.join(named)}. Those fields decide "
        f"what a project is allowed to do, not what it points at — "
        f"{reasons}. They are set in the configuration Mini App, which the "
        f"user opens with /config. Tell them that; do not retry."
    )


def _checked_name(args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ConfigWriteRefused("name is required.")
    if name == RESERVED_CONTEXT_NAME:
        raise ConfigWriteRefused(
            f"'{RESERVED_CONTEXT_NAME}' is this context. It is built in code, "
            f"never stored in config.yaml, and cannot be created, changed or "
            f"removed from here."
        )
    problem = _validate_context_name(name)
    if problem is not None:
        raise ConfigWriteRefused(f"'{name}' is not a usable name. {problem}")
    return name


def _checked_directory(value: Any) -> str:
    result = check_directory(str(value))
    if not result["exists"]:
        raise ConfigWriteRefused(
            f"{result['path']} does not exist, or is not a directory. Check "
            f"the path with validate_directory and ask the user for the right "
            f"one — a project pointing at nothing cannot start a session."
        )
    return str(result["path"])


def _checked_sandbox(sandbox: Any, current: Any) -> str:
    """The ``sandbox:`` mapping to write, or refuse.

    Whether *current* already has one is read off *current* itself, so
    there is no second argument that could disagree with it.

    The supervisor may give a project a sandbox and may never edit or
    remove one.  A sandbox block carries more than its backend —
    ``allow_host_escape``, ``computer_use``, a ``dockerfile`` — and this
    tool writes the whole mapping, so editing an existing one would drop
    settings the user chose without ever naming them.  Adding one where
    there is none can only narrow what the context can reach.
    """
    backend = str(sandbox).strip()
    if backend not in _SANDBOX_BACKENDS:
        raise ConfigWriteRefused(
            f"'{backend}' is not a sandbox backend. Pick one of "
            f"{sorted(_SANDBOX_BACKENDS)}."
        )
    if hasattr(current, "items") and (
        hasattr(current.get("sandbox"), "items")
        or hasattr(current.get("container"), "items")
    ):
        raise ConfigWriteRefused(
            "That project already has a sandbox. Changing or removing one is "
            "done in the configuration Mini App (/config) — a sandbox block "
            "holds settings this tool does not carry, and rewriting it here "
            "would drop them."
        )
    return backend


def _context_fields(
    name: str, args: dict[str, Any], current: Any, creating: bool
) -> dict[str, Any]:
    """The keys ``write_context`` will merge into the context *name*.

    Only the keys the caller supplied, so an update leaves everything
    else on disk — including the keys this tool may not set — exactly as
    it found them.  *creating* is decided by the caller and passed in, so
    the headline the user approves and the path the code takes can never
    describe different operations.
    """
    fields: dict[str, Any] = {}
    if args.get("directory") is not None:
        fields["directory"] = _checked_directory(args["directory"])
    if args.get("description") is not None:
        fields["description"] = str(args["description"]).strip()
    if args.get("model"):
        fields["model"] = str(args["model"]).strip()
    if args.get("effort"):
        effort = str(args["effort"]).strip()
        if effort not in _VALID_EFFORT_LEVELS:
            raise ConfigWriteRefused(
                f"'{effort}' is not an effort level. Pick one of "
                f"{list(_VALID_EFFORT_LEVELS)}."
            )
        fields["effort"] = effort
    if args.get("sandbox"):
        fields["sandbox"] = {"backend": _checked_sandbox(args["sandbox"], current)}

    if not creating:
        if not fields:
            raise ConfigWriteRefused(
                f"Nothing to change on '{name}'. Pass at least one of "
                f"{list(WRITABLE_FIELDS)}."
            )
        return fields

    if "directory" not in fields or "description" not in fields:
        raise ConfigWriteRefused(
            f"There is no project called '{name}' yet, so this would create "
            f"one, and a new project needs both a directory and a "
            f"description."
        )
    # The same builder the setup wizard uses, so a project added by chat
    # and one added by the wizard are the same shape — including the
    # ``allowed_tools`` default, which this tool may not otherwise set.
    created = build_context_dict(
        fields.pop("directory"), fields.pop("description"),
    )
    created.update(fields)
    return created


def _stage_write(
    name: str, args: dict[str, Any], existing: Any, incoming: dict[str, Any],
) -> str:
    """Put the proposed context into *incoming*; name what that does."""
    _refuse_withheld(args)
    current = existing.get(name)
    creating = not hasattr(current, "items")
    incoming[name] = _context_fields(name, args, current, creating)
    verb = "Add" if creating else "Change"
    return f"{verb} the project '{name}'"


def _stage_removal(
    name: str, raw: Any, existing: Any, incoming: dict[str, Any],
) -> str:
    """Take the context out of *incoming*; name what that does.

    A ``default_context`` naming the removed project fails validation and
    the file is valid without one, so it goes with it — and says so on the
    card, because losing the default is not what the user asked for.
    """
    if name not in existing:
        raise ConfigWriteRefused(
            f"There is no project called '{name}'. Call list_contexts to "
            f"see what is configured."
        )
    del incoming[name]
    if raw.get("default_context") == name:
        del raw["default_context"]
        return f"Remove the project '{name}', which is the default project"
    return f"Remove the project '{name}'"


def plan_config_write(
    config_path: Path,
    tool: str,
    args: dict[str, Any],
    *,
    enforce_token: bool = True,
) -> ConfigWritePlan:
    """Work out what *tool* would do to *config_path*, without writing it.

    *enforce_token* is lowered only to re-render a diff after the file
    moved under a waiting approval card: the user is owed a sight of what
    the request would now do, and that render must not be blocked by the
    very staleness it is reporting.  Nothing is ever written on that path.
    """
    if tool not in CONFIG_WRITE_TOOLS:
        raise ConfigWriteRefused(f"Unknown config write tool: {tool}")
    if not config_path.exists():
        raise ConfigWriteRefused(
            f"There is no config file at {config_path}, so there is nothing "
            f"to write to."
        )

    try:
        stat = config_path.stat()
        old_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigWriteRefused(
            f"config.yaml cannot be read, so it cannot be changed from here: "
            f"{exc}."
        ) from exc
    token = _token_for(stat, old_text.encode("utf-8"))

    if enforce_token and str(args.get("state_token") or "").strip() != token:
        raise ConfigWriteRefused(
            "config.yaml has changed since you read it, so nothing was "
            "written. Call list_contexts again, check the change you have in "
            "mind is still the right one, and propose it with the state_token "
            "that call returns."
        )

    name = _checked_name(args)
    try:
        raw = load_raw_yaml(config_path)
    except (OSError, YAMLError) as exc:
        raise ConfigWriteRefused(
            f"config.yaml cannot be parsed, so it cannot be changed from "
            f"here: {exc}. Tell the user to fix the file — the running "
            f"configuration is whatever loaded last."
        ) from exc
    existing = raw.get("contexts")
    if not hasattr(existing, "items"):
        existing = {}

    # Every configured name is carried through with no fields of its own:
    # ``patch_contexts`` deletes what the payload omits and merges what it
    # names, so an empty mapping is how a context is left alone.
    incoming: dict[str, Any] = {other: {} for other in existing}
    if tool == WRITE_CONTEXT:
        headline = _stage_write(name, args, existing, incoming)
    else:
        headline = _stage_removal(name, raw, existing, incoming)

    patch_raw_yaml(raw, {"contexts": incoming})

    try:
        _validate_raw(to_plain(raw))
    except ValueError as exc:
        raise ConfigWriteRefused(
            f"That would leave config.yaml invalid, so nothing was written: "
            f"{exc}"
        ) from exc

    new_text = dump_raw_yaml(raw)
    if new_text == old_text:
        raise ConfigWriteRefused(
            f"config.yaml already says that; there is nothing to write. Tell "
            f"the user '{name}' is already configured that way."
        )

    return ConfigWritePlan(
        headline=headline,
        diff=_render_diff(old_text, new_text),
        new_text=new_text,
        token=token,
    )


def apply_plan(config_path: Path, plan: ConfigWritePlan) -> str:
    """Write *plan* and return the state token of the result.

    The token is checked once more here.  The card's own check closes the
    window while a person is deciding; this one closes the window between
    that decision and the tool call it releases.
    """
    if state_token(config_path) != plan.token:
        raise ConfigWriteRefused(
            "config.yaml changed between the approval and the write, so "
            "nothing was written. Call list_contexts again and propose the "
            "change with the state_token it returns."
        )
    config_path.write_text(plan.new_text, encoding="utf-8")
    return state_token(config_path)


__all__ = [
    "CONFIG_WRITE_TOOLS",
    "REMOVE_CONTEXT",
    "WRITABLE_FIELDS",
    "WITHHELD_FIELDS",
    "WRITE_CONTEXT",
    "ConfigWritePlan",
    "ConfigWriteRefused",
    "apply_plan",
    "plan_config_write",
    "state_token",
]
