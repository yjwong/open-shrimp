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
    cfg = _parse(_base_raw(backend="opencode"))
    assert cfg.backend == "opencode"


def test_unknown_backend_fails_validation():
    with pytest.raises(ValueError, match="backend must be one of"):
        _validate_raw(_base_raw(backend="nope"))


def test_valid_backend_passes_validation():
    _validate_raw(_base_raw(backend="claude_sdk"))  # no raise


def test_default_backend_omitted_from_serialized_dict():
    cfg = _parse(_base_raw())
    assert "backend" not in config_to_dict(cfg)


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


def test_effective_backend_inherits_top_level():
    cfg = _parse(_base_raw())
    assert effective_backend(cfg.contexts["default"], cfg) == "claude_sdk"


def test_effective_backend_uses_override():
    raw = _base_raw_with_context_backend("opencode", backend="claude_sdk")
    # OpenCode-backed contexts require a provider-qualified model.
    raw["contexts"]["default"]["model"] = "openai/gpt-5.5"
    cfg = _parse(raw)
    assert effective_backend(cfg.contexts["default"], cfg) == "opencode"


def test_default_context_backend_omitted_from_serialized_dict():
    cfg = _parse(_base_raw())
    serialized = config_to_dict(cfg)
    assert "backend" not in serialized["contexts"]["default"]


# ── per-context OpenCode validation ──


@pytest.fixture
def _opencode_build_published(monkeypatch: pytest.MonkeyPatch):
    """Pin the platform answer so these run the same on every host."""
    import open_shrimp.backend.opencode.release as release

    monkeypatch.setattr(release, "no_build_reason", lambda: None)


def _opencode_raw(**ctx_extra):
    raw = _base_raw()
    raw["contexts"]["default"].update(ctx_extra)
    return raw


def test_opencode_per_context_requires_provider_qualified_model(
    _opencode_build_published,
):
    """A bare ``model:`` value rejects only on opencode-backed contexts."""
    raw = _opencode_raw(backend="opencode", model="gpt-5.5")
    with pytest.raises(ValueError, match="provider-qualified model"):
        _validate_raw(raw)


def test_opencode_per_context_accepts_provider_qualified_model(
    _opencode_build_published,
):
    raw = _opencode_raw(backend="opencode", model="openai/gpt-5.5")
    _validate_raw(raw)  # no raise


def test_claude_context_accepts_bare_model_when_opencode_is_another_context(
    _opencode_build_published,
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


def test_opencode_top_level_still_validates_each_context(_opencode_build_published):
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


def test_opencode_per_context_allows_computer_use(_opencode_build_published):
    """OpenCode + computer_use validates: the guest desktop is agent-neutral."""
    raw = _opencode_raw(
        backend="opencode",
        model="openai/gpt-5.5",
        sandbox={"backend": "libvirt", "computer_use": True},
    )
    _validate_raw(raw)  # no raise


def test_claude_context_allows_computer_use_when_other_context_is_opencode(
    _opencode_build_published,
):
    """Both Claude- and OpenCode-backed contexts may enable computer_use."""
    raw = _base_raw()
    raw["contexts"]["claude_with_gui"] = {
        "directory": "/tmp/c",
        "description": "gui",
        "allowed_tools": [],
        "sandbox": {"backend": "libvirt", "computer_use": True},
    }
    raw["contexts"]["opencode_ctx"] = {
        "directory": "/tmp/o",
        "description": "o",
        "allowed_tools": [],
        "backend": "opencode",
        "model": "openai/gpt-5.5",
    }
    _validate_raw(raw)  # no raise


def test_opencode_validates_with_no_binary_on_disk(
    monkeypatch: pytest.MonkeyPatch,
):
    """The CLI is fetched on first use, so its absence at startup is not a
    config error — refusing to boot over it would tell the user to install
    something that was about to arrive on its own.

    The empty ``BIN_DIR`` is stubbed rather than assumed so that re-adding a
    binary lookup to validation fails here instead of passing on whatever the
    developer's machine happens to have downloaded."""
    import open_shrimp.backend.opencode.binary as binary
    import open_shrimp.backend.opencode.release as release

    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(binary, "managed_binary", lambda name: None)
    monkeypatch.setattr(release, "no_build_reason", lambda: None)

    _validate_raw(_opencode_raw(backend="opencode", model="openai/gpt-5.5"))


def test_opencode_is_refused_where_no_build_is_published(
    monkeypatch: pytest.MonkeyPatch,
):
    """There the fetch has nothing to fetch and no later moment recovers it."""
    import open_shrimp.backend.opencode.release as release

    monkeypatch.setattr(
        release, "no_build_reason",
        lambda: "no opencode build is published for Linux ppc64le",
    )

    raw = _opencode_raw(backend="opencode", model="openai/gpt-5.5")
    with pytest.raises(ValueError, match="no opencode build is published"):
        _validate_raw(raw)


# ── what a wizard writes ──


def test_a_wizard_pinning_an_opencode_model_writes_a_context_that_resolves_to_it(
    _opencode_build_published,
):
    """The whole point of deriving the backend in ``build_context_dict``: a
    first config naming ``openai/gpt-5.6-sol`` has to reach the turn through
    OpenCode, and the only thing between the wizard and that turn is this."""
    from open_shrimp.config import build_context_dict

    raw = _base_raw()
    raw["contexts"] = {
        "gpt": build_context_dict("/tmp", "gpt", "openai/gpt-5.6-sol"),
        "default": build_context_dict("/tmp", "claude", "sonnet"),
    }

    _validate_raw(raw)
    cfg = _parse(raw)

    assert effective_backend(cfg.contexts["gpt"], cfg) == "opencode"
    assert effective_backend(cfg.contexts["default"], cfg) == "claude_sdk"


def test_a_claude_only_config_carries_no_backend_key_at_all():
    """Byte-identical to what the wizard wrote before any of this existed, so
    the derivation costs nothing on the configs that do not need it."""
    from open_shrimp.config import build_context_dict

    assert "backend" not in build_context_dict("/tmp", "d", "sonnet")
    assert "backend" not in build_context_dict("/tmp", "d", None)
