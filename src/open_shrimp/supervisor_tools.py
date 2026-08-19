"""The supervisor's tools: read the docs, report the config, check a path.

All three are read-only.  The supervisor has no shell and no file tools
(see :mod:`open_shrimp.supervisor`), so this module is the whole of what
it can do.

``read_docs`` builds its own URL from a path against a base pinned in
code.  The model supplies a path, never a host, and a path that resolves
off the documentation site is refused rather than fetched.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from open_shrimp.config import (
    Config,
    check_directory,
    effective_backend,
    is_sandboxed,
    sandbox_backend,
)
from open_shrimp.supervisor import (
    DOCS_BASE_URL,
    DOCS_INDEX_PATH,
    SUPERVISOR_TOOL_NAMES,
)
from open_shrimp.tools import OpenShrimpTool, _text_result

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


def build_supervisor_tools(config: Config) -> list[OpenShrimpTool]:
    """The supervisor's three read-only tools, bound to *config*."""

    async def list_contexts(args: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = {
            "default_context": config.default_context,
            "backend": config.backend,
            "contexts": [
                _context_summary(name, config) for name in config.contexts
            ],
        }
        if not config.contexts:
            report["note"] = (
                "No projects are configured yet. Contexts are added in the "
                "configuration Mini App, reachable with /config."
            )
        return _text_result(json.dumps(report, indent=2))

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
                "Report the contexts configured on this OpenShrimp install: "
                "directory, description, tools, sandbox and model for each, "
                "plus the default context and agent backend."
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
    return tools


__all__ = ["build_supervisor_tools"]
