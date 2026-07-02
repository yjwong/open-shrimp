"""``_patch_raw_yaml`` round-trips the editable top-level keys, including the
new ``backend`` selector, while leaving everything else untouched."""

from __future__ import annotations

from open_shrimp.config import _validate_raw
from open_shrimp.config_app.api import _patch_raw_yaml


def _base_raw() -> dict:
    return {
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


def test_top_level_backend_set():
    raw = _base_raw()
    _patch_raw_yaml(raw, {"backend": "opencode"})
    assert raw["backend"] == "opencode"


def test_top_level_backend_default_removes_key():
    raw = _base_raw()
    raw["backend"] = "opencode"
    _patch_raw_yaml(raw, {"backend": "claude_sdk"})
    assert "backend" not in raw


def test_top_level_backend_null_removes_key():
    raw = _base_raw()
    raw["backend"] = "opencode"
    _patch_raw_yaml(raw, {"backend": None})
    assert "backend" not in raw


def test_top_level_backend_empty_removes_key():
    raw = _base_raw()
    raw["backend"] = "opencode"
    _patch_raw_yaml(raw, {"backend": ""})
    assert "backend" not in raw


def test_top_level_backend_absent_leaves_untouched():
    raw = _base_raw()
    raw["backend"] = "opencode"
    _patch_raw_yaml(raw, {"allowed_users": [2]})
    assert raw["backend"] == "opencode"
    assert raw["allowed_users"] == [2]


def test_per_context_backend_round_trips_through_contexts():
    raw = _base_raw()
    body = {
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
                "backend": "opencode",
                "model": "openai/gpt-5.5",
            }
        }
    }
    _patch_raw_yaml(raw, body)
    assert raw["contexts"]["default"]["backend"] == "opencode"
    assert raw["contexts"]["default"]["model"] == "openai/gpt-5.5"


def test_phone_use_context_patches_and_validates():
    raw = _base_raw()
    body = {
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
            },
            "phone": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
                "sandbox": {
                    "backend": "libvirt",
                    "phone_use": True,
                    "android": {
                        "image_type": "GAPPS",
                        "resolution": "720x1280",
                        "dpi": 320,
                        "gpu": "software",
                    },
                },
            }
        }
    }
    _patch_raw_yaml(raw, body)
    sandbox = raw["contexts"]["phone"]["sandbox"]
    assert sandbox["phone_use"] is True
    assert sandbox["android"]["image_type"] == "GAPPS"
    # Config app accepts the phone-use payload without a validation error.
    _validate_raw(raw)
