import json

from open_shrimp.sandbox.opencode_plugins import (
    APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN,
    ensure_opencode_plugin_config,
)


def test_managed_config_copies_host_provider_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config" / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.jsonc").write_text(
        """
        {
          // OpenCode global provider config may be JSONC.
          "provider": {
            "llamacpp": {
              "name": "llama.cpp",
              "npm": "@ai-sdk/openai-compatible",
              "options": {
                "apiKey": "copy-as-is",
                "baseURL": "http://10.101.10.11:8081/v1"
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    config_path = ensure_opencode_plugin_config(tmp_path / "data")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["plugin"] == [APPLY_PATCH_LARGE_DELETE_GUARD_PLUGIN]
    assert config["provider"] == {
        "llamacpp": {
            "name": "llama.cpp",
            "npm": "@ai-sdk/openai-compatible",
            "options": {
                "apiKey": "copy-as-is",
                "baseURL": "http://10.101.10.11:8081/v1",
            },
        }
    }


def test_managed_config_merges_host_provider_config_in_opencode_order(
    tmp_path, monkeypatch,
):
    config_dir = tmp_path / "config" / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"provider": {"llamacpp": {"name": "old", "env": []}}}),
        encoding="utf-8",
    )
    (config_dir / "opencode.json").write_text(
        json.dumps({"provider": {"llamacpp": {"name": "new"}}}),
        encoding="utf-8",
    )
    (config_dir / "opencode.jsonc").write_text(
        json.dumps({"provider": {"other": {"name": "Other"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    config_path = ensure_opencode_plugin_config(tmp_path / "data")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["provider"] == {
        "llamacpp": {"name": "new", "env": []},
        "other": {"name": "Other"},
    }
