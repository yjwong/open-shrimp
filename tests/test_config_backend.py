"""Config plumbing for the top-level ``backend`` key and the per-context
``contexts.<name>.backend`` override."""

from __future__ import annotations

import pytest

from open_shrimp.config import (
    _parse,
    _validate_raw,
    config_to_dict,
    effective_backend,
)


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


# ── per-context backend override ──


def _base_raw_with_context_backend(ctx_backend: str | None, **extra):
    raw = _base_raw(**extra)
    if ctx_backend is not None:
        raw["contexts"]["default"]["backend"] = ctx_backend
    return raw


def test_context_backend_absent_defaults_to_none():
    cfg = _parse(_base_raw())
    assert cfg.contexts["default"].backend is None


def test_context_backend_round_trips():
    raw = _base_raw_with_context_backend("claude_sdk")
    cfg = _parse(raw)
    assert cfg.contexts["default"].backend == "claude_sdk"
    serialized = config_to_dict(cfg)
    assert serialized["contexts"]["default"]["backend"] == "claude_sdk"


def test_context_backend_unknown_fails_validation():
    raw = _base_raw_with_context_backend("nope")
    with pytest.raises(ValueError, match="Context 'default': backend must be one of"):
        _validate_raw(raw)


def test_context_backend_known_passes_validation():
    raw = _base_raw_with_context_backend("claude_sdk")
    _validate_raw(raw)  # no raise


def test_effective_backend_inherits_top_level():
    cfg = _parse(_base_raw())
    assert effective_backend(cfg.contexts["default"], cfg) == "claude_sdk"


def test_effective_backend_uses_override():
    raw = _base_raw_with_context_backend("claude_sdk")
    cfg = _parse(raw)
    # The override is explicit even though it matches the top-level default.
    assert effective_backend(cfg.contexts["default"], cfg) == "claude_sdk"


def test_default_context_backend_omitted_from_serialized_dict():
    cfg = _parse(_base_raw())
    serialized = config_to_dict(cfg)
    assert "backend" not in serialized["contexts"]["default"]


# ── per-context OpenCode validation ──


@pytest.fixture
def _stub_opencode_binary(monkeypatch: pytest.MonkeyPatch):
    """Stub the opencode binary discovery so validation can reach the rest."""
    import open_shrimp.sandbox.opencode_runtime as runtime

    monkeypatch.setattr(runtime, "_find_opencode_binary", lambda: "/fake/opencode")


def _opencode_raw(**ctx_extra):
    raw = _base_raw()
    raw["contexts"]["default"].update(ctx_extra)
    return raw


def test_opencode_per_context_requires_provider_qualified_model(
    _stub_opencode_binary,
):
    """A bare ``model:`` value rejects only on opencode-backed contexts."""
    raw = _opencode_raw(backend="opencode", model="gpt-5.5")
    with pytest.raises(ValueError, match="provider-qualified model"):
        _validate_raw(raw)


def test_opencode_per_context_accepts_provider_qualified_model(
    _stub_opencode_binary,
):
    raw = _opencode_raw(backend="opencode", model="openai/gpt-5.5")
    _validate_raw(raw)  # no raise


def test_claude_context_accepts_bare_model_when_opencode_is_another_context(
    _stub_opencode_binary,
):
    """OpenCode model rules apply only to opencode-backed contexts."""
    raw = _base_raw()
    raw["contexts"]["claude_ctx"] = {
        "directory": "/tmp/c",
        "description": "claude",
        "allowed_tools": [],
        # No backend override — inherits default (claude_sdk).
        "model": "claude-sonnet-4-6",  # bare name, fine for claude_sdk.
    }
    raw["contexts"]["opencode_ctx"] = {
        "directory": "/tmp/o",
        "description": "opencode",
        "allowed_tools": [],
        "backend": "opencode",
        "model": "openai/gpt-5.5",
    }
    _validate_raw(raw)  # no raise


def test_opencode_top_level_still_validates_each_context(_stub_opencode_binary):
    """When the top-level backend is opencode, every context is checked."""
    raw = _base_raw(backend="opencode")
    raw["contexts"]["default"]["model"] = "openai/gpt-5.5"
    raw["contexts"]["bad"] = {
        "directory": "/tmp/b",
        "description": "bad",
        "allowed_tools": [],
        "model": "gpt-5.5",  # missing provider — should fail.
    }
    with pytest.raises(ValueError, match="provider-qualified model"):
        _validate_raw(raw)


def test_opencode_per_context_rejects_computer_use(_stub_opencode_binary):
    raw = _opencode_raw(
        backend="opencode",
        model="openai/gpt-5.5",
        sandbox={"backend": "docker", "computer_use": True},
    )
    with pytest.raises(ValueError, match="computer_use is not supported"):
        _validate_raw(raw)


def test_claude_context_allows_computer_use_when_other_context_is_opencode(
    _stub_opencode_binary,
):
    """The computer_use ban only fires for opencode-backed contexts."""
    raw = _base_raw()
    raw["contexts"]["claude_with_gui"] = {
        "directory": "/tmp/c",
        "description": "gui",
        "allowed_tools": [],
        "sandbox": {"backend": "docker", "computer_use": True},
    }
    raw["contexts"]["opencode_ctx"] = {
        "directory": "/tmp/o",
        "description": "o",
        "allowed_tools": [],
        "backend": "opencode",
        "model": "openai/gpt-5.5",
    }
    _validate_raw(raw)  # no raise
