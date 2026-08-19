"""What setup offers to import, and what it refuses to.

``~/.claude.json`` holds every directory the Claude CLI has ever read
something in.  Most of those are not projects, so the filter is the whole
value of this step: offering a dozen folders the user never worked in makes
the tick list worse than the single path prompt it replaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_shrimp.backend.claude_sdk.projects import discover_claude_projects
from open_shrimp.main import _run_projects_discover


def _real(session: str = "abc-123", **extra) -> dict:
    entry = {
        "lastSessionId": session,
        "hasTrustDialogAccepted": True,
        "mcpServers": {},
    }
    entry.update(extra)
    return entry


def _write_claude_json(tmp_path: Path, monkeypatch, projects: dict) -> Path:
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return path


def test_stub_entries_are_dropped(tmp_path, monkeypatch):
    """A folder gets a key the moment anything there is read.  Seven of the
    twelve entries on a real machine hold only ``mcpServers``."""
    _write_claude_json(
        tmp_path,
        monkeypatch,
        {
            "/home/ada/stub": {"mcpServers": {"playwright": {"command": "npx"}}},
            "/home/ada/real": _real(),
        },
    )

    assert [p.directory for p in discover_claude_projects()] == ["/home/ada/real"]


@pytest.mark.parametrize(
    "entry",
    [
        {"hasTrustDialogAccepted": True},
        {"lastSessionId": "abc-123", "hasTrustDialogAccepted": False},
        {"lastSessionId": "", "hasTrustDialogAccepted": True},
    ],
    ids=["never-ran-a-session", "never-trusted", "empty-session-id"],
)
def test_both_conditions_are_required(tmp_path, monkeypatch, entry):
    """Either field alone admits a folder the user never chose to work in."""
    _write_claude_json(tmp_path, monkeypatch, {"/home/ada/maybe": entry})

    assert discover_claude_projects() == []


def test_newest_session_comes_first(tmp_path, monkeypatch):
    _write_claude_json(
        tmp_path,
        monkeypatch,
        {
            "/home/ada/older": _real(lastStartTime=1785596290405),
            "/home/ada/newest": _real(lastStartTime=1787158434050),
            "/home/ada/middle": _real(lastStartTime=1786727365747),
        },
    )

    assert [p.name for p in discover_claude_projects()] == [
        "newest",
        "middle",
        "older",
    ]


def test_a_missing_last_start_time_still_imports(tmp_path, monkeypatch):
    """The key is absent on older entries, so it decides the order only.  It
    cannot decide whether a trusted folder with a real session is a project."""
    _write_claude_json(
        tmp_path,
        monkeypatch,
        {
            "/home/ada/undated": _real(),
            "/home/ada/dated": _real(lastStartTime=1787158434050),
        },
    )

    found = discover_claude_projects()
    assert [p.name for p in found] == ["dated", "undated"]
    assert found[1].last_start_time is None


def test_the_resolver_honours_claude_config_dir(tmp_path, monkeypatch):
    """Claude Code inside the desktop app keeps its config elsewhere, and the
    variable is how it says where."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"projects": {"/home/ada/home-dir": _real()}}), encoding="utf-8"
    )
    (elsewhere / ".claude.json").write_text(
        json.dumps({"projects": {"/home/ada/config-dir": _real()}}), encoding="utf-8"
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert [p.name for p in discover_claude_projects()] == ["home-dir"]

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    assert [p.name for p in discover_claude_projects()] == ["config-dir"]


def test_a_missing_file_is_an_empty_list_not_an_error(tmp_path, monkeypatch):
    """A fresh machine has no ``~/.claude.json``, and setup must still reach
    its next question."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))

    assert discover_claude_projects() == []


def test_malformed_json_is_an_empty_list_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".claude.json").write_text("{ not json", encoding="utf-8")

    assert discover_claude_projects() == []


def test_discover_json_is_parsable(tmp_path, monkeypatch, capsys):
    _write_claude_json(
        tmp_path, monkeypatch, {"/home/ada/real": _real(lastStartTime=17)}
    )

    assert _run_projects_discover(json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["projects"] == [
        {"directory": "/home/ada/real", "name": "real", "last_start_time": 17}
    ]


def test_an_empty_result_is_still_valid_json(tmp_path, monkeypatch, capsys):
    """So a GUI can render "none found" rather than parse a failure."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))

    assert _run_projects_discover(json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == {"projects": []}
