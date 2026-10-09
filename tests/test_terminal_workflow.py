"""Terminal Mini App access to the transcripts of a workflow's agents."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from open_shrimp.backend.claude_sdk import workflow
from open_shrimp.terminal import api, log_source

TASK_ID = "wx4y3pah7"
PROJECT = "-home-u-proj"
SESSION = "2afa4b65-1334-43be-8374-e1886d8ac27b"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workflow, "_transcript_dirs", {})
    monkeypatch.setattr(log_source, "_CLAUDE_TMP_BASE", tmp_path / "tmp")
    monkeypatch.setattr(log_source, "is_task_active", lambda tid: False)


def _run_dir(home: Path, run_id: str = "wf_1ecbcacd-086") -> Path:
    path = home / "projects" / PROJECT / SESSION / "subagents" / "workflows" / run_id
    path.mkdir(parents=True)
    return path


def _journal(run_dir: Path, *entries: dict) -> None:
    (run_dir / "journal.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries)
    )


def _started(agent_id: str, label: str, phase: str = "A") -> dict:
    return {"type": "started", "key": "k", "agentId": agent_id,
            "label": label, "phase": phase}


def _result(agent_id: str) -> dict:
    return {"type": "result", "key": "k", "agentId": agent_id, "result": "ok"}


def _launch(transcript_dir: str, task_id: str = TASK_ID) -> None:
    workflow.record_launch({
        "status": "async_launched",
        "taskId": task_id,
        "taskType": "local_workflow",
        "runId": "wf_1ecbcacd-086",
        "transcriptDir": transcript_dir,
    })


def test_record_launch_ignores_other_tool_results() -> None:
    workflow.record_launch({"taskId": "t1", "taskType": "local_agent",
                            "transcriptDir": "/x"})
    workflow.record_launch("Workflow launched in background.")
    workflow.record_launch(None)
    assert workflow.transcript_dir("t1") is None


def test_agents_are_running_until_they_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_dir(tmp_path / "home")
    _journal(run, {"type": "launched"}, _started("aaa1", "a1"),
             _started("aaa2", "a2", "B"), _result("aaa1"))
    _launch(str(run))
    monkeypatch.setattr(log_source, "is_task_active", lambda tid: True)

    agents = log_source.list_workflow_agents(TASK_ID)

    assert [(a.agent_id, a.label, a.phase, a.state) for a in agents] == [
        ("aaa1", "a1", "A", "done"),
        ("aaa2", "a2", "B", "running"),
    ]


def test_agent_without_result_is_stopped_once_the_task_ends(
    tmp_path: Path,
) -> None:
    run = _run_dir(tmp_path / "home")
    _journal(run, _started("aaa1", "a1"))
    _launch(str(run))

    [agent] = log_source.list_workflow_agents(TASK_ID)
    assert agent.state == "stopped"


def test_half_written_journal_line_is_skipped(tmp_path: Path) -> None:
    run = _run_dir(tmp_path / "home")
    _journal(run, _started("aaa1", "a1"))
    with (run / "journal.jsonl").open("a") as f:
        f.write('{"type":"started","agentId":"aa')
    _launch(str(run))

    assert [a.agent_id for a in log_source.list_workflow_agents(TASK_ID)] == [
        "aaa1",
    ]


def test_guest_transcript_dir_maps_to_the_sandbox_claude_home(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    context_dir = state_dir / "myctx"
    run = _run_dir(context_dir / "claude-home")
    _journal(run, _started("aaa1", "a1"))
    guest = f"/home/nobody-here/.claude/{run.relative_to(context_dir / 'claude-home')}"
    _launch(guest)
    managers = {"libvirt": SimpleNamespace(state_dir=state_dir)}

    agents = log_source.list_workflow_agents(TASK_ID, managers)
    assert [a.agent_id for a in agents] == ["aaa1"]

    source = log_source.resolve(
        "workflow_agent", TASK_ID, sandbox_managers=managers, agent_id="aaa1",
    )
    assert source is None  # no transcript written yet
    (run / "agent-aaa1.jsonl").write_text("{}\n")
    source = log_source.resolve(
        "workflow_agent", TASK_ID, sandbox_managers=managers, agent_id="aaa1",
    )
    assert source is not None
    assert source.path == run / "agent-aaa1.jsonl"
    assert source.render == "jsonl"


def test_finished_run_is_found_from_its_output_file_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    run = _run_dir(home)
    _journal(run, _started("aaa1", "a1"), _result("aaa1"))
    (run / "agent-aaa1.jsonl").write_text("{}\n")
    output = tmp_path / "tmp" / PROJECT / SESSION / "tasks" / f"{TASK_ID}.output"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"workflowProgress": [
        {"type": "workflow_phase", "index": 1, "title": "A"},
        {"type": "workflow_agent", "index": 1, "agentId": "aaa1"},
    ]}))
    monkeypatch.setattr(log_source, "_claude_homes", lambda managers: [home])

    [agent] = log_source.list_workflow_agents(TASK_ID)
    assert (agent.agent_id, agent.state) == ("aaa1", "done")


def test_unknown_run_lists_nothing() -> None:
    assert log_source.list_workflow_agents(TASK_ID) is None
    assert log_source.list_workflow_agents("../etc") is None


def test_agent_tail_ends_when_its_result_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_dir(tmp_path / "home")
    _journal(run, _started("aaa1", "a1"))
    (run / "agent-aaa1.jsonl").write_text("{}\n")
    _launch(str(run))
    monkeypatch.setattr(log_source, "is_task_active", lambda tid: True)

    source = log_source.resolve("workflow_agent", TASK_ID, agent_id="aaa1")
    assert source.is_active()
    _journal(run, _started("aaa1", "a1"), _result("aaa1"))
    assert not source.is_active()


def test_agent_id_is_validated(tmp_path: Path) -> None:
    run = _run_dir(tmp_path / "home")
    _launch(str(run))
    assert log_source.resolve(
        "workflow_agent", TASK_ID, agent_id="../../x",
    ) is None
    assert log_source.resolve("workflow_agent", TASK_ID) is None


def test_transcript_shows_the_task_without_the_harness_frame() -> None:
    from open_shrimp.terminal.transcript import parse_transcript

    prompt = (
        "[Workflow harness — computed task] The task text below was computed "
        "at runtime by a workflow script. The computed task text follows:\n"
        "  Run `echo one`.\n  Then return ok."
    )
    line = json.dumps({"type": "user", "message": {"content": prompt}})

    assert parse_transcript(line) == [
        {"kind": "prompt", "text": "Run `echo one`.\nThen return ok."},
    ]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _no_auth(request: Request) -> int:
        return 1

    monkeypatch.setattr(api, "_authenticate", _no_auth)
    app = Starlette(routes=[
        Route("/api/terminal/workflow", api.workflow_agents_endpoint),
    ])
    return TestClient(app)


def test_endpoint_lists_agents(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_dir(tmp_path / "home")
    _journal(run, _started("aaa1", "a1"))
    _launch(str(run))
    monkeypatch.setattr(api, "is_task_active", lambda tid: True)
    monkeypatch.setattr(log_source, "is_task_active", lambda tid: True)

    resp = client.get(f"/api/terminal/workflow?id={TASK_ID}")

    assert resp.status_code == 200
    assert resp.json() == {"active": True, "agents": [
        {"agent_id": "aaa1", "label": "a1", "phase": "A", "state": "running"},
    ]}


def test_endpoint_running_run_without_transcripts_is_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "is_task_active", lambda tid: True)
    resp = client.get(f"/api/terminal/workflow?id={TASK_ID}")
    assert resp.json() == {"active": True, "agents": []}


def test_endpoint_unknown_finished_run_is_404(client: TestClient) -> None:
    resp = client.get(f"/api/terminal/workflow?id={TASK_ID}")
    assert resp.status_code == 404
