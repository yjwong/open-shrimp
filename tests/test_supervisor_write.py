"""The supervisor writing ``config.yaml``.

A context is a directory plus a tool policy, so writing one is a
privilege escalation: a context with a broad directory and a shell in
``allowed_tools`` is a host shell that never passed a ``host_bash``
approval.  Three things stand in the way and each is asserted here.

* The tool cannot set the fields that grant power at all.
* Every write shows a diff and asks, ahead of every rule that could
  otherwise answer for the user.
* A write is bound to the file it was planned against, at the call, at
  the approval, and again at the write.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.opencode.policy import OpenCodePolicy
from open_shrimp.backend.types import (
    PermissionResultAllow,
    ToolPermissionContext,
)
from open_shrimp.config import RESERVED_CONTEXT_NAME, load_config
from open_shrimp.handlers.approval import (
    _CONFIG_WRITE_APPROVE_PREFIX,
    _CONFIG_WRITE_DENY_PREFIX,
    _send_config_write_approval,
)
from open_shrimp.handlers.state import _approval_futures
from open_shrimp.hooks import make_can_use_tool
from open_shrimp.supervisor import (
    SUPERVISOR_TOOL_NAMES,
    SUPERVISOR_WRITE_TOOL_NAMES,
)
from open_shrimp.supervisor_tools import build_supervisor_tools
from open_shrimp.supervisor_write import (
    WITHHELD_FIELDS,
    ConfigWriteRefused,
    apply_plan,
    plan_config_write,
    state_token,
)

CHAT_ID = 700


def _config_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "telegram": {"token": "1:aaa"},
        "allowed_users": [7],
        "default_context": "work",
        "contexts": {
            "work": {
                "directory": "/tmp",
                "description": "a project",
                "allowed_tools": ["LSP"],
            },
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config_dict()), encoding="utf-8")
    return path


def _token(path: Path) -> str:
    return state_token(path)


def _write_args(path: Path, **kwargs: Any) -> dict[str, Any]:
    return {"state_token": _token(path), **kwargs}


def _tools(config_file: Path):
    config = load_config(str(config_file))
    return {
        tool.name: tool
        for tool in build_supervisor_tools(config, str(config_file))
    }


async def _call(config_file: Path, tool: str, **args: Any) -> dict[str, Any]:
    return await _tools(config_file)[tool].handler(args)


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The state token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_contexts_hands_out_a_token_that_matches_the_file(
    config_file: Path,
):
    import json

    report = json.loads(_text(await _call(config_file, "list_contexts")))
    assert report["state_token"] == _token(config_file)


@pytest.mark.asyncio
async def test_list_contexts_reads_the_file_not_the_session_snapshot(
    config_file: Path,
):
    import json

    config = load_config(str(config_file))
    tools = {
        t.name: t for t in build_supervisor_tools(config, str(config_file))
    }

    # The process hot-reloads config.yaml, so a snapshot taken when the
    # session started is stale by its second turn.
    added = _config_dict()
    added["contexts"]["later"] = {
        "directory": "/tmp", "description": "added since", "allowed_tools": [],
    }
    config_file.write_text(yaml.safe_dump(added), encoding="utf-8")

    report = json.loads(_text(await tools["list_contexts"].handler({})))
    assert {c["name"] for c in report["contexts"]} == {"work", "later"}


@pytest.mark.asyncio
async def test_a_stale_token_is_refused_and_says_the_file_changed(
    config_file: Path, tmp_path: Path
):
    stale = _write_args(
        config_file, name="new", directory=str(tmp_path), description="new",
    )
    config_file.write_text(
        yaml.safe_dump(_config_dict(allowed_users=[7, 8])), encoding="utf-8"
    )

    result = await _call(config_file, "write_context", **stale)

    assert result.get("is_error")
    assert "changed" in _text(result)
    assert "list_contexts" in _text(result)
    assert "new" not in load_config(str(config_file)).contexts


@pytest.mark.asyncio
async def test_an_unrelated_edit_invalidates_the_token(config_file: Path):
    # ``allowed_users`` is nowhere near ``contexts``, but a config that
    # moved under the agent is a config it has not read.
    before = _token(config_file)
    config_file.write_text(
        yaml.safe_dump(_config_dict(allowed_users=[7, 8])), encoding="utf-8"
    )
    assert _token(config_file) != before


# ---------------------------------------------------------------------------
# The reserved name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reserved_name_cannot_be_created(
    config_file: Path, tmp_path: Path
):
    result = await _call(
        config_file,
        "write_context",
        **_write_args(
            config_file,
            name=RESERVED_CONTEXT_NAME,
            directory=str(tmp_path),
            description="me",
        ),
    )
    assert result.get("is_error")
    assert RESERVED_CONTEXT_NAME not in config_file.read_text()


@pytest.mark.asyncio
async def test_the_reserved_name_cannot_be_removed(config_file: Path):
    result = await _call(
        config_file,
        "remove_context",
        **_write_args(config_file, name=RESERVED_CONTEXT_NAME),
    )
    assert result.get("is_error")
    assert "built in code" in _text(result)


@pytest.mark.asyncio
async def test_the_reserved_name_cannot_be_altered_into_contexts(
    config_file: Path, tmp_path: Path
):
    # Even were the name check removed, ``_validate_raw`` refuses it, so
    # the escalation needs two independent failures to land.
    existing = _config_dict()
    existing["contexts"][RESERVED_CONTEXT_NAME] = {
        "directory": str(tmp_path), "description": "x", "allowed_tools": [],
    }
    config_file.write_text(yaml.safe_dump(existing), encoding="utf-8")

    with pytest.raises(ConfigWriteRefused):
        plan_config_write(
            config_file,
            "write_context",
            _write_args(config_file, name="work", description="renamed"),
        )


# ---------------------------------------------------------------------------
# What the tool may not set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(WITHHELD_FIELDS))
@pytest.mark.asyncio
async def test_a_power_granting_field_is_refused_not_dropped(
    config_file: Path, field: str
):
    # Dropping it silently would let the agent report a change it never
    # made — which is worse than refusing, because nobody finds out.
    result = await _call(
        config_file,
        "write_context",
        **_write_args(config_file, name="work", **{field: ["Bash"]}),
    )
    assert result.get("is_error")
    assert field in _text(result)
    assert "/config" in _text(result)


@pytest.mark.asyncio
async def test_a_new_project_gets_the_wizards_tool_list(
    config_file: Path, tmp_path: Path
):
    await _call(
        config_file,
        "write_context",
        **_write_args(
            config_file, name="api", directory=str(tmp_path), description="api",
        ),
    )
    written = load_config(str(config_file)).contexts["api"]
    assert written.allowed_tools == ["LSP", "AskUserQuestion"]


@pytest.mark.asyncio
async def test_an_update_leaves_the_withheld_fields_on_disk_alone(
    config_file: Path, tmp_path: Path
):
    rich = _config_dict()
    rich["contexts"]["work"]["allowed_tools"] = ["Bash", "LSP"]
    rich["contexts"]["work"]["additional_directories"] = ["/srv"]
    rich["contexts"]["work"]["mcp"] = {"linear": {"command": "linear-mcp"}}
    config_file.write_text(yaml.safe_dump(rich), encoding="utf-8")

    await _call(
        config_file,
        "write_context",
        **_write_args(config_file, name="work", description="renamed"),
    )

    raw = yaml.safe_load(config_file.read_text())["contexts"]["work"]
    assert raw["description"] == "renamed"
    assert raw["allowed_tools"] == ["Bash", "LSP"]
    assert raw["additional_directories"] == ["/srv"]
    assert raw["mcp"] == {"linear": {"command": "linear-mcp"}}


@pytest.mark.asyncio
async def test_a_sandbox_can_be_added_but_not_edited(
    config_file: Path, tmp_path: Path
):
    await _call(
        config_file,
        "write_context",
        **_write_args(config_file, name="work", sandbox="docker"),
    )
    assert load_config(str(config_file)).contexts["work"].sandbox.backend == "docker"

    # Rewriting the block wholesale would drop settings the user chose
    # and never name them on the diff, so it is refused outright.
    result = await _call(
        config_file,
        "write_context",
        **_write_args(config_file, name="work", sandbox="libvirt"),
    )
    assert result.get("is_error")
    assert "/config" in _text(result)
    assert load_config(str(config_file)).contexts["work"].sandbox.backend == "docker"


# ---------------------------------------------------------------------------
# Validation, comments, and the shape of the file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_that_would_not_validate_never_reaches_disk(
    config_file: Path, tmp_path: Path
):
    before = config_file.read_text()
    result = await _call(
        config_file,
        "write_context",
        **_write_args(
            config_file,
            name="api",
            directory=str(tmp_path),
            description="api",
            effort="glacial",
        ),
    )
    assert result.get("is_error")
    assert config_file.read_text() == before


@pytest.mark.asyncio
async def test_a_directory_that_does_not_exist_is_refused(config_file: Path):
    result = await _call(
        config_file,
        "write_context",
        **_write_args(
            config_file,
            name="api",
            directory="/no/such/place",
            description="api",
        ),
    )
    assert result.get("is_error")
    assert "validate_directory" in _text(result)


@pytest.mark.asyncio
async def test_comments_survive_a_write(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# the whole file\n"
        "telegram:\n"
        "  token: 1:aaa\n"
        "allowed_users: [7]\n"
        "contexts:\n"
        "  # why this project exists\n"
        "  work:\n"
        "    directory: /tmp\n"
        "    description: a project\n"
        "    allowed_tools: [LSP]\n",
        encoding="utf-8",
    )

    await _call(
        path,
        "write_context",
        **_write_args(path, name="work", description="renamed"),
    )

    text = path.read_text()
    assert "# the whole file" in text
    assert "# why this project exists" in text


@pytest.mark.asyncio
async def test_removing_the_default_project_clears_the_default(
    config_file: Path,
):
    result = await _call(
        config_file, "remove_context", **_write_args(config_file, name="work"),
    )
    assert not result.get("is_error")
    config = load_config(str(config_file))
    assert config.contexts == {}
    assert config.default_context is None


@pytest.mark.asyncio
async def test_removing_a_project_that_is_not_there_is_refused(
    config_file: Path,
):
    result = await _call(
        config_file, "remove_context", **_write_args(config_file, name="ghost"),
    )
    assert result.get("is_error")
    assert "list_contexts" in _text(result)


# ---------------------------------------------------------------------------
# A write is live on the next turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_context_the_supervisor_added_is_usable_on_the_next_turn(
    monkeypatch, config_file: Path, tmp_path: Path
):
    from open_shrimp.bot import _watch_config

    project = tmp_path / "api"
    project.mkdir()
    bot_data: dict[str, Any] = {"config": load_config(str(config_file))}

    await _call(
        config_file,
        "write_context",
        **_write_args(
            config_file, name="api", directory=str(project), description="api",
        ),
    )

    # No restart: the watcher reloads the file and the new project is in
    # the config every handler reads from.
    async def fake_awatch(_path, **_kwargs):
        yield {("modified", str(config_file))}

    import watchfiles

    monkeypatch.setattr(watchfiles, "awatch", fake_awatch)
    bot = type("_Bot", (), {"send_message": AsyncMock()})()
    await _watch_config(str(config_file), bot_data, bot)

    assert "api" in bot_data["config"].contexts
    assert bot_data["config"].contexts["api"].directory == str(project)


# ---------------------------------------------------------------------------
# The approval
# ---------------------------------------------------------------------------


class _CardBot:
    """Records the card and lets a test tap one of its buttons."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.message_id = 42
        self.markup = None

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self.markup = kwargs.get("reply_markup")
        return type("_Msg", (), {"message_id": self.message_id})()

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append(text)

    async def edit_message_reply_markup(self, **kwargs):
        pass

    def buttons(self) -> list[str]:
        if self.markup is None:
            return []
        return [b.callback_data for row in self.markup.inline_keyboard for b in row]


async def _tap(bot: _CardBot, prefix: str) -> None:
    """Resolve whichever future the card registered under *prefix*."""
    for _ in range(200):
        data = next((d for d in bot.buttons() if d.startswith(prefix)), None)
        future = _approval_futures.get(data) if data else None
        if future is not None and not future.done():
            future.set_result(prefix == _CONFIG_WRITE_APPROVE_PREFIX)
            return
        await asyncio.sleep(0)
    raise AssertionError(f"no live {prefix} future")


@pytest.mark.asyncio
async def test_the_card_shows_a_diff_and_approving_permits_the_write(
    config_file: Path, tmp_path: Path
):
    bot = _CardBot()
    args = _write_args(
        config_file, name="api", directory=str(tmp_path), description="api",
    )
    task = asyncio.create_task(
        _send_config_write_approval(
            bot, CHAT_ID, str(config_file), "write_context", args, "tu-1",
        )
    )
    await _tap(bot, _CONFIG_WRITE_APPROVE_PREFIX)

    assert await task is None
    assert "```diff" in bot.sent[0]
    # Verbatim, because inside a code entity Telegram honours only a
    # backslash and a backtick: escaping the rest prints the backslashes,
    # and a diff is almost entirely characters the prose escaper touches.
    assert "\n+  api:\n" in bot.sent[0]
    assert "\\+" not in bot.sent[0].split("```diff")[1]
    assert "Approved" in bot.edits[-1]


@pytest.mark.asyncio
async def test_the_card_carries_no_way_to_stop_being_asked(
    config_file: Path, tmp_path: Path
):
    bot = _CardBot()
    args = _write_args(
        config_file, name="api", directory=str(tmp_path), description="api",
    )
    task = asyncio.create_task(
        _send_config_write_approval(
            bot, CHAT_ID, str(config_file), "write_context", args, "tu-2",
        )
    )
    await _tap(bot, _CONFIG_WRITE_DENY_PREFIX)
    refusal = await task

    assert sorted(
        d.split(":")[0] for d in bot.buttons()
    ) == ["cw_approve", "cw_deny"]
    assert refusal is not None and "denied" in refusal


def test_a_windows_directory_survives_the_card_intact():
    # Verified against the live Bot API: the prose escaper does not escape
    # a backslash, and Telegram then eats it — 'C:\\Users\\ada' is shown as
    # 'C:Usersada'.  On a card whose whole job is to show the user the
    # path they are approving, a silently altered path is the one thing it
    # must not do.
    from open_shrimp.handlers.approval import _format_config_write

    card = _format_config_write(
        "Add the project 'win'", r"+    directory: C:\Users\ada\my-project",
    )
    assert r"C:\\Users\\ada\\my-project" in card


@pytest.mark.asyncio
async def test_the_bot_token_never_reaches_the_diff(tmp_path: Path):
    # A small config puts ``telegram:`` inside the diff's context lines.
    path = tmp_path / "config.yaml"
    path.write_text(
        "telegram:\n  token: 12345:SECRETVALUE\nallowed_users: [7]\ncontexts:\n"
        "  work:\n    directory: /tmp\n    description: p\n    allowed_tools: []\n",
        encoding="utf-8",
    )
    plan = plan_config_write(
        path, "write_context", _write_args(path, name="work", description="q"),
    )
    assert "SECRETVALUE" not in plan.diff


@pytest.mark.asyncio
async def test_a_file_change_under_a_waiting_card_re_renders_and_does_not_write(
    config_file: Path, tmp_path: Path
):
    bot = _CardBot()
    before = config_file.read_text()
    args = _write_args(
        config_file, name="api", directory=str(tmp_path), description="api",
    )
    task = asyncio.create_task(
        _send_config_write_approval(
            bot, CHAT_ID, str(config_file), "write_context", args, "tu-3",
        )
    )
    while not bot.sent:
        await asyncio.sleep(0)

    # The user edits the same file in the Mini App while the card waits.
    moved = _config_dict()
    moved["contexts"]["other"] = {
        "directory": "/tmp", "description": "meanwhile", "allowed_tools": [],
    }
    config_file.write_text(yaml.safe_dump(moved), encoding="utf-8")

    await _tap(bot, _CONFIG_WRITE_APPROVE_PREFIX)
    refusal = await task

    assert refusal is not None
    assert "changed" in refusal
    # The card is re-rendered against the file as it now stands...
    assert "```diff" in bot.edits[-1]
    assert "changed while this was waiting" in bot.edits[-1]
    # ...and the write the user tapped for did not happen.
    assert "api" not in config_file.read_text()
    assert config_file.read_text() != before


@pytest.mark.asyncio
async def test_an_unreadable_config_is_a_refusal_not_a_dead_turn(
    config_file: Path,
):
    # The supervisor is asked to fix config.yaml exactly when config.yaml
    # is broken, so a parser exception here would take the turn down at
    # the moment the user most needs an answer.  The token is taken after
    # the file is broken, so it matches and the parse is really reached.
    config_file.write_text("contexts:\n  work:\n bad\n", encoding="utf-8")

    bot = _CardBot()
    refusal = await _send_config_write_approval(
        bot,
        CHAT_ID,
        str(config_file),
        "write_context",
        {"state_token": state_token(config_file), "name": "api"},
        "tu-7",
    )
    assert refusal is not None and "cannot be parsed" in refusal
    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_broken_file_still_refuses_when_the_token_is_stale_too(
    config_file: Path,
):
    token = _token(config_file)
    config_file.write_text("contexts:\n  work:\n bad\n", encoding="utf-8")

    bot = _CardBot()
    refusal = await _send_config_write_approval(
        bot, CHAT_ID, str(config_file), "write_context",
        {"state_token": token, "name": "api"}, "tu-8",
    )
    assert refusal is not None and "changed" in refusal
    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_plan_that_can_never_apply_never_troubles_the_user(
    config_file: Path,
):
    bot = _CardBot()
    refusal = await _send_config_write_approval(
        bot,
        CHAT_ID,
        str(config_file),
        "write_context",
        {"state_token": "not-the-token", "name": "api"},
        "tu-4",
    )
    assert refusal is not None and "changed" in refusal
    assert bot.sent == []


@pytest.mark.asyncio
async def test_the_write_refuses_once_more_after_the_approval(
    config_file: Path, tmp_path: Path
):
    # The card releases a decision, not a lock.
    plan = plan_config_write(
        config_file,
        "write_context",
        _write_args(
            config_file, name="api", directory=str(tmp_path), description="api",
        ),
    )
    config_file.write_text(
        yaml.safe_dump(_config_dict(allowed_users=[7, 9])), encoding="utf-8"
    )
    with pytest.raises(ConfigWriteRefused):
        apply_plan(config_file, plan)
    assert "api" not in config_file.read_text()


# ---------------------------------------------------------------------------
# The hook slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy,wire",
    [
        (ClaudeSdkPolicy(), "mcp__openshrimp__write_context"),
        (OpenCodePolicy(), "openshrimp_write_context"),
    ],
)
@pytest.mark.asyncio
async def test_a_config_write_always_asks_even_under_accept_all(policy, wire):
    asked: list[str] = []

    async def approval(tool, tool_input, tool_use_id):
        asked.append(tool)
        return None

    can_use_tool = make_can_use_tool(
        request_approval=AsyncMock(return_value=True),
        cwd="/tmp",
        # Every grant that could otherwise answer for the user, on.
        is_edit_auto_approved=lambda: True,
        is_tool_auto_approved=lambda *_: True,
        is_containerized=True,
        request_config_write_approval=approval,
        policy=policy,
    )

    result = await can_use_tool(
        wire, {"name": "api"}, ToolPermissionContext(tool_use_id="tu-5"),
    )

    assert isinstance(result, PermissionResultAllow)
    assert asked == ["write_context"]


@pytest.mark.asyncio
async def test_a_config_write_is_denied_when_no_approval_is_wired():
    can_use_tool = make_can_use_tool(
        request_approval=AsyncMock(return_value=True),
        cwd="/tmp",
        is_edit_auto_approved=lambda: True,
        is_containerized=True,
        policy=ClaudeSdkPolicy(),
    )
    result = await can_use_tool(
        "mcp__openshrimp__remove_context",
        {"name": "work"},
        ToolPermissionContext(tool_use_id="tu-6"),
    )
    assert not isinstance(result, PermissionResultAllow)


def test_every_card_senders_prefix_is_one_the_phone_probes():
    # The registry exists so a new card sender cannot be added without
    # the phone learning about it.
    from open_shrimp.handlers import approval
    from open_shrimp.handlers.state import APPROVE_CALLBACK_PREFIXES

    assert approval._CONFIG_WRITE_APPROVE_PREFIX in APPROVE_CALLBACK_PREFIXES
    assert approval._HOST_BASH_APPROVE_PREFIX in APPROVE_CALLBACK_PREFIXES
    assert "approve:" in APPROVE_CALLBACK_PREFIXES


def test_the_write_tools_are_absent_from_the_auto_approved_list():
    # Auto-approval is spelled by adding a tool to ``allowed_tools``,
    # which is also what stops it ever reaching ``can_use_tool``.
    assert not set(SUPERVISOR_WRITE_TOOL_NAMES) & set(SUPERVISOR_TOOL_NAMES)


def test_no_write_tools_exist_without_a_config_path(config_file: Path):
    config = load_config(str(config_file))
    names = {t.name for t in build_supervisor_tools(config, None)}
    assert names == set(SUPERVISOR_TOOL_NAMES)


def test_every_write_tool_declares_itself_as_writing(config_file: Path):
    config = load_config(str(config_file))
    tools = {t.name: t for t in build_supervisor_tools(config, str(config_file))}
    for name in SUPERVISOR_WRITE_TOOL_NAMES:
        assert tools[name].read_only is False
