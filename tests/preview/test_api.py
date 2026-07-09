"""Tests for the preview HTTP API routes (PDF streaming + page-anchored review)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

from open_shrimp.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_shrimp.preview.api import create_preview_routes
from open_shrimp.review.auth import generate_auth_token

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ALLOWED_USER_ID = 111222333
CHAT_ID = 99887766

# %PDF magic is enough for the endpoint (it doesn't parse the file).
PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


def _make_config(context_dir: str) -> Config:
    return Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory=context_dir,
                description="Test context",
                model="claude-sonnet-4-6",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )


def _make_app(config: Config) -> Starlette:
    app = Starlette(routes=create_preview_routes())
    app.state.config = config
    return app


def _client(config: Config) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_make_app(config)),
        base_url="http://testserver",
    )


def _build_init_data(
    bot_token: str = BOT_TOKEN,
    user_id: int = ALLOWED_USER_ID,
) -> str:
    user_obj = json.dumps(
        {"id": user_id, "first_name": "Test", "username": "testuser"},
        separators=(",", ":"),
    )
    params: dict[str, str] = {
        "auth_date": str(int(time.time())),
        "user": user_obj,
        "query_id": "AAHQ",
    }
    data_check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    params["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urlencode(params)


def _auth_header() -> dict[str, str]:
    return {"authorization": f"tg-init-data {_build_init_data()}"}


# --- pdf_endpoint ------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_endpoint_serves_pdf(tmp_path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(PDF_BYTES)
    async with _client(_make_config(str(tmp_path))) as client:
        resp = await client.get(
            "/api/preview/pdf",
            params={"path": str(pdf)},
            headers=_auth_header(),
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == PDF_BYTES
    # Never cache, so a regenerated deck isn't served stale on refresh.
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_pdf_endpoint_serves_regenerated_content(tmp_path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(PDF_BYTES)
    async with _client(_make_config(str(tmp_path))) as client:
        first = await client.get(
            "/api/preview/pdf",
            params={"path": str(pdf)},
            headers=_auth_header(),
        )
        assert first.content == PDF_BYTES
        # Agent regenerates the same path with new content.
        new_bytes = b"%PDF-1.4\n% regenerated\n%%EOF\n"
        pdf.write_bytes(new_bytes)
        second = await client.get(
            "/api/preview/pdf",
            params={"path": str(pdf)},
            headers=_auth_header(),
        )
    assert second.content == new_bytes


@pytest.mark.asyncio
async def test_pdf_endpoint_token_param_auth(tmp_path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(PDF_BYTES)
    token = generate_auth_token(ALLOWED_USER_ID, CHAT_ID, BOT_TOKEN)
    async with _client(_make_config(str(tmp_path))) as client:
        resp = await client.get(
            "/api/preview/pdf",
            params={"path": str(pdf), "token": token},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_endpoint_requires_auth(tmp_path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(PDF_BYTES)
    async with _client(_make_config(str(tmp_path))) as client:
        resp = await client.get("/api/preview/pdf", params={"path": str(pdf)})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pdf_endpoint_rejects_outside_context(tmp_path) -> None:
    context_dir = tmp_path / "ctx"
    context_dir.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF_BYTES)
    async with _client(_make_config(str(context_dir))) as client:
        resp = await client.get(
            "/api/preview/pdf",
            params={"path": str(outside)},
            headers=_auth_header(),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pdf_endpoint_missing_file(tmp_path) -> None:
    async with _client(_make_config(str(tmp_path))) as client:
        resp = await client.get(
            "/api/preview/pdf",
            params={"path": str(tmp_path / "nope.pdf")},
            headers=_auth_header(),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pdf_endpoint_rejects_non_pdf(tmp_path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("# hi")
    async with _client(_make_config(str(tmp_path))) as client:
        resp = await client.get(
            "/api/preview/pdf",
            params={"path": str(doc)},
            headers=_auth_header(),
        )
    assert resp.status_code == 400


# --- submit_review_endpoint: page anchors ------------------------------------


@pytest.mark.asyncio
async def test_submit_review_page_comments(tmp_path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(PDF_BYTES)
    dispatch = AsyncMock()
    with patch("open_shrimp.dispatch_registry.dispatch", dispatch):
        async with _client(_make_config(str(tmp_path))) as client:
            resp = await client.post(
                "/api/preview/submit-review",
                json={
                    "chat_id": CHAT_ID,
                    "thread_id": None,
                    "path": str(pdf),
                    "comments": [
                        {"page": 4, "comment": "Fix this title"},
                        {"page": 1, "comment": "Logo is stretched"},
                    ],
                },
                headers=_auth_header(),
            )
    assert resp.status_code == 200
    assert dispatch.await_count == 1
    prompt = dispatch.await_args.args[0]
    assert f"the PDF at `{pdf}`" in prompt
    assert "### Comment 1 (page 4)" in prompt
    assert "Fix this title" in prompt
    assert "### Comment 2 (page 1)" in prompt
    assert dispatch.await_args.args[1] == CHAT_ID


@pytest.mark.asyncio
async def test_submit_review_page_and_block_text_mix(tmp_path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("# hi")
    dispatch = AsyncMock()
    with patch("open_shrimp.dispatch_registry.dispatch", dispatch):
        async with _client(_make_config(str(tmp_path))) as client:
            resp = await client.post(
                "/api/preview/submit-review",
                json={
                    "chat_id": CHAT_ID,
                    "thread_id": None,
                    "path": str(doc),
                    "comments": [
                        {"block_text": "some heading", "comment": "reword"},
                    ],
                },
                headers=_auth_header(),
            )
    assert resp.status_code == 200
    prompt = dispatch.await_args.args[0]
    assert f"the document at `{doc}`" in prompt
    assert "### Comment 1\n" in prompt
    assert "> some heading" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, -1, 2001, "4", 1.5, True])
async def test_submit_review_rejects_invalid_page(tmp_path, page) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(PDF_BYTES)
    dispatch = AsyncMock()
    with patch("open_shrimp.dispatch_registry.dispatch", dispatch):
        async with _client(_make_config(str(tmp_path))) as client:
            resp = await client.post(
                "/api/preview/submit-review",
                json={
                    "chat_id": CHAT_ID,
                    "thread_id": None,
                    "path": str(pdf),
                    "comments": [{"page": page, "comment": "x"}],
                },
                headers=_auth_header(),
            )
    assert resp.status_code == 400
    assert "page" in resp.json()["error"]
    assert dispatch.await_count == 0
