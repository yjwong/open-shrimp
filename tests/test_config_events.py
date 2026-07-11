"""Validation and parsing of the top-level ``events:`` config section."""

from __future__ import annotations

import copy

import pytest

from open_shrimp.config import EventsConfig, _parse, _validate_raw

MAIN_TOKEN = "123:main-token"

_VALID_EVENTS = {
    "chat_id": -1001234567890,
    "sources": [
        {
            "name": "tg-intake",
            "type": "telegram",
            "token": "456:intake-token",
            "allowed_chats": [-100987654321, 42424242],
        },
    ],
}


def _base_raw(events=None):
    raw = {
        "telegram": {"token": MAIN_TOKEN},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
            }
        },
        "default_context": "default",
    }
    if events is not None:
        raw["events"] = copy.deepcopy(events)
    return raw


def test_config_without_events_section_is_valid():
    raw = _base_raw()
    _validate_raw(raw)  # no raise
    assert _parse(raw).events is None


def test_valid_events_config_parses():
    raw = _base_raw(_VALID_EVENTS)
    _validate_raw(raw)  # no raise
    cfg = _parse(raw)
    assert isinstance(cfg.events, EventsConfig)
    assert cfg.events.chat_id == -1001234567890
    assert len(cfg.events.sources) == 1
    src = cfg.events.sources[0]
    assert src.name == "tg-intake"
    assert src.type == "telegram"
    assert src.token == "456:intake-token"
    assert src.allowed_chats == [-100987654321, 42424242]


def test_lark_source_parses():
    events = {
        "chat_id": 1,
        "sources": [
            {"name": "lark", "type": "lark", "app_id": "cli_x", "app_secret": "s"},
        ],
    }
    cfg = _parse(_base_raw(events))
    src = cfg.events.sources[0]
    assert src.type == "lark"
    assert src.app_id == "cli_x"
    assert src.app_secret == "s"


def test_missing_chat_id_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    del events["chat_id"]
    with pytest.raises(ValueError, match="chat_id"):
        _validate_raw(_base_raw(events))


def test_duplicate_source_names_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"].append(copy.deepcopy(events["sources"][0]))
    with pytest.raises(ValueError, match="duplicate source name"):
        _validate_raw(_base_raw(events))


def test_telegram_source_without_token_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    del events["sources"][0]["token"]
    with pytest.raises(ValueError, match="requires a 'token'"):
        _validate_raw(_base_raw(events))


def test_telegram_source_without_allowed_chats_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    del events["sources"][0]["allowed_chats"]
    with pytest.raises(ValueError, match="allowed_chats"):
        _validate_raw(_base_raw(events))


def test_telegram_source_with_empty_allowed_chats_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"][0]["allowed_chats"] = []
    with pytest.raises(ValueError, match="allowed_chats"):
        _validate_raw(_base_raw(events))


def test_source_token_equal_to_main_bot_token_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"][0]["token"] = MAIN_TOKEN
    with pytest.raises(ValueError, match="must not be the main"):
        _validate_raw(_base_raw(events))


def test_lark_source_missing_app_id_rejected():
    events = {
        "chat_id": 1,
        "sources": [{"name": "lark", "type": "lark", "app_secret": "s"}],
    }
    with pytest.raises(ValueError, match="app_id"):
        _validate_raw(_base_raw(events))


def test_unknown_source_type_rejected():
    events = {
        "chat_id": 1,
        "sources": [{"name": "x", "type": "whatsapp"}],
    }
    with pytest.raises(ValueError, match="type must be one of"):
        _validate_raw(_base_raw(events))


def test_multiline_source_name_rejected():
    events = {
        "chat_id": 1,
        "sources": [
            {
                "name": "a\nb",
                "type": "telegram",
                "token": "456:t",
                "allowed_chats": [1],
            }
        ],
    }
    with pytest.raises(ValueError, match="single line"):
        _validate_raw(_base_raw(events))


# ── Pick-up fields (context / pickup) ──


def test_context_and_pickup_default_when_absent():
    cfg = _parse(_base_raw(_VALID_EVENTS))
    src = cfg.events.sources[0]
    assert src.context is None
    assert src.pickup is True


def test_valid_context_parses():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"][0]["context"] = "default"
    raw = _base_raw(events)
    _validate_raw(raw)  # no raise
    assert _parse(raw).events.sources[0].context == "default"


def test_undefined_context_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"][0]["context"] = "nope"
    with pytest.raises(ValueError, match="not a defined context"):
        _validate_raw(_base_raw(events))


def test_pickup_false_parses():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"][0]["pickup"] = False
    raw = _base_raw(events)
    _validate_raw(raw)  # no raise
    assert _parse(raw).events.sources[0].pickup is False


def test_non_boolean_pickup_rejected():
    events = copy.deepcopy(_VALID_EVENTS)
    events["sources"][0]["pickup"] = "yes"
    with pytest.raises(ValueError, match="pickup must be a boolean"):
        _validate_raw(_base_raw(events))


# ── Lark domain (feishu / lark international) ──


def _lark_events(domain=None):
    src = {"name": "lark", "type": "lark", "app_id": "cli_x", "app_secret": "s"}
    if domain is not None:
        src["domain"] = domain
    return {"chat_id": 1, "sources": [src]}


def test_lark_domain_absent_parses_as_none():
    cfg = _parse(_base_raw(_lark_events()))
    assert cfg.events.sources[0].domain is None


def test_lark_domain_lark_parses():
    raw = _base_raw(_lark_events(domain="lark"))
    _validate_raw(raw)  # no raise
    assert _parse(raw).events.sources[0].domain == "lark"


def test_lark_domain_feishu_parses():
    raw = _base_raw(_lark_events(domain="feishu"))
    _validate_raw(raw)  # no raise
    assert _parse(raw).events.sources[0].domain == "feishu"


def test_lark_domain_invalid_rejected():
    with pytest.raises(ValueError, match="domain must be"):
        _validate_raw(_base_raw(_lark_events(domain="larksuite")))
