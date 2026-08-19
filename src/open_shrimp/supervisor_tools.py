"""The supervisor's tools: read the docs, report the config, change it.

The supervisor has no shell and no file tools (see
:mod:`open_shrimp.supervisor`), so this module is the whole of what it
can do.  Three tools read and two write.

``read_docs`` builds its own URL from a path against a base pinned in
code.  The model supplies a path, never a host, and a path that resolves
off the documentation site is refused rather than fetched.

``list_contexts`` reads ``config.yaml`` rather than the ``Config`` the
session was built with, and returns a state token alongside it.  Both are
the same decision: the process hot-reloads the file, so a snapshot taken
when the session started is stale by its second turn, and a token that
did not describe the file the report came from would be worth nothing.

``write_context`` and ``remove_context`` require that token and hand the
whole planning job to :mod:`open_shrimp.supervisor_write`, which is also
what the approval card in front of them uses.  They are the only tools
here that are not auto-approved; every call renders a diff and asks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from open_shrimp.config import (
    _SANDBOX_BACKENDS,
    _VALID_EFFORT_LEVELS,
    Config,
    check_directory,
    effective_backend,
    is_sandboxed,
    load_config,
    sandbox_backend,
)
from open_shrimp.supervisor import (
    DOCS_BASE_URL,
    DOCS_INDEX_PATH,
    SUPERVISOR_TOOL_NAMES,
    SUPERVISOR_WRITE_TOOL_NAMES,
)
from open_shrimp.supervisor_write import (
    REMOVE_CONTEXT,
    WRITABLE_FIELDS,
    WRITE_CONTEXT,
    ConfigWriteRefused,
    apply_plan,
    plan_config_write,
    state_token,
)
from open_shrimp.tools import OpenShrimpTool, ToolHandler, _text_result

logger = logging.getLogger(__name__)

# Every documentation URL must start with this, after redirects.
_DOCS_ROOT = DOCS_BASE_URL.rstrip("/") + "/"

# Enough for /llms-full.txt, which is the largest page the site serves.
_MAX_DOC_CHARS = 200_000

_READ_DOCS_TIMEOUT = 30.0

_read_docs_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                f"Path on the documentation site, e.g. '{DOCS_INDEX_PATH}' "
                f"for the index of every page, '/llms/guides/contexts.md' "
                f"for one page's Markdown source, or '/llms-full.txt' for "
                f"the whole site. Paths only — the host is fixed."
            ),
        },
    },
    "required": ["path"],
}

_validate_directory_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory path to check. '~' is expanded.",
        },
    },
    "required": ["path"],
}

_STATE_TOKEN_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "The state_token from the most recent list_contexts call. The write "
        "is refused if config.yaml has changed since, so re-read it rather "
        "than remembering one from earlier in the conversation."
    ),
}

# ``additionalProperties: false`` so a field this tool may not set is a
# schema error at the model, before it can be quietly dropped and then
# reported to the user as done.
_write_context_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state_token": _STATE_TOKEN_PROPERTY,
        "name": {
            "type": "string",
            "description": (
                "The project's key in config.yaml. Letters, numbers, hyphens "
                "and underscores. An existing name updates that project; a "
                "new one creates it."
            ),
        },
        "directory": {
            "type": "string",
            "description": (
                "Absolute path to the project directory. It must already "
                "exist — check with validate_directory first."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "One short phrase naming the project, shown in the /context "
                "menu. Required when creating."
            ),
        },
        "model": {
            "type": "string",
            "description": "Model id for this project. Omit to inherit.",
        },
        "effort": {
            "type": "string",
            "enum": list(_VALID_EFFORT_LEVELS),
            "description": "Reasoning effort for this project.",
        },
        "sandbox": {
            "type": "string",
            "enum": sorted(_SANDBOX_BACKENDS),
            "description": (
                "Run this project inside a sandbox on the named backend. Can "
                "only be given to a project that has none; changing or "
                "removing a sandbox is done in the Mini App at /config."
            ),
        },
    },
    "required": ["state_token", "name"],
    "additionalProperties": False,
}

_remove_context_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state_token": _STATE_TOKEN_PROPERTY,
        "name": {
            "type": "string",
            "description": "The project to remove from config.yaml.",
        },
    },
    "required": ["state_token", "name"],
    "additionalProperties": False,
}


def _docs_url(path: str) -> str | None:
    """Resolve *path* against the pinned documentation base.

    Returns ``None`` when the result leaves the documentation site, which
    is how an absolute URL or a protocol-relative path is refused.
    """
    target = urljoin(_DOCS_ROOT, path.lstrip("/"))
    if not target.startswith(_DOCS_ROOT):
        return None
    return target


async def _read_docs(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or DOCS_INDEX_PATH).strip()
    url = _docs_url(path)
    if url is None:
        return _text_result(
            f"'{path}' is not a path on {DOCS_BASE_URL}. Pass a path such "
            f"as '{DOCS_INDEX_PATH}', not a URL.",
            is_error=True,
        )

    try:
        async with httpx.AsyncClient(
            timeout=_READ_DOCS_TIMEOUT, follow_redirects=True
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("read_docs failed for %s: %s", url, exc)
        return _text_result(
            f"Could not reach {url}: {exc}", is_error=True,
        )

    # A redirect chain is still the model choosing a host if it can end
    # anywhere, so the destination is checked as well as the request.
    if not str(response.url).startswith(_DOCS_ROOT):
        return _text_result(
            f"{url} redirected off {DOCS_BASE_URL}; refusing to read it.",
            is_error=True,
        )

    if response.status_code == 404:
        hint = (
            ""
            if url == _docs_url(DOCS_INDEX_PATH)
            else f" Fetch {DOCS_INDEX_PATH} for the index of every page."
        )
        return _text_result(f"No such page: {url}.{hint}", is_error=True)
    if response.status_code >= 400:
        return _text_result(
            f"{url} returned HTTP {response.status_code}.", is_error=True,
        )

    text = response.text
    if len(text) > _MAX_DOC_CHARS:
        text = (
            text[:_MAX_DOC_CHARS]
            + f"\n\n[truncated at {_MAX_DOC_CHARS} characters]"
        )
    return _text_result(f"# {response.url}\n\n{text}")


def _context_summary(name: str, config: Config) -> dict[str, Any]:
    ctx = config.contexts[name]
    sandboxed = is_sandboxed(ctx)
    summary: dict[str, Any] = {
        "name": name,
        "directory": ctx.directory,
        "description": ctx.description,
        "allowed_tools": list(ctx.allowed_tools),
        "sandboxed": sandboxed,
        "backend": effective_backend(ctx, config),
    }
    if sandboxed:
        summary["sandbox_backend"] = sandbox_backend(ctx)
    if ctx.disallowed_tools:
        summary["disallowed_tools"] = list(ctx.disallowed_tools)
    if ctx.model is not None:
        summary["model"] = ctx.model
    if ctx.effort is not None:
        summary["effort"] = ctx.effort
    if ctx.additional_directories:
        summary["additional_directories"] = list(ctx.additional_directories)
    if ctx.mcp:
        summary["mcp_servers"] = sorted(ctx.mcp)
    return summary


async def _validate_directory(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "").strip()
    if not path:
        return _text_result("path is required.", is_error=True)

    result = check_directory(path)
    if result["exists"]:
        return _text_result(f"{result['path']} exists and is a directory.")
    return _text_result(
        f"{result['path']} does not exist, or is not a directory."
    )


def build_supervisor_tools(
    config: Config, config_path: str | None = None
) -> list[OpenShrimpTool]:
    """The supervisor's tools, bound to *config*.

    Without a *config_path* the two write tools are not built at all.
    There is no file to write, no state token to hand out, and a tool
    that exists only to refuse is worse than one the model never sees.
    """
    path = Path(config_path) if config_path else None

    async def list_contexts(args: dict[str, Any]) -> dict[str, Any]:
        # Read from disk, not from the session's ``config``: the process
        # hot-reloads the file, so the snapshot this closure was built
        # with is stale from the second turn onwards — and the state
        # token has to describe the file the report came from.
        if path is None:
            current, token = config, None
        else:
            try:
                current, token = load_config(str(path)), state_token(path)
            except (OSError, ValueError) as exc:
                return _text_result(
                    f"config.yaml could not be read: {exc}. Tell the user; "
                    f"the running configuration is whatever loaded last.",
                    is_error=True,
                )

        report: dict[str, Any] = {
            "default_context": current.default_context,
            "backend": current.backend,
            "contexts": [
                _context_summary(name, current) for name in current.contexts
            ],
        }
        if token is not None:
            report["state_token"] = token
        if not current.contexts:
            report["note"] = (
                "No projects are configured yet. Add one with write_context, "
                "or the user can add one in the Mini App at /config."
            )
        return _text_result(json.dumps(report, indent=2))

    def write_handler(tool: str, write_path: Path) -> ToolHandler:
        """Bind one write tool to the file it may write.

        Plans and applies from scratch. The approval card ahead of this
        call planned the same write and checked the same token; both
        happen again because the card released a decision, not a lock —
        the file can move between the tap and this call, and the second
        check is what makes that a refusal rather than a write of
        something nobody read.
        """

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                plan = plan_config_write(write_path, tool, args)
                token = apply_plan(write_path, plan)
            except ConfigWriteRefused as exc:
                return _text_result(str(exc), is_error=True)
            except OSError as exc:
                logger.warning("Config write failed: %s", exc)
                return _text_result(
                    f"config.yaml could not be written: {exc}", is_error=True,
                )
            return _text_result(
                f"Done — {plan.headline[0].lower()}{plan.headline[1:]}. It is "
                f"live now; no restart is needed. The new state_token is "
                f"{token}."
            )

        return handler

    tools = [
        OpenShrimpTool(
            name="read_docs",
            description=(
                "Read a page of the OpenShrimp documentation. The host is "
                f"pinned to {DOCS_BASE_URL}; pass a path only. Start at "
                f"{DOCS_INDEX_PATH}, which indexes every page and gives the "
                "Markdown URL of each."
            ),
            input_schema=_read_docs_schema,
            read_only=True,
            handler=_read_docs,
        ),
        OpenShrimpTool(
            name="list_contexts",
            description=(
                "Report the projects configured on this OpenShrimp install: "
                "directory, description, tools, sandbox and model for each, "
                "plus the default project and agent backend. Also returns a "
                "state_token that write_context and remove_context require, "
                "so call this immediately before proposing a change."
            ),
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            handler=list_contexts,
        ),
        OpenShrimpTool(
            name="validate_directory",
            description=(
                "Check whether a path exists and is a directory. Answers "
                "the same question the configuration Mini App asks before "
                "it accepts a context directory."
            ),
            input_schema=_validate_directory_schema,
            read_only=True,
            handler=_validate_directory,
        ),
    ]

    # ``client_manager`` auto-approves by name without importing this module,
    # so the two lists have to be the same list.
    assert tuple(tool.name for tool in tools) == SUPERVISOR_TOOL_NAMES

    if path is None:
        return tools

    write_tools = [
        OpenShrimpTool(
            name=WRITE_CONTEXT,
            description=(
                "Add a project to config.yaml, or change one that is already "
                "there. Takes effect on the next turn with no restart. Needs "
                f"the state_token from list_contexts, and shows the user a "
                f"diff to approve before anything is written. Sets "
                f"{', '.join(WRITABLE_FIELDS)} and nothing else — the fields "
                "that decide what a project may run are set by the user in "
                "the Mini App at /config."
            ),
            input_schema=_write_context_schema,
            read_only=False,
            handler=write_handler(WRITE_CONTEXT, path),
        ),
        OpenShrimpTool(
            name=REMOVE_CONTEXT,
            description=(
                "Remove a project from config.yaml. Deletes no files — only "
                "the entry that made the directory reachable from Telegram. "
                "Needs the state_token from list_contexts, and shows the user "
                "a diff to approve first."
            ),
            input_schema=_remove_context_schema,
            read_only=False,
            handler=write_handler(REMOVE_CONTEXT, path),
        ),
    ]
    return tools + write_tools


__all__ = ["build_supervisor_tools"]
