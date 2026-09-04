"""A config reload that fails must not fail silently.

The watcher used to log and stop there.  That was survivable while every
edit came from a person at a keyboard who could read the log; it is not
survivable once something else writes the file and would otherwise be
told the write took effect when the running config never changed.

Two cases are reported: the file did not load at all, and it loaded but
changed a field that only a restart applies.  The third case — the file
loaded and something downstream of it failed — is deliberately not
reported, because by then the new config is live and the report's
promise that nothing changed would be false.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.rich_stub import wire_rich
import yaml

from open_shrimp.bot import _reload_failure_text, _watch_config
from open_shrimp.config import Config, ConfigParseError, load_config

GOOD_CONFIG: dict[str, Any] = {
    "telegram": {"token": "1:aaa"},
    "allowed_users": [11, 22],
    "default_context": "work",
    "contexts": {
        "work": {
            "directory": "/tmp",
            "description": "a project",
            "allowed_tools": ["LSP"],
        },
    },
}


def _bot(**kwargs):
    bot = type("_Bot", (), {"send_message": AsyncMock(**kwargs)})()
    return wire_rich(bot)


def _texts(bot) -> list[str]:
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]


def _recipients(bot) -> list[int]:
    return [call.kwargs["chat_id"] for call in bot.send_message.await_args_list]


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(GOOD_CONFIG), encoding="utf-8")
    return path


@pytest.fixture
def loaded(config_file: Path) -> Config:
    return load_config(str(config_file))


def _save(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


async def _run_watcher(
    monkeypatch, path: Path, bot, config: Config, rounds: int = 1
) -> dict[str, Any]:
    """Drive *rounds* iterations of the watcher's loop over *path*.

    ``awatch`` is replaced rather than waited on: the test is about what
    the loop body does with a change, not about inotify.
    """

    async def fake_awatch(_path, **_kwargs):
        for _ in range(rounds):
            yield {("modified", str(path))}

    import watchfiles

    monkeypatch.setattr(watchfiles, "awatch", fake_awatch)
    bot_data: dict[str, Any] = {"config": config}
    await _watch_config(str(path), bot_data, bot)
    return bot_data


# ---------------------------------------------------------------------------
# The parse error itself
# ---------------------------------------------------------------------------


def test_a_broken_file_names_the_line_and_not_the_parser(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("telegram:\n  token: 1:aaa\n bad-indent: yes\n", "utf-8")

    with pytest.raises(ConfigParseError) as excinfo:
        load_config(str(path))

    message = str(excinfo.value)
    assert "is not valid YAML" in message
    assert "line 3" in message
    assert "Traceback" not in message


def test_a_valid_file_still_loads(loaded: Config):
    assert loaded.default_context == "work"


def test_a_field_of_the_wrong_shape_is_a_bad_edit_not_a_crash(
    tmp_path: Path,
):
    # ``_validate_raw`` does not type-check ``review``, so a mapping
    # turned into a list used to reach ``_parse`` and raise a bare
    # AttributeError, which reads to every caller as an OpenShrimp bug.
    path = tmp_path / "config.yaml"
    _save(path, {**GOOD_CONFIG, "review": []})

    with pytest.raises(ConfigParseError) as excinfo:
        load_config(str(path))

    assert "could not read it" in str(excinfo.value)


def test_a_parse_error_is_a_value_error():
    # Callers that already tell a bad config from a missing one catch
    # ValueError; a new exception type must not walk past them.
    assert issubclass(ConfigParseError, ValueError)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_reload_reaches_telegram(
    monkeypatch, config_file: Path, loaded: Config
):
    config_file.write_text("contexts:\n  work:\n   directory: /tmp\n  bad\n")

    bot = _bot()
    await _run_watcher(monkeypatch, config_file, bot, loaded)

    assert _recipients(bot) == [11, 22]
    text = _texts(bot)[0]
    assert "could not load" in text
    assert "not valid YAML" in text
    assert "still running on the last config" in text


@pytest.mark.asyncio
async def test_a_validation_failure_reaches_telegram_with_its_own_words(
    monkeypatch, config_file: Path, loaded: Config
):
    _save(config_file, {**GOOD_CONFIG, "contexts": {"work": {"directory": "/tmp"}}})

    bot = _bot()
    await _run_watcher(monkeypatch, config_file, bot, loaded)

    assert "missing required field: description" in _texts(bot)[0]


@pytest.mark.asyncio
async def test_a_deleted_file_says_so_rather_than_blaming_a_bug(
    monkeypatch, config_file: Path, loaded: Config
):
    config_file.unlink()

    bot = _bot()
    await _run_watcher(monkeypatch, config_file, bot, loaded)

    assert "not there any more" in _texts(bot)[0]


@pytest.mark.asyncio
async def test_a_successful_reload_says_nothing(
    monkeypatch, config_file: Path, loaded: Config
):
    _save(
        config_file,
        {
            **GOOD_CONFIG,
            "contexts": {
                **GOOD_CONFIG["contexts"],
                "other": {
                    "directory": "/tmp",
                    "description": "another",
                    "allowed_tools": [],
                },
            },
        },
    )

    bot = _bot()
    bot_data = await _run_watcher(monkeypatch, config_file, bot, loaded)

    assert _texts(bot) == []
    assert "other" in bot_data["config"].contexts


@pytest.mark.asyncio
async def test_a_restart_only_field_is_reported(
    monkeypatch, config_file: Path, loaded: Config
):
    _save(config_file, {**GOOD_CONFIG, "telegram": {"token": "2:bbb"}})

    bot = _bot()
    await _run_watcher(monkeypatch, config_file, bot, loaded)

    text = _texts(bot)[0]
    assert "Telegram token" in text
    assert "restart" in text


@pytest.mark.asyncio
async def test_the_new_config_is_live_before_the_report_is_sent(
    monkeypatch, config_file: Path, loaded: Config
):
    # A stalled Telegram send must not hold the reloaded config back from
    # the handlers waiting on it, so the install comes first.
    seen: list[str] = []
    _save(config_file, {**GOOD_CONFIG, "telegram": {"token": "2:bbb"}})

    async def fake_awatch(_path, **_kwargs):
        yield {("modified", str(config_file))}

    import watchfiles

    monkeypatch.setattr(watchfiles, "awatch", fake_awatch)
    bot_data: dict[str, Any] = {"config": loaded}

    async def record(**kwargs):
        seen.append(bot_data["config"].telegram.token)

    bot = _bot(side_effect=record)
    await _watch_config(str(config_file), bot_data, bot)

    assert seen == ["2:bbb", "2:bbb"]


@pytest.mark.asyncio
async def test_a_blocked_operator_costs_nobody_else_their_message(
    monkeypatch, config_file: Path, loaded: Config
):
    config_file.write_text("{{{", encoding="utf-8")
    bot = _bot(side_effect=RuntimeError("blocked by the user"))

    # Two iterations: a send that raises must neither skip the second
    # recipient nor end the watcher.
    await _run_watcher(monkeypatch, config_file, bot, loaded, rounds=2)

    assert _recipients(bot) == [11, 22, 11, 22]


def test_an_unexpected_failure_offers_no_half_parsed_detail():
    text = _reload_failure_text(OSError("Permission denied: /etc/shadow"))
    assert "/etc/shadow" not in text
    assert "could not be read" in text
