"""The reserved ``openshrimp`` supervisor context.

Two properties carry the design and both are asserted here: the name
cannot be taken by a user's project, and the context that answers to it
has no shell and no file tools.  The rest guards the routes into it —
only a user picking it may get there.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from open_shrimp.config import (
    RESERVED_CONTEXT_NAME,
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
    _validate_raw,
    load_config,
)
from open_shrimp.config_app.api import config_put_endpoint
from open_shrimp.db import ChatScope, init_db, set_active_context
from open_shrimp.handlers.utils import _get_context, _get_context_name
from open_shrimp.setup import _validate_context_name
from open_shrimp.supervisor import (
    SupervisorDispatchRefused,
    is_supervisor_context,
    resolve_context,
    selectable_contexts,
    supervisor_context,
    system_prompt,
)
from tests.config_app_stub import disable_auth, make_request, write_config

CHAT_ID = 500
SCOPE = ChatScope(chat_id=CHAT_ID, thread_id=None)

# What this context exists to withhold.  Stated here, as the test's own
# demand, rather than imported from the code under test — deriving it from
# the denial list would make the assertions below vacuous.
FORBIDDEN_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")


@pytest.fixture
def db(tmp_path):
    conn = asyncio.run(init_db(tmp_path / "openshrimp.sqlite3"))
    yield conn
    asyncio.run(conn.close())


def _config(contexts: dict[str, ContextConfig] | None = None) -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts=contexts or {},
        default_context=None,
        review=ReviewConfig(),
    )


def _project() -> ContextConfig:
    return ContextConfig(
        directory="/tmp", description="a project", allowed_tools=["LSP"],
    )


# ---------------------------------------------------------------------------
# The name is reserved
# ---------------------------------------------------------------------------


def test_wizard_rejects_the_reserved_name():
    message = _validate_context_name(RESERVED_CONTEXT_NAME)
    assert message is not None
    assert RESERVED_CONTEXT_NAME in message


def test_wizard_still_accepts_ordinary_names():
    assert _validate_context_name("my-project") is None
    assert _validate_context_name("open_shrimp") is None


def test_config_validation_rejects_the_reserved_name():
    raw = {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            RESERVED_CONTEXT_NAME: {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
            }
        },
    }
    with pytest.raises(ValueError, match="reserved"):
        _validate_raw(raw)


def test_config_validation_rejects_it_as_default_context():
    """It is not in ``contexts``, so it cannot be named as the default."""
    raw = {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {},
        "default_context": RESERVED_CONTEXT_NAME,
    }
    with pytest.raises(ValueError, match="default_context"):
        _validate_raw(raw)


# ---------------------------------------------------------------------------
# The Mini App cannot write it into contexts:
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_auth(monkeypatch):
    disable_auth(monkeypatch)


@pytest.mark.asyncio
async def test_mini_app_save_cannot_create_the_reserved_context(tmp_path, _no_auth):
    config_file = write_config(tmp_path)
    config = load_config(str(config_file))

    payload = {
        "allowed_users": [1],
        "default_context": "default",
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
            },
            RESERVED_CONTEXT_NAME: {
                "directory": "/",
                "description": "pwned",
                "allowed_tools": ["Bash"],
            },
        },
    }
    response = await config_put_endpoint(
        make_request(config, config_file, payload),
    )

    assert response.status_code == 422
    assert "reserved" in bytes(response.body).decode()
    # Refused before the write, so the file still says what it said.
    assert set(load_config(str(config_file)).contexts) == {"default"}


# ---------------------------------------------------------------------------
# Tool policy: no shell, no file tools
# ---------------------------------------------------------------------------


def test_supervisor_grants_no_file_tools_and_no_shell():
    ctx = supervisor_context()
    for tool in FORBIDDEN_TOOLS:
        assert tool not in ctx.allowed_tools, f"{tool} is granted"
        assert tool in ctx.disallowed_tools, (
            f"{tool} is merely absent from allowed_tools, which auto-approves "
            f"read-only file tools and prompts for the rest — it must be "
            f"denied outright"
        )


def test_supervisor_resolved_sdk_options_deny_the_file_tools():
    """The denial has to survive translation into what the backend runs."""
    from open_shrimp.backend.claude_sdk.options import translate_options
    from open_shrimp.backend.protocol import BackendOptions

    ctx = supervisor_context()
    sdk = translate_options(
        BackendOptions(
            cwd=ctx.directory,
            allowed_tools=list(ctx.allowed_tools),
            disallowed_tools=list(ctx.disallowed_tools),
        )
    )
    for tool in FORBIDDEN_TOOLS:
        assert tool in sdk.disallowed_tools
        assert tool not in (sdk.allowed_tools or [])


def test_supervisor_denials_reach_opencode_as_deny_rules():
    """OpenCode names tools in lower case; a deny also hides the tool."""
    from open_shrimp.backend.opencode.client import _rule_from_entry

    ctx = supervisor_context()
    denied = {
        _rule_from_entry(entry, "deny")["permission"]
        for entry in ctx.disallowed_tools
    }
    assert {"bash", "read", "write", "edit", "glob", "grep"} <= denied


def test_supervisor_allows_webfetch_on_both_backends():
    from open_shrimp.backend.opencode.client import _rule_from_entry

    ctx = supervisor_context()
    # The Claude SDK backend passes the name through unchanged.
    assert "WebFetch" in ctx.allowed_tools
    # OpenCode lowercases it to its own permission name.
    rule = _rule_from_entry("WebFetch", "allow")
    assert rule == {"permission": "webfetch", "pattern": "*", "action": "allow"}


def test_supervisor_is_not_sandboxed_but_has_a_directory_that_exists():
    """A label only — but the agent CLI refuses to start in a missing cwd."""
    from pathlib import Path

    from open_shrimp.config import is_sandboxed

    ctx = supervisor_context()
    assert not is_sandboxed(ctx)
    assert Path(ctx.directory).is_dir()


def test_supervisor_directory_falls_back_when_the_config_dir_is_absent(
    monkeypatch, tmp_path,
):
    from open_shrimp import supervisor as supervisor_module

    monkeypatch.setattr(
        supervisor_module, "DEFAULT_CONFIG_PATH", tmp_path / "gone" / "config.yaml",
    )
    assert supervisor_module._supervisor_directory() == str(
        supervisor_module.Path.home()
    )


def test_system_prompt_pins_the_documentation_host():
    prompt = system_prompt()
    assert "https://shrimp.wong.place" in prompt
    assert "/llms.txt" in prompt


# ---------------------------------------------------------------------------
# Reachability: only a user picking it
# ---------------------------------------------------------------------------


def test_picker_offers_the_supervisor_with_an_empty_contexts_map():
    from open_shrimp.handlers.commands import _build_context_page

    config = _config()
    assert selectable_contexts(config) == {
        RESERVED_CONTEXT_NAME: supervisor_context(),
    }

    _text, markup = _build_context_page(config, current=None, page=0)
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert callbacks == [f"ctx:{RESERVED_CONTEXT_NAME}"]


def test_picker_offers_the_supervisor_alongside_projects():
    from open_shrimp.handlers.commands import _build_context_page

    config = _config({"work": _project()})
    _text, markup = _build_context_page(config, current=None, page=0)
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert callbacks == ["ctx:work", f"ctx:{RESERVED_CONTEXT_NAME}"]


def test_supervisor_is_absent_from_configured_contexts():
    """Everything that acts without a user picking reads ``contexts``."""
    config = _config({"work": _project()})
    assert RESERVED_CONTEXT_NAME not in config.contexts


@pytest.mark.asyncio
async def test_an_unbound_scope_never_falls_back_to_the_supervisor(db):
    config = _config()
    assert await _get_context_name(SCOPE, config, db) is None
    assert await _get_context(SCOPE, config, db) is None


@pytest.mark.asyncio
async def test_a_scope_that_picked_the_supervisor_resolves_to_it(db):
    config = _config()
    await set_active_context(db, SCOPE, RESERVED_CONTEXT_NAME)

    assert await _get_context_name(SCOPE, config, db) == RESERVED_CONTEXT_NAME
    resolved = await _get_context(SCOPE, config, db)
    assert resolved is not None
    name, ctx = resolved
    assert name == RESERVED_CONTEXT_NAME
    assert ctx.disallowed_tools == supervisor_context().disallowed_tools


def test_resolve_context_answers_for_both_kinds():
    config = _config({"work": _project()})
    assert resolve_context(config, "work") is not None
    assert resolve_context(config, RESERVED_CONTEXT_NAME) is not None
    assert resolve_context(config, "nope") is None
    assert resolve_context(config, None) is None


# ---------------------------------------------------------------------------
# dispatch_registry cannot start a turn there
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_delivery_into_the_supervisor_scope_is_refused(db, monkeypatch):
    """The whole registry path — schedules, host_monitor, event pick-up."""
    from open_shrimp.handlers import messages

    delivered: list[str] = []

    async def _spy(prompt, *args, **kwargs):
        delivered.append(prompt)

    monkeypatch.setattr(messages, "_dispatch_to_agent", _spy)
    config = _config({"work": _project()})

    await set_active_context(db, SCOPE, "work")
    await messages.dispatch_from_registry("go", SCOPE, config, db, None)
    assert delivered == ["go"]

    await set_active_context(db, SCOPE, RESERVED_CONTEXT_NAME)
    with pytest.raises(SupervisorDispatchRefused):
        await messages.dispatch_from_registry("go again", SCOPE, config, db, None)
    assert delivered == ["go"]


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def test_the_built_tools_match_the_names_client_manager_auto_approves():
    """``client_manager`` approves by name without importing the builder."""
    from open_shrimp.supervisor import SUPERVISOR_TOOL_NAMES
    from open_shrimp.supervisor_tools import build_supervisor_tools

    built = tuple(tool.name for tool in build_supervisor_tools(_config()))
    assert built == SUPERVISOR_TOOL_NAMES
    assert set(built) == {"read_docs", "list_contexts", "validate_directory"}


def test_every_supervisor_tool_is_read_only():
    from open_shrimp.supervisor_tools import build_supervisor_tools

    assert all(tool.read_only for tool in build_supervisor_tools(_config()))


def test_read_docs_refuses_a_host_the_model_chose():
    from open_shrimp.supervisor_tools import _docs_url

    assert _docs_url("/llms.txt") == "https://shrimp.wong.place/llms.txt"
    assert _docs_url("llms/guides/contexts.md") == (
        "https://shrimp.wong.place/llms/guides/contexts.md"
    )
    # An absolute URL is refused outright.
    assert _docs_url("https://evil.example/x") is None
    assert _docs_url("http://evil.example/x") is None

    # Everything else either resolves onto the pinned host or is refused;
    # no input produces a URL somewhere else.
    for path in ("//evil.example/x", "../../evil", "/../..//evil.example/y"):
        resolved = _docs_url(path)
        assert resolved is None or resolved.startswith(
            "https://shrimp.wong.place/"
        ), f"{path!r} resolved to {resolved!r}"


@pytest.mark.asyncio
async def test_read_docs_will_not_follow_a_redirect_off_the_docs_host(monkeypatch):
    from open_shrimp import supervisor_tools

    class _Response:
        status_code = 200
        url = "https://evil.example/x"
        text = "gotcha"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr(
        supervisor_tools.httpx, "AsyncClient", lambda **kw: _Client(),
    )
    result = await supervisor_tools._read_docs({"path": "/llms.txt"})
    assert result.get("is_error")
    assert "gotcha" not in json.dumps(result)


@pytest.mark.asyncio
async def test_list_contexts_reports_the_configured_projects():
    from open_shrimp.supervisor_tools import build_supervisor_tools

    config = _config({"work": _project()})
    tool = next(
        t for t in build_supervisor_tools(config) if t.name == "list_contexts"
    )
    report = json.loads((await tool.handler({}))["content"][0]["text"])

    assert [c["name"] for c in report["contexts"]] == ["work"]
    assert report["contexts"][0]["directory"] == "/tmp"
    # The supervisor does not report itself as a configured project.
    assert RESERVED_CONTEXT_NAME not in {c["name"] for c in report["contexts"]}


@pytest.mark.asyncio
async def test_list_contexts_says_so_when_nothing_is_configured():
    from open_shrimp.supervisor_tools import build_supervisor_tools

    tool = next(
        t for t in build_supervisor_tools(_config()) if t.name == "list_contexts"
    )
    report = json.loads((await tool.handler({}))["content"][0]["text"])
    assert report["contexts"] == []
    assert "/config" in report["note"]


@pytest.mark.asyncio
async def test_validate_directory_agrees_with_the_mini_app(tmp_path):
    from open_shrimp.config_app.api import check_directory
    from open_shrimp.supervisor_tools import build_supervisor_tools

    tool = next(
        t
        for t in build_supervisor_tools(_config())
        if t.name == "validate_directory"
    )

    result = await tool.handler({"path": str(tmp_path)})
    assert "exists" in result["content"][0]["text"]
    assert check_directory(str(tmp_path))["exists"] is True

    missing = tmp_path / "nope"
    result = await tool.handler({"path": str(missing)})
    assert "does not exist" in result["content"][0]["text"]
    assert check_directory(str(missing))["exists"] is False


def test_is_supervisor_context():
    assert is_supervisor_context(RESERVED_CONTEXT_NAME)
    assert not is_supervisor_context("work")
    assert not is_supervisor_context(None)
