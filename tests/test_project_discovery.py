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
from open_shrimp.main import _run_projects_discover, _run_projects_name


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
        {
            "directory": "/home/ada/real",
            "name": "real",
            "context_name": "real",
            "last_start_time": 17,
        }
    ]


# ---------------------------------------------------------------------------
# Naming
#
# A folder name is under no obligation to be a legal context name, and the
# user never typed it, so a refusal here would be a dead end in the middle of
# an import.  These are the two ways that happens.
# ---------------------------------------------------------------------------


def test_a_folder_name_a_context_cannot_take_is_converted(tmp_path, monkeypatch):
    """``talenthub.glints.com`` is a real folder and an illegal context name."""
    _write_claude_json(
        tmp_path,
        monkeypatch,
        {
            "/home/ada/work/talenthub.glints.com": _real(lastStartTime=3),
            "/home/ada/My Notes": _real(lastStartTime=2),
            "/home/ada/....": _real(lastStartTime=1),
        },
    )

    found = discover_claude_projects()
    assert [p.context_name for p in found] == [
        "talenthub-glints-com",
        "My-Notes",
        "project",
    ]
    # The folder is still named as it reads on disk, because that is what the
    # user recognises in the tick list.
    assert [p.name for p in found] == ["talenthub.glints.com", "My Notes", "...."]

    from open_shrimp.config import _validate_context_name

    assert all(_validate_context_name(p.context_name) is None for p in found)


def test_two_projects_with_one_basename_get_distinct_names(tmp_path, monkeypatch):
    """A mapping would keep the last one, and an import of two would report
    as one."""
    _write_claude_json(
        tmp_path,
        monkeypatch,
        {
            "/home/ada/work/api": _real(lastStartTime=3),
            "/home/ada/play/api": _real(lastStartTime=2),
            "/home/ada/old/a.p.i": _real(lastStartTime=1),
        },
    )

    found = discover_claude_projects()
    # Newest first keeps the plain name: it is the folder the user has most
    # recently worked in.
    assert [(p.directory, p.context_name) for p in found] == [
        ("/home/ada/work/api", "api"),
        ("/home/ada/play/api", "api-2"),
        ("/home/ada/old/a.p.i", "a-p-i"),
    ]


def test_a_folder_called_openshrimp_cannot_take_the_reserved_name(
    tmp_path, monkeypatch
):
    """The supervisor is built in code; an import must not collide with it."""
    _write_claude_json(tmp_path, monkeypatch, {"/home/ada/openshrimp": _real()})

    assert [p.context_name for p in discover_claude_projects()] == ["openshrimp-2"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("open-shrimp", "open-shrimp"),
        ("my_project", "my_project"),
        ("talenthub.glints.com", "talenthub-glints-com"),
        ("  spaced  out  ", "spaced-out"),
        ("--leading--and--trailing--", "leading-and-trailing"),
        ("", "project"),
        (".", "project"),
        ("2026", "2026"),
        # isalnum() is Unicode-aware and the validator uses it, so a name the
        # validator would accept is not mangled for looking foreign.
        ("café", "café"),
    ],
)
def test_the_sanitised_name_is_one_the_validator_accepts(raw, expected):
    from open_shrimp.config import _validate_context_name, sanitise_context_name

    assert sanitise_context_name(raw) == expected
    assert _validate_context_name(expected) is None


def test_a_taken_name_takes_the_next_number():
    from open_shrimp.config import unique_context_name

    taken: set[str] = set()
    for expected in ("api", "api-2", "api-3"):
        name = unique_context_name("api", taken)
        assert name == expected
        taken.add(name)


def test_an_empty_result_is_still_valid_json(tmp_path, monkeypatch, capsys):
    """So a GUI can render "none found" rather than parse a failure."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))

    assert _run_projects_discover(json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == {"projects": []}


# ---------------------------------------------------------------------------
# Naming one folder
#
# A wizard with a folder picker has the same question about the folder the
# user chose, and no way to answer it: the rule for a legal context name lives
# in Python.  Answering it here is what stops a GUI naming a folder it picked
# differently from the same folder found by discovery.
# ---------------------------------------------------------------------------


def _named_rows(capsys, *paths: str, taken: list[str] | None = None) -> list[dict]:
    assert (
        _run_projects_name(
            paths=list(paths), taken=list(taken or []), json_output=True
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)["projects"]


def _named(capsys, path: str, taken: list[str] | None = None) -> dict:
    rows = _named_rows(capsys, path, taken=taken)
    assert len(rows) == 1
    return rows[0]


def test_naming_answers_in_the_same_shape_as_discovery(tmp_path, monkeypatch, capsys):
    """So both GUIs decode it with the decoder they already have."""
    _write_claude_json(
        tmp_path, monkeypatch, {"/home/ada/real": _real(lastStartTime=17)}
    )
    assert _run_projects_discover(json_output=True) == 0
    discovered = json.loads(capsys.readouterr().out)["projects"][0]

    assert _named(capsys, "/home/ada/real").keys() == discovered.keys()


def test_a_hand_picked_folder_is_named_by_the_same_rule(capsys):
    """The rule discovery applies, applied to a folder the picker found."""
    assert _named(capsys, "/home/ada/work/talenthub.glints.com") == {
        "directory": "/home/ada/work/talenthub.glints.com",
        "name": "talenthub.glints.com",
        "context_name": "talenthub-glints-com",
        "last_start_time": None,
    }


def test_the_names_the_caller_already_used_are_avoided(capsys):
    """Uniqueness is a property of the list being built, and only the caller
    holding that list knows what is in it."""
    assert (
        _named(capsys, "/home/ada/api", taken=["api", "api-2"])["context_name"]
        == "api-3"
    )


def test_a_taken_name_is_one_flag_each(capsys):
    """Repeated rather than comma-joined: the names come from editable boxes,
    so a comma in one would otherwise split it into two names and leave the
    real one unclaimed."""
    assert _named(capsys, "/home/ada/api", taken=["api,api-2"])["context_name"] == "api"


def test_the_reserved_name_is_refused_here_too(capsys):
    assert _named(capsys, "/home/ada/openshrimp")["context_name"] == "openshrimp-2"


def test_a_unicode_folder_name_survives(capsys):
    """The validator is Unicode-aware, so a name it would accept must not be
    mangled for looking foreign — and no front end may narrow it."""
    assert _named(capsys, "/home/ada/café")["context_name"] == "café"


def test_an_empty_taken_list_names_nothing_taken(capsys):
    """A GUI adding its first row sends no names, which must not read as one
    empty name already spoken for."""
    assert _named(capsys, "/home/ada/api")["context_name"] == "api"


def test_one_folder_gets_one_name_however_the_path_is_spelled(
    tmp_path, monkeypatch, capsys
):
    """Otherwise the name a folder is given is decided by how the caller typed
    the path, and a picker that hands over a relative one names it '.'."""
    folder = tmp_path / "namecheck"
    folder.mkdir()
    monkeypatch.chdir(folder)

    (tmp_path / "other").mkdir()
    spellings = [
        str(folder),
        ".",
        str(tmp_path / "other" / ".." / "namecheck"),
    ]

    answers = [_named(capsys, spelling) for spelling in spellings]
    assert {json.dumps(answer, sort_keys=True) for answer in answers} == {
        json.dumps(
            {
                "directory": str(folder.resolve()),
                "name": "namecheck",
                "context_name": "namecheck",
                "last_start_time": None,
            },
            sort_keys=True,
        )
    }


def test_a_folder_that_is_gone_is_still_named_as_typed(capsys):
    """Naming is not validation — the config write is where a missing
    directory is refused — so the answer echoes the path the user can still
    correct rather than a resolved one they never typed."""
    assert _named(capsys, "~/nowhere-at-all") == {
        "directory": str(Path("~/nowhere-at-all").expanduser()),
        "name": "nowhere-at-all",
        "context_name": "nowhere-at-all",
        "last_start_time": None,
    }


def test_a_whole_selection_is_named_in_one_call(capsys):
    """A picker that allows several folders pays one spawn, and their
    uniqueness against each other is settled here rather than by the caller
    looping and re-sending what it was just told."""
    rows = _named_rows(capsys, "/home/ada/work/api", "/home/ada/play/api")
    assert [row["context_name"] for row in rows] == ["api", "api-2"]


def test_the_text_listing_names_the_folder_as_well_as_the_context(capsys):
    """The two are different answers: one is what the user will type after
    /context, the other is what they can match against their filesystem."""
    assert (
        _run_projects_name(
            paths=["/home/ada/work/talenthub.glints.com"], taken=[], json_output=False
        )
        == 0
    )
    line = capsys.readouterr().out.strip()
    assert line.split() == [
        "talenthub-glints-com",
        "talenthub.glints.com",
        "/home/ada/work/talenthub.glints.com",
    ]


def test_the_text_listing_of_a_discovery_names_both_too(tmp_path, monkeypatch, capsys):
    """Same listing, same shape, whichever command produced the rows."""
    _write_claude_json(
        tmp_path, monkeypatch, {"/home/ada/work/talenthub.glints.com": _real()}
    )
    assert _run_projects_discover(json_output=False) == 0
    assert capsys.readouterr().out.split() == [
        "talenthub-glints-com",
        "talenthub.glints.com",
        "/home/ada/work/talenthub.glints.com",
    ]
