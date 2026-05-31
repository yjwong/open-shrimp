from __future__ import annotations

import pytest

from open_shrimp.agent_tool import AgentToolContext, create_agent_tool, validate_agent_args
from open_shrimp.opencode_client import OpenCodeClient, OpenCodeOptions

from tests.opencode_client.mock_server import MockOpenCode, session_idle, text_delta


def test_validate_agent_args_defaults_subagent() -> None:
    args = validate_agent_args({"description": "Search repo", "prompt": "Find it"})

    assert args.description == "Search repo"
    assert args.prompt == "Find it"
    assert args.subagent_type == "general"


def test_validate_agent_args_requires_prompt() -> None:
    with pytest.raises(ValueError, match="prompt is required"):
        validate_agent_args({"description": "Search repo"})


@pytest.mark.asyncio
async def test_foreground_agent_tool_runs_child_session(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        tool = create_agent_tool(
            AgentToolContext(client_getter=lambda: client, cwd="/repo")
        )
        child_id = ""

        original_create_session = client.create_session

        async def create_session_spy(**kwargs):
            nonlocal child_id
            child_id = await original_create_session(**kwargs)
            mock_server.script(child_id, [text_delta("p1", "agent answer"), session_idle()])
            return child_id

        client.create_session = create_session_spy  # type: ignore[method-assign]
        result = await tool.handler(
            {
                "description": "Explore code",
                "prompt": "Summarize it",
                "subagent_type": "explore",
            }
        )

    assert result["content"][0]["text"] == "agent answer"
    assert mock_server.created_sessions[-1]["body"]["agent"] == "explore"
    assert mock_server.created_sessions[-1]["body"]["parentID"]
    assert mock_server.prompts[-1]["session_id"] == child_id
    assert mock_server.prompts[-1]["body"]["agent"] == "explore"
