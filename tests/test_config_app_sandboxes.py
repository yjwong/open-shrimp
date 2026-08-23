"""Sandbox catalog endpoint uses declarations and the standing diagnosis."""

from __future__ import annotations

import json
import types

import pytest

from open_shrimp import doctor, sandbox_diagnosis
from open_shrimp.config import _parse
from open_shrimp.config_app.api import create_config_routes, sandboxes_endpoint
from tests.config_app_stub import disable_auth


def _config():
    return _parse({
        "telegram": {"token": "t"},
        "allowed_users": [1],
        "contexts": {
            "default": {
                "directory": "/tmp",
                "description": "d",
                "allowed_tools": [],
            },
        },
        "default_context": "default",
    })


def _request():
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(config=_config())),
        headers={},
    )


def _body(response):
    return json.loads(bytes(response.body).decode())


@pytest.fixture(autouse=True)
def _fresh_diagnoses():
    sandbox_diagnosis._cache.clear()
    sandbox_diagnosis._locks.clear()


@pytest.mark.asyncio
async def test_endpoint_returns_capabilities_and_remedy(monkeypatch):
    disable_auth(monkeypatch)
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")

    def run(label, check, config):
        return doctor.Outcome(
            label,
            label != "virtiofsd",
            "install virtiofsd" if label == "virtiofsd" else "ok",
        )

    monkeypatch.setattr(doctor, "run_check", run)

    response = await sandboxes_endpoint(_request())
    payload = _body(response)

    assert response.status_code == 200
    assert payload["sandbox"]["backend"] == "libvirt"
    assert not payload["sandbox"]["available"]
    assert [offer["backend"] for offer in payload["sandboxes"]] == ["libvirt"]
    offer = payload["sandboxes"][0]
    assert not offer["available"]
    assert "install virtiofsd" in offer["detail"]
    assert "android" in offer["capabilities"]
    assert offer["base_image_placeholder"] == "Path to base qcow2/cloud image"


@pytest.mark.asyncio
async def test_endpoint_reuses_the_diagnosis_cache(monkeypatch):
    disable_auth(monkeypatch)
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    runs = 0

    def run(label, check, config):
        nonlocal runs
        runs += 1
        return doctor.Outcome(label, True, "ok")

    monkeypatch.setattr(doctor, "run_check", run)

    await sandboxes_endpoint(_request())
    await sandboxes_endpoint(_request())

    assert runs == 1


@pytest.mark.asyncio
async def test_endpoint_returns_no_offer_on_an_unsupported_platform(monkeypatch):
    disable_auth(monkeypatch)
    monkeypatch.setattr(doctor.platform, "system", lambda: "FreeBSD")

    response = await sandboxes_endpoint(_request())
    payload = _body(response)

    assert payload["sandboxes"] == []
    assert payload["sandbox"]["backend"] is None
    assert not payload["sandbox"]["available"]
    assert payload["sandbox"]["note"]


def test_sandboxes_route_is_registered():
    routes = create_config_routes()

    route = next(route for route in routes if route.path == "/api/config/sandboxes")
    assert route.methods == {"GET", "HEAD"}
