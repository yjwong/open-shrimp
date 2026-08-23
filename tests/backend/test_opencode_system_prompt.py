"""OpenShrimp's appended system prompt must reach OpenCode's wire body.

``BackendOptions.system_prompt`` carries plain text and OpenCode appends it to
the agent's own prompt through ``system`` on ``prompt_async``.  Anything the
server reads as falsy is dropped there without a trace, so the mapping is
asserted on the body actually posted rather than on the options object.
"""

from __future__ import annotations

import pytest

from open_shrimp.backend.opencode.client import OpenCodeClient
from open_shrimp.backend.protocol import BackendOptions


class _Response:
    status_code = 204
    text = ""


class _RecordingHttp:
    """Stands in for the client's ``httpx.AsyncClient``, capturing bodies."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def post(self, path: str, json: dict | None = None) -> _Response:
        self.bodies.append(json or {})
        return _Response()


async def _prompt_body(system_prompt: str | None) -> dict:
    client = OpenCodeClient(
        BackendOptions(
            cwd="/work", model="prov/model", system_prompt=system_prompt,
        )
    )
    http = _RecordingHttp()
    client._http = http
    await client.prompt_session(
        "ses_1", parts=[{"type": "text", "text": "hi"}],
        system=client._options.system_prompt,
    )
    return http.bodies[-1]


@pytest.mark.asyncio
async def test_system_prompt_reaches_the_prompt_body():
    body = await _prompt_body("Answer concisely.")
    assert body["system"] == "Answer concisely."


@pytest.mark.asyncio
async def test_absent_system_prompt_leaves_the_field_off():
    body = await _prompt_body(None)
    assert "system" not in body
