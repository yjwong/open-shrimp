"""Phase-2 OpenCode model catalog: ``_normalise_model_options`` shaping and
the ``GET /api/config/models`` endpoint (catalog fetch + 502 on failure)."""

from __future__ import annotations

import json
import types

import pytest

from open_shrimp.config import _parse
from open_shrimp.config_app import api as config_api
from open_shrimp.config_app.api import _normalise_model_options, models_endpoint


def _base_raw(**extra):
    raw = {
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp/proj",
                "description": "d",
                "allowed_tools": [],
                "backend": "opencode",
                "model": "openai/gpt-5.5",
            }
        },
        "default_context": "default",
    }
    raw.update(extra)
    return raw


# --- _normalise_model_options ------------------------------------------------


def test_extra_values_emitted_first_and_deduped_against_catalog():
    catalog = [{"providerID": "openai", "id": "gpt-5.5"}]
    opts = _normalise_model_options(catalog, {"openai/gpt-5.5"})
    assert opts == [{"value": "openai/gpt-5.5", "label": "openai/gpt-5.5"}]


def test_extra_values_without_slash_skipped():
    opts = _normalise_model_options([], {"opus", "openai/gpt-5.5"})
    assert opts == [{"value": "openai/gpt-5.5", "label": "openai/gpt-5.5"}]


def test_id_preferred_then_apiid_fallback():
    catalog = [
        {"providerID": "openai", "id": "gpt-5.5"},
        {"providerID": "anthropic", "apiID": "claude-sonnet-4-6"},
    ]
    opts = _normalise_model_options(catalog)
    values = [o["value"] for o in opts]
    assert values == ["anthropic/claude-sonnet-4-6", "openai/gpt-5.5"]


def test_label_suffixed_only_when_name_differs():
    catalog = [
        {"providerID": "openai", "id": "gpt-5.5", "name": "GPT-5.5"},
        {"providerID": "openai", "id": "gpt-5", "name": "gpt-5"},
    ]
    opts = _normalise_model_options(catalog)
    by_value = {o["value"]: o["label"] for o in opts}
    assert by_value["openai/gpt-5.5"] == "openai/gpt-5.5 - GPT-5.5"
    assert by_value["openai/gpt-5"] == "openai/gpt-5"


def test_sorted_by_value():
    catalog = [
        {"providerID": "openai", "id": "z"},
        {"providerID": "anthropic", "id": "a"},
    ]
    opts = _normalise_model_options(catalog)
    assert [o["value"] for o in opts] == ["anthropic/a", "openai/z"]


def test_invalid_catalog_items_skipped():
    catalog = [
        {"providerID": "openai"},  # no model id
        {"id": "gpt-5.5"},  # no provider
        {"providerID": "openai", "id": "gpt-5.5"},
    ]
    opts = _normalise_model_options(catalog)
    assert opts == [{"value": "openai/gpt-5.5", "label": "openai/gpt-5.5"}]


# --- models_endpoint ---------------------------------------------------------


def _make_request(config, *, directory: str | None = None):
    query = {"directory": directory} if directory is not None else {}
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(config=config)),
        headers={},
        query_params=query,
    )


def _body(response):
    return json.loads(bytes(response.body).decode())


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    async def fake_auth(request):
        return 1

    monkeypatch.setattr(config_api, "_authenticate", fake_auth)


@pytest.mark.asyncio
async def test_endpoint_returns_models_with_configured_value(monkeypatch):
    config = _parse(_base_raw())

    async def fake_get_json(path, *, params=None):
        assert path == "/api/model"
        assert params == {"location[directory]": "/tmp/proj"}
        return [{"providerID": "openai", "id": "gpt-6"}]

    monkeypatch.setattr(config_api._http, "get_json", fake_get_json)

    resp = await models_endpoint(_make_request(config))
    assert resp.status_code == 200
    values = [o["value"] for o in _body(resp)["models"]]
    # The configured model (not in the live catalog) still appears.
    assert "openai/gpt-5.5" in values
    assert "openai/gpt-6" in values


@pytest.mark.asyncio
async def test_endpoint_502_on_fetch_failure(monkeypatch):
    config = _parse(_base_raw())

    async def boom(path, *, params=None):
        raise RuntimeError("serve down")

    monkeypatch.setattr(config_api._http, "get_json", boom)

    resp = await models_endpoint(_make_request(config))
    assert resp.status_code == 502
    assert "serve down" in _body(resp)["error"]


@pytest.mark.asyncio
async def test_endpoint_skips_fetch_for_claude_sdk_context(monkeypatch):
    raw = _base_raw()
    raw["contexts"]["default"]["backend"] = "claude_sdk"
    raw["contexts"]["default"]["model"] = "sonnet"
    config = _parse(raw)

    async def boom(path, *, params=None):
        raise AssertionError("should not fetch for claude_sdk")

    monkeypatch.setattr(config_api._http, "get_json", boom)

    resp = await models_endpoint(_make_request(config, directory="/tmp/proj"))
    assert resp.status_code == 200
    assert _body(resp)["models"] == []
