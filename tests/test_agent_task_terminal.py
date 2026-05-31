from __future__ import annotations

import time

from open_shrimp import agent_tasks
from open_shrimp.db import ChatScope
from open_shrimp.terminal.jsonl_render import (
    render_openshrimp_agent_content,
    render_openshrimp_agent_lines,
)
from open_shrimp.terminal.log_source import resolve_task


async def _noop_abort() -> None:
    return None


def test_resolve_task_prefers_python_owned_agent_output(tmp_path) -> None:
    task_id = "atestterminal"
    output_path = tmp_path / f"{task_id}.jsonl"
    task = agent_tasks.AgentBackgroundTask(
        task_id=task_id,
        scope=ChatScope(chat_id=1, thread_id=None),
        context_name="default",
        parent_session_id="parent",
        child_session_id="child",
        tool_use_id=None,
        description="Explore code",
        prompt="Summarize it",
        subagent_type="explore",
        started_at=time.monotonic(),
        output_path=output_path,
        status="running",
        abort=_noop_abort,
    )

    agent_tasks.register_task(task)
    source = resolve_task(task_id, task_type="opencode_agent")

    assert source is not None
    assert source.path == output_path
    assert source.render == "openshrimp-agent-jsonl"
    assert source.is_active()

    agent_tasks.complete_task(task, "completed")
    assert not source.is_active()


def test_render_openshrimp_agent_transcript() -> None:
    raw = (
        '{"event":"launched","description":"Explore code",'
        '"subagent_type":"explore","prompt":"Summarize it"}\n'
        '{"event":"assistant_text","text":"hello"}\n'
        '{"event":"tool_start","tool":"Read",'
        '"tool_input":{"filePath":"/repo/README.md"}}\n'
        '{"event":"tool_result","is_error":false}\n'
        '{"event":"finished","status":"completed"}\n'
    )

    rendered = render_openshrimp_agent_content(raw)

    assert "Summarize it" in rendered
    assert "hello" in rendered
    assert "🔧 Read: /repo/README.md" in rendered
    assert "[tool done]" not in rendered
    assert "[completed]" not in rendered


def test_render_openshrimp_agent_lines_buffers_partial_line() -> None:
    rendered, remainder = render_openshrimp_agent_lines(
        '{"event":"assistant_text","text":"hello"}\n{"event"'
    )

    assert "hello" in rendered
    assert remainder == '{"event"'
