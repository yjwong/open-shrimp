import json
import time

import pytest

from open_shrimp.mcp_proxy import credentials


@pytest.fixture(autouse=True)
def reset_credential_caches(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(credentials, "_opencode_file_cache", None)


def write_opencode_auth(tmp_path, data: dict) -> None:
    path = tmp_path / "xdg" / "opencode" / "mcp-auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_get_oauth_credential_reads_opencode_mcp_auth(tmp_path) -> None:
    expires_at_seconds = int(time.time()) + 3600
    write_opencode_auth(
        tmp_path,
        {
            "figma": {
                "serverUrl": "https://mcp.figma.com/mcp",
                "tokens": {
                    "accessToken": "opencode-token",
                    "expiresAt": expires_at_seconds,
                },
            }
        },
    )

    cred = credentials.get_oauth_credential("figma", "https://mcp.figma.com/mcp")

    assert cred is not None
    assert cred.access_token == "opencode-token"
    assert cred.expires_at_ms == expires_at_seconds * 1000


def test_get_oauth_credential_ignores_opencode_auth_for_different_url(
    tmp_path,
) -> None:
    write_opencode_auth(
        tmp_path,
        {
            "figma": {
                "serverUrl": "https://other.example/mcp",
                "tokens": {"accessToken": "wrong-token"},
            }
        },
    )

    cred = credentials.get_oauth_credential("figma", "https://mcp.figma.com/mcp")

    assert cred is None


def test_get_oauth_credential_allows_missing_server_url(tmp_path) -> None:
    write_opencode_auth(
        tmp_path,
        {
            "figma": {
                "tokens": {"accessToken": "opencode-token"},
            }
        },
    )

    cred = credentials.get_oauth_credential("figma", "https://mcp.figma.com/mcp")

    assert cred is not None
    assert cred.access_token == "opencode-token"


def test_get_oauth_credential_returns_none_without_opencode_auth(tmp_path) -> None:
    cred = credentials.get_oauth_credential("figma", "https://mcp.figma.com/mcp")

    assert cred is None


def test_get_oauth_credential_ignores_malformed_opencode_auth(tmp_path) -> None:
    write_opencode_auth(
        tmp_path,
        {
            "figma": {
                "serverUrl": "https://mcp.figma.com/mcp",
                "tokens": {"accessToken": ""},
            }
        },
    )

    cred = credentials.get_oauth_credential("figma", "https://mcp.figma.com/mcp")

    assert cred is None
