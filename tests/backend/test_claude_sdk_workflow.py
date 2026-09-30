"""The Workflow tool's approval card, tool row and live progress body."""

from __future__ import annotations

from open_shrimp.backend.claude_sdk import workflow
from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy
from open_shrimp.backend.opencode.policy import OpenCodePolicy

SCRIPT = """export const meta = {
  name: 'review-changes',
  title: 'Review',
  description: 'Review changed files across dimensions',
  phases: [{ title: 'Review' }, { title: "Verify", detail: 'x' }],
}
const results = await agent('look at the diff')
"""


class TestApprovalCard:
    def test_names_the_run_and_lists_its_phases(self) -> None:
        text = ClaudeSdkPolicy().format_approval_text(
            "Workflow", {"script": SCRIPT, "args": {"pr": 12}}, None,
        )
        assert "**Workflow** `review-changes`" in text
        assert "Review changed files across dimensions" in text
        # meta's own title is not a phase.
        assert "**Phases:** Review → Verify" in text
        assert '**Args:** `{"pr": 12}`' in text
        assert "look at the diff" not in text

    def test_expanded_card_carries_the_script(self) -> None:
        text = ClaudeSdkPolicy().format_expanded_prompt(
            "Workflow", {"script": SCRIPT},
        )
        assert "```js" in text
        assert "look at the diff" in text

    def test_a_saved_workflow_is_named_by_its_source(self) -> None:
        text = workflow.format_approval(
            {"name": "spec", "resumeFromRunId": "wf_abc123"}, expanded=False,
        )
        assert "**Saved workflow:** `spec`" in text
        assert "**Resumes run:** `wf_abc123`" in text

    def test_show_script_falls_back_when_there_is_no_mini_app(self) -> None:
        extras = ClaudeSdkPolicy().approval_keyboard_extras(
            "Workflow", {"script": SCRIPT}, "tu-1", None,
            chat_id=1, thread_id=None, user_id=1, bot_token="t",
            is_private_chat=True,
        )
        assert [b.text for b in extras.primary_row_extras] == ["Show script"]
        assert extras.pre_primary_rows == []


class TestToolRow:
    def test_summary_is_the_meta_name(self) -> None:
        p = ClaudeSdkPolicy()
        assert p.summarize("Workflow", {"script": SCRIPT}, None) == "review-changes"
        assert p.summarize("Workflow", {"name": "spec"}, None) == "spec"


def _agent(index: int, phase: int, state: str, label: str) -> dict:
    return {
        "type": "workflow_agent", "index": index, "phaseIndex": phase,
        "phaseTitle": "", "label": label, "state": state,
    }


class TestProgressBody:
    def test_one_line_per_phase_with_running_agents_named(self) -> None:
        body = workflow.render_progress({"workflow_progress": [
            {"type": "workflow_phase", "index": 1, "title": "Review"},
            {"type": "workflow_phase", "index": 2, "title": "Verify"},
            _agent(1, 1, "done", "review:bugs"),
            _agent(2, 1, "error", "review:perf"),
            _agent(3, 2, "start", "verify:a.py"),
            _agent(4, 2, "done", "verify:b.py"),
        ]})
        assert body == (
            "⚠️ **Review** — 1/2 agents done · 1 failed<br>"
            "⏳ **Verify** — 1/2 agents done<br>↳ verify:a.py"
        )

    def test_an_announced_phase_with_no_agents_yet(self) -> None:
        body = workflow.render_progress({"workflow_progress": [
            {"type": "workflow_phase", "index": 1, "title": "Review"},
        ]})
        assert body == "▫️ **Review**"

    def test_a_running_agent_shows_its_last_tool(self) -> None:
        agent = _agent(1, 1, "progress", "tool-caller")
        agent["lastToolName"] = "Bash"
        agent["lastToolSummary"] = "ls  -la\n" + "x" * 60
        body = workflow.render_progress({"workflow_progress": [
            {"type": "workflow_phase", "index": 1, "title": "Tools"}, agent,
        ]})
        assert body == (
            "⏳ **Tools** — 0/1 agent done<br>"
            "↳ tool-caller · Bash `ls -la " + "x" * 33 + "...`"
        )

    def test_running_agents_past_three_collapse(self) -> None:
        body = workflow.render_progress({"workflow_progress": [
            _agent(i, 1, "start", f"a{i}") for i in range(1, 6)
        ]})
        assert body.endswith("↳ a3<br>↳ +2 more")
        assert "a4" not in body

    def test_a_progress_event_without_a_snapshot_renders_nothing(self) -> None:
        # The CLI drops the snapshot from token-count-only updates.
        assert workflow.render_progress({"usage": {}}) is None

    def test_only_workflow_tasks_get_a_body(self) -> None:
        data = {"workflow_progress": [
            {"type": "workflow_phase", "index": 1, "title": "Review"},
        ]}
        assert ClaudeSdkPolicy().render_task_progress("local_agent", data) is None
        assert ClaudeSdkPolicy().render_task_progress("local_workflow", data)
        assert OpenCodePolicy().render_task_progress("local_workflow", data) is None
