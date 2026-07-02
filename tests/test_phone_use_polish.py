"""Phone-use config/provisioning polish: resolution validation, the labwc
window rule, gpu/resolution parsing, and phone_install_apk registration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from open_shrimp.config import _parse, _validate_raw
from open_shrimp.sandbox.libvirt_helpers import _build_cloud_init_user_data
from open_shrimp.tools import create_openshrimp_tools


def _phone_raw(android: dict | None = None):
    sandbox = {"backend": "libvirt", "phone_use": True}
    if android is not None:
        sandbox["android"] = android
    return {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
                "sandbox": sandbox,
            }
        },
        "default_context": "default",
    }


def test_valid_resolution_passes_validation():
    _validate_raw(_phone_raw({"resolution": "720x1280"}))  # no raise


@pytest.mark.parametrize("bad", ["720", "720X1280", "720x", "1080p", "abcxdef"])
def test_malformed_resolution_rejected(bad):
    with pytest.raises(ValueError, match="WIDTHxHEIGHT"):
        _validate_raw(_phone_raw({"resolution": bad}))


def test_gpu_software_parses_and_disables_virgl_autoenable():
    cfg = _parse(_phone_raw({"gpu": "software"}))
    ctx = cfg.contexts["default"]
    assert ctx.sandbox.android.gpu == "software"
    # virgl auto-enable is skipped for the software opt-out.
    assert ctx.sandbox.virgl is False


def test_phone_use_defaults_gpu_virgl_and_autoenables_virgl():
    cfg = _parse(_phone_raw())
    ctx = cfg.contexts["default"]
    assert ctx.sandbox.android is not None
    assert ctx.sandbox.android.gpu == "virgl"
    assert ctx.sandbox.virgl is True


def test_phone_use_does_not_auto_add_persistent_paths():
    cfg = _parse(_phone_raw())
    ctx = cfg.contexts["default"]
    assert ctx.sandbox.persistent_paths == []


def test_phone_use_preserves_explicit_persistent_paths():
    raw = _phone_raw()
    raw["contexts"]["default"]["sandbox"]["persistent_paths"] = ["/var/lib/example"]
    cfg = _parse(raw)
    ctx = cfg.contexts["default"]
    assert ctx.sandbox.persistent_paths == ["/var/lib/example"]


def test_cloud_init_phone_use_adds_maximize_window_rule():
    user_data = _build_cloud_init_user_data(
        "ssh-ed25519 AAAA", computer_use=True, phone_use=True,
    )
    assert "<windowRules>" in user_data
    assert 'identifier="Waydroid"' in user_data
    assert 'name="Maximize"' in user_data


def test_cloud_init_computer_use_only_has_no_window_rule():
    user_data = _build_cloud_init_user_data("ssh-ed25519 AAAA", computer_use=True)
    assert "<windowRules>" not in user_data


def test_phone_use_registers_install_apk_tool():
    sandbox = MagicMock()
    sandbox.get_screenshots_dir.return_value = "/tmp/shots"
    sandbox.supports_port_forwarding.return_value = False
    tools = create_openshrimp_tools(
        bot=MagicMock(), chat_id=1, sandbox=sandbox, phone_use=True,
    )
    names = {t.name for t in tools}
    assert {"phone_shell", "phone_screenshot", "phone_install_apk"} <= names


def test_no_phone_tools_without_phone_use():
    sandbox = MagicMock()
    sandbox.get_screenshots_dir.return_value = "/tmp/shots"
    sandbox.supports_port_forwarding.return_value = False
    tools = create_openshrimp_tools(bot=MagicMock(), chat_id=1, sandbox=sandbox)
    names = {t.name for t in tools}
    assert "phone_install_apk" not in names
    assert "phone_shell" not in names
