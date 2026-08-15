"""The non-interactive CLI surface a setup UI outside Python drives.

The terminal wizard needs a tty and the config HTTP API needs a running bot,
so neither can serve a first run launched from a GUI.  These commands are
that path, and they must stay machine-parsable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from open_shrimp.config import load_config
from open_shrimp.main import _parse_args, _run_config_write, _run_models


# -- Argument placement ------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "/A", "install"],
        ["install", "--config", "/A"],
        ["--config", "/A", "doctor"],
        ["doctor", "--config", "/A"],
        ["--config", "/A", "uninstall"],
        ["--config", "/A", "update"],
        ["--config", "/A", "config", "write"],
        ["config", "write", "--config", "/A"],
    ],
)
def test_config_flag_is_honoured_on_either_side_of_the_subcommand(argv, monkeypatch):
    """--config used to be silently dropped or rejected depending on position."""
    monkeypatch.setattr(sys, "argv", ["openshrimp", *argv])
    assert _parse_args().config == "/A"


# -- models ------------------------------------------------------------------


def test_models_json_is_parsable(capsys, tmp_path):
    # No config: falls back to the default backend's catalog, which is the
    # first-run case a setup UI hits.
    assert _run_models(json_output=True, config_path=str(tmp_path / "none.yaml")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["models"]
    for entry in payload["models"]:
        assert set(entry) == {"alias", "model_id", "description"}


def test_models_follows_the_configured_backend(capsys, tmp_path, monkeypatch):
    """OpenCode wants provider-qualified ids, so offering Claude aliases there
    would let a setup UI pin a model the backend rejects on every turn."""
    from open_shrimp.backend.protocol import ModelChoice

    class _Backend:
        def model_catalog(self):
            return [ModelChoice("gpt", "openai/gpt-5.5", "provider-qualified")]

    config = tmp_path / "config.yaml"
    config.write_text("telegram:\n  token: x\n")
    monkeypatch.setattr("open_shrimp.config.load_config", lambda p: object())
    monkeypatch.setattr("open_shrimp.backend.get_backend", lambda c: _Backend())

    assert _run_models(json_output=True, config_path=str(config)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["model_id"] for m in payload["models"]] == ["openai/gpt-5.5"]


# -- config write ------------------------------------------------------------


class _Args:
    def __init__(self, config: Path, json: str = "-", force: bool = False):
        self.config = str(config)
        self.json = json
        self.force = force


def _spec(directory: Path, **overrides) -> dict:
    spec = {
        "token": "123456:ABCdefGHIjklMNOpqrs",
        "user_id": 42,
        "context_name": "default",
        "directory": str(directory),
        "description": "written by a setup UI",
    }
    spec.update(overrides)
    return spec


def _write(tmp_path, monkeypatch, capsys, spec, **kwargs) -> tuple[int, dict]:
    monkeypatch.setattr(
        "sys.stdin", __import__("io").StringIO(json.dumps(spec) if spec is not None else "not json")
    )
    code = _run_config_write(_Args(tmp_path / "config.yaml", **kwargs))
    return code, json.loads(capsys.readouterr().out)


def test_written_config_loads(tmp_path, monkeypatch, capsys):
    code, out = _write(tmp_path, monkeypatch, capsys, _spec(tmp_path, model="sonnet"))

    assert code == 0 and out["ok"] is True

    config = load_config(out["config_path"])
    assert config.default_context == "default"
    assert config.allowed_users == [42]
    assert config.contexts["default"].directory == str(tmp_path)


def test_model_is_optional(tmp_path, monkeypatch, capsys):
    code, out = _write(tmp_path, monkeypatch, capsys, _spec(tmp_path))
    assert code == 0
    assert load_config(out["config_path"]).contexts["default"].model is None


@pytest.mark.parametrize(
    "spec_overrides, expected",
    [
        ({"user_id": None}, "missing required field"),
        ({"directory": ""}, "missing required field"),
        ({"user_id": "abc"}, "user_id must be a number"),
    ],
)
def test_bad_input_reports_json_not_a_traceback(
    tmp_path, monkeypatch, capsys, spec_overrides, expected
):
    spec = _spec(tmp_path)
    spec.update(spec_overrides)
    code, out = _write(tmp_path, monkeypatch, capsys, spec)
    assert code == 1
    assert out["ok"] is False
    assert expected in out["error"]


def test_missing_directory_is_rejected(tmp_path, monkeypatch, capsys):
    """The terminal wizard validates this; the CLI that replaces it must too."""
    spec = _spec(tmp_path / "does-not-exist")
    code, out = _write(tmp_path, monkeypatch, capsys, spec)

    assert code == 1
    assert "directory does not exist" in out["error"]
    assert not (tmp_path / "config.yaml").exists()


def test_malformed_json_reports_json(tmp_path, monkeypatch, capsys):
    code, out = _write(tmp_path, monkeypatch, capsys, None)
    assert code == 1
    assert "invalid JSON" in out["error"]


def test_existing_config_is_not_clobbered(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text("telegram:\n  token: keep-me\n")

    code, out = _write(tmp_path, monkeypatch, capsys, _spec(tmp_path))

    assert code == 1
    assert "--force" in out["error"]
    assert "keep-me" in (tmp_path / "config.yaml").read_text()


def test_force_overwrites(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text("telegram:\n  token: replace-me\n")

    code, _ = _write(tmp_path, monkeypatch, capsys, _spec(tmp_path), force=True)

    assert code == 0
    assert "replace-me" not in (tmp_path / "config.yaml").read_text()


def test_reads_from_a_file_not_only_stdin(tmp_path, capsys):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(_spec(tmp_path)))

    code = _run_config_write(_Args(tmp_path / "config.yaml", json=str(spec_file)))

    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
