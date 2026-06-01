from __future__ import annotations

import pytest

from open_shrimp.opencode_client.client import OpenCodeClient
from open_shrimp.opencode_client.options import OpenCodeOptions
from tests.opencode_client.mock_server import MockOpenCode


def _client(allowed_tools: list[str] | None = None) -> OpenCodeClient:
    return OpenCodeClient(
        OpenCodeOptions(
            cwd="/tmp/project",
            provider="openai",
            model="gpt-5",
            allowed_tools=allowed_tools,
        )
    )


def test_todowrite_is_auto_allowed_by_default() -> None:
    rules = _client()._build_initial_rules()

    assert {
        "permission": "todowrite",
        "pattern": "*",
        "action": "allow",
    } in rules


def test_mutating_tools_are_not_auto_allowed_from_config() -> None:
    rules = _client(["Edit", "Write", "ApplyPatch"])._build_initial_rules()

    assert not any(
        rule["permission"] in {"edit", "write", "apply_patch"}
        and rule["action"] == "allow"
        for rule in rules
    )


def test_openshrimp_schedule_mutations_ask_by_default() -> None:
    rules = _client(["openshrimp_send_file"])._build_initial_rules()

    assert {
        "permission": "openshrimp_create_schedule",
        "pattern": "*",
        "action": "ask",
    } in rules
    assert {
        "permission": "openshrimp_delete_schedule",
        "pattern": "*",
        "action": "ask",
    } in rules


def test_openshrimp_schedule_mutations_can_be_allowed_explicitly() -> None:
    rules = _client(["openshrimp_create_schedule"])._build_initial_rules()

    matching = [
        rule for rule in rules
        if rule["permission"] == "openshrimp_create_schedule"
    ]
    assert matching[-1] == {
        "permission": "openshrimp_create_schedule",
        "pattern": "*",
        "action": "allow",
    }


def test_old_sdk_mcp_tool_names_map_to_opencode_names() -> None:
    rules = _client(["mcp__openshrimp__send_file"])._build_initial_rules()

    assert {
        "permission": "openshrimp_send_file",
        "pattern": "*",
        "action": "allow",
    } in rules


def test_host_bash_is_never_auto_allowed_from_config() -> None:
    rules = _client([
        "openshrimp_host_bash",
        "mcp__openshrimp__host_bash",
    ])._build_initial_rules()

    assert not any(
        rule["permission"] == "openshrimp_host_bash"
        and rule["action"] == "allow"
        for rule in rules
    )


def test_builtin_task_tool_is_disabled_by_default() -> None:
    rules = _client()._build_initial_rules()

    assert rules[-1] == {
        "permission": "task",
        "pattern": "*",
        "action": "deny",
    }


def test_builtin_task_tool_deny_overrides_allowed_tools() -> None:
    rules = _client(["Task", "task"])._build_initial_rules()

    matching = [rule for rule in rules if rule["permission"] == "task"]
    assert matching[-1] == {
        "permission": "task",
        "pattern": "*",
        "action": "deny",
    }


@pytest.mark.asyncio
async def test_patch_session_permissions_appends_rule(
    mock_server: MockOpenCode,
    wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        rule = {"permission": "bash", "pattern": "git *", "action": "allow"}
        await client.patch_session_permissions(sid, [rule])
        session = await client.get_session_info(sid)

    assert mock_server.patched_sessions[-1] == {
        "session_id": sid,
        "body": {"permission": [rule]},
    }
    assert rule in session["permission"]


@pytest.mark.asyncio
async def test_patch_config_permission_writes_bash_rule(
    mock_server: MockOpenCode,
    wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        await client.patch_config_permission({"bash": {"git *": "allow"}})
        config = await client.get_config()

    assert mock_server.config_patches[-1] == {
        "body": {"permission": {"bash": {"git *": "allow"}}},
        "params": {"directory": "/tmp/project"},
    }
    assert config["permission"]["bash"]["git *"] == "allow"
