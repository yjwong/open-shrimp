"""Config plumbing for the top-level ``backend`` key (step 3)."""

from __future__ import annotations

import pytest

from open_shrimp.config import _parse, _validate_raw, config_to_dict


def _base_raw(**extra):
    raw = {
        "telegram": {"token": "t"},
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
    raw.update(extra)
    return raw


def test_absent_backend_defaults_to_claude_sdk():
    cfg = _parse(_base_raw())
    assert cfg.backend == "claude_sdk"


def test_explicit_backend_parsed():
    cfg = _parse(_base_raw(backend="claude_sdk"))
    assert cfg.backend == "claude_sdk"


def test_unknown_backend_fails_validation():
    with pytest.raises(ValueError, match="backend must be one of"):
        _validate_raw(_base_raw(backend="nope"))


def test_valid_backend_passes_validation():
    _validate_raw(_base_raw(backend="claude_sdk"))  # no raise


def test_default_backend_omitted_from_serialized_dict():
    cfg = _parse(_base_raw())
    assert "backend" not in config_to_dict(cfg)


def test_non_default_backend_round_trips():
    # No second backend is registered yet, so claude_sdk is the only valid
    # value; assert the serializer's omit-when-default rule rather than a
    # round-trip of a non-default name.
    cfg = _parse(_base_raw(backend="claude_sdk"))
    assert config_to_dict(cfg).get("backend") is None
