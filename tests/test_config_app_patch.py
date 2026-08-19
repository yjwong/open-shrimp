"""``patch_raw_yaml`` round-trips the editable top-level keys, including the
``backend`` selector, while leaving everything else untouched — including keys
inside a context that the config Mini App does not model."""

from __future__ import annotations

import json

import pytest

from open_shrimp.config import (
    _validate_raw,
    load_config,
    load_raw_yaml,
    patch_raw_yaml,
    write_raw_yaml,
)
from open_shrimp.config_app.api import (
    config_get_endpoint,
    config_put_endpoint,
)
from tests.config_app_stub import disable_auth, make_request, write_config


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
    patch_raw_yaml(raw, {"backend": "opencode"})
    assert raw["backend"] == "opencode"


def test_top_level_backend_default_removes_key():
    raw = _base_raw()
    raw["backend"] = "opencode"
    patch_raw_yaml(raw, {"backend": "claude_sdk"})
    assert "backend" not in raw


def test_top_level_backend_null_removes_key():
    raw = _base_raw()
    raw["backend"] = "opencode"
    patch_raw_yaml(raw, {"backend": None})
    assert "backend" not in raw


def test_top_level_backend_empty_removes_key():
    raw = _base_raw()
    raw["backend"] = "opencode"
    patch_raw_yaml(raw, {"backend": ""})
    assert "backend" not in raw


def test_top_level_backend_absent_leaves_untouched():
    raw = _base_raw()
    raw["backend"] = "opencode"
    patch_raw_yaml(raw, {"allowed_users": [2]})
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
    patch_raw_yaml(raw, body)
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
    patch_raw_yaml(raw, body)
    sandbox = raw["contexts"]["phone"]["sandbox"]
    assert sandbox["phone_use"] is True
    assert sandbox["android"]["image_type"] == "GAPPS"
    # Config app accepts the phone-use payload without a validation error.
    _validate_raw(raw)


def _saved_context() -> dict:
    """A context exactly as the Mini App editor serialises it on save."""
    return {
        "directory": "/tmp",
        "description": "d",
        "allowed_tools": [],
        "disallowed_tools": [],
        "model": None,
        "effort": None,
        "backend": None,
        "additional_directories": [],
        "default_for_chats": [],
        "locked_for_chats": [],
        "sandbox": None,
    }


def test_saving_a_context_preserves_unmodelled_mcp_block():
    raw = _base_raw()
    raw["contexts"]["default"]["mcp"] = {
        "playwright": {"command": "npx", "args": ["@playwright/mcp"]},
    }
    ctx = _saved_context()
    ctx["description"] = "edited"
    patch_raw_yaml(raw, {"contexts": {"default": ctx}})
    assert raw["contexts"]["default"]["description"] == "edited"
    assert raw["contexts"]["default"]["mcp"] == {
        "playwright": {"command": "npx", "args": ["@playwright/mcp"]},
    }


def test_saving_a_context_preserves_every_unsent_key():
    raw = _base_raw()
    raw["contexts"]["default"]["some_future_field"] = {"a": 1}
    patch_raw_yaml(raw, {"contexts": {"default": _saved_context()}})
    assert raw["contexts"]["default"]["some_future_field"] == {"a": 1}


def test_removing_a_context_deletes_it():
    raw = _base_raw()
    raw["contexts"]["scratch"] = {
        "directory": "/tmp/scratch",
        "description": "s",
        "allowed_tools": [],
        "mcp": {"srv": {"command": "x"}},
    }
    patch_raw_yaml(raw, {"contexts": {"default": _saved_context()}})
    assert set(raw["contexts"]) == {"default"}


def test_clearing_a_list_is_written_not_restored():
    raw = _base_raw()
    raw["contexts"]["default"]["disallowed_tools"] = ["Bash"]
    raw["contexts"]["default"]["additional_directories"] = ["/srv"]
    patch_raw_yaml(raw, {"contexts": {"default": _saved_context()}})
    assert raw["contexts"]["default"]["disallowed_tools"] == []
    assert raw["contexts"]["default"]["additional_directories"] == []


def test_clearing_a_scalar_is_written_not_restored():
    raw = _base_raw()
    raw["contexts"]["default"]["model"] = "opus"
    raw["contexts"]["default"]["sandbox"] = {"backend": "docker"}
    patch_raw_yaml(raw, {"contexts": {"default": _saved_context()}})
    assert raw["contexts"]["default"]["model"] is None
    assert raw["contexts"]["default"]["sandbox"] is None


def test_new_context_is_added_alongside_existing():
    raw = _base_raw()
    raw["contexts"]["default"]["mcp"] = {"srv": {"command": "x"}}
    body = {"contexts": {"default": _saved_context(), "new": _saved_context()}}
    patch_raw_yaml(raw, body)
    assert set(raw["contexts"]) == {"default", "new"}
    assert raw["contexts"]["default"]["mcp"] == {"srv": {"command": "x"}}
    assert "mcp" not in raw["contexts"]["new"]


def test_merge_preserves_comments_round_trip(tmp_path):
    """The merge writes through ruamel's CommentedMap, keeping comments."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "# top comment\n"
        "telegram:\n"
        "  token: t  # keep me\n"
        "allowed_users:\n"
        "  - 1\n"
        "contexts:\n"
        "  default:\n"
        "    # why this context exists\n"
        "    directory: /tmp\n"
        "    description: d\n"
        "    allowed_tools: []\n"
        "    mcp:\n"
        "      playwright:\n"
        "        command: npx\n"
        "default_context: default\n",
        encoding="utf-8",
    )

    raw = load_raw_yaml(config_file)
    ctx = _saved_context()
    ctx["description"] = "edited"
    patch_raw_yaml(raw, {"contexts": {"default": ctx}})
    write_raw_yaml(config_file, raw)

    text = config_file.read_text(encoding="utf-8")
    assert "# top comment" in text
    assert "# keep me" in text
    assert "# why this context exists" in text
    assert "playwright" in text
    assert "description: edited" in text

    reloaded = load_raw_yaml(config_file)
    assert reloaded["contexts"]["default"]["mcp"] == {
        "playwright": {"command": "npx"},
    }


# --- end-to-end through the real GET/PUT endpoints ---------------------------


@pytest.fixture
def _no_auth(monkeypatch):
    disable_auth(monkeypatch)


def _make_request(config, config_path, body=None):
    return make_request(config, config_path, body)


def _write_config(tmp_path):
    return write_config(tmp_path, mcp=True)


@pytest.mark.asyncio
async def test_editor_save_through_endpoints_keeps_mcp_on_disk(tmp_path, _no_auth):
    """The reported data-loss chain: GET, edit a context, PUT, mcp survives."""
    config_file = _write_config(tmp_path)
    config = load_config(str(config_file))
    assert config.contexts["default"].mcp

    get_resp = await config_get_endpoint(_make_request(config, config_file))
    payload = json.loads(bytes(get_resp.body).decode())

    # The editor rebuilds the context it saved from its own form state, so
    # the payload carries only the fields it models -- no ``mcp``.
    edited = _saved_context()
    edited["description"] = "edited"
    payload["contexts"]["default"] = edited

    put_resp = await config_put_endpoint(
        _make_request(config, config_file, payload),
    )
    assert put_resp.status_code == 200

    reloaded = load_config(str(config_file))
    assert reloaded.contexts["default"].description == "edited"
    assert reloaded.contexts["default"].mcp == {
        "playwright": {"command": "npx", "args": ["@playwright/mcp"]},
    }


@pytest.mark.asyncio
async def test_context_deletion_through_endpoints_removes_it(tmp_path, _no_auth):
    config_file = _write_config(tmp_path)
    config = load_config(str(config_file))

    payload = {
        "allowed_users": [1],
        "default_context": "default",
        "contexts": {"default": _saved_context()},
    }
    # Add a second context, then delete it again through a second save.
    payload["contexts"]["scratch"] = _saved_context()
    resp = await config_put_endpoint(_make_request(config, config_file, payload))
    assert resp.status_code == 200
    assert set(load_config(str(config_file)).contexts) == {"default", "scratch"}

    del payload["contexts"]["scratch"]
    config = load_config(str(config_file))
    resp = await config_put_endpoint(_make_request(config, config_file, payload))
    assert resp.status_code == 200

    reloaded = load_config(str(config_file))
    assert set(reloaded.contexts) == {"default"}
    assert reloaded.contexts["default"].mcp
