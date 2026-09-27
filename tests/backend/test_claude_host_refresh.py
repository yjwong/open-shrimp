"""Host-only Claude token refresh: guests get no refresh token, host refreshes."""

from __future__ import annotations

import json
import stat
import sys
import threading
from pathlib import Path

import pytest

from open_shrimp.backend.claude_sdk import cred_watcher, host_refresh
from open_shrimp.backend.claude_sdk.host_refresh import (
    PROBE_SERVER_NAME,
    REFRESH_LEAD_S,
    RETRY_S,
    keep_host_token_fresh,
    run_refresh_cli,
)
from open_shrimp.backend.claude_sdk.libvirt_install import (
    provision_claude_credentials,
)

HOST_CREDS = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-a",
        "refreshToken": "sk-ant-ort01-r",
        "expiresAt": 1_000_000,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
}


class _Stop(threading.Event):
    """Records each wait and sets itself after *limit* of them."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.waits: list[float] = []
        self._limit = limit

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self._limit:
            self.set()
        return self.is_set()


def test_guest_payload_drops_only_the_refresh_token() -> None:
    out = json.loads(cred_watcher.guest_payload(json.dumps(HOST_CREDS)))
    oauth = out["claudeAiOauth"]
    assert "refreshToken" not in oauth
    assert oauth["accessToken"] == "sk-ant-oat01-a"
    assert oauth["expiresAt"] == 1_000_000
    assert oauth["subscriptionType"] == "max"


def test_write_target_writes_guest_payload_private(tmp_path: Path) -> None:
    cred_watcher.write_target(tmp_path, json.dumps(HOST_CREDS))
    dest = tmp_path / ".credentials.json"
    assert "refreshToken" not in json.loads(dest.read_text())["claudeAiOauth"]
    if sys.platform != "win32":
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "darwin", reason="reads the Keychain")
def test_provision_strips_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_home = tmp_path / "host"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text(
        json.dumps(HOST_CREDS),
    )
    monkeypatch.setattr(Path, "home", lambda: host_home)

    guest_home = tmp_path / "claude-home"
    provision_claude_credentials(guest_home)

    oauth = json.loads((guest_home / ".credentials.json").read_text())[
        "claudeAiOauth"
    ]
    assert "refreshToken" not in oauth
    assert oauth["accessToken"] == "sk-ant-oat01-a"


@pytest.mark.skipif(sys.platform == "darwin", reason="reads the Keychain")
def test_read_host_expires_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = tmp_path / ".credentials.json"
    monkeypatch.setattr(cred_watcher, "HOST_CREDENTIALS", creds)
    assert cred_watcher.read_host_expires_at() is None

    creds.write_text(json.dumps(HOST_CREDS))
    assert cred_watcher.read_host_expires_at() == 1_000_000

    # What the CLI leaves behind after an invalid_grant.
    blanked = {"claudeAiOauth": {"accessToken": "", "refreshToken": "",
                                 "expiresAt": 0}}
    creds.write_text(json.dumps(blanked))
    assert cred_watcher.read_host_expires_at() is None


def test_sleeps_until_the_lead_window_when_far_from_expiry() -> None:
    now = 1_000.0
    expires_at_ms = int((now + 3600) * 1000)
    calls: list[None] = []
    stop = _Stop(limit=1)

    keep_host_token_fresh(
        stop, lambda: expires_at_ms, lambda: calls.append(None),
        now=lambda: now,
    )

    assert calls == []
    assert stop.waits == [pytest.approx(min(3600 - REFRESH_LEAD_S,
                                            host_refresh.MAX_SLEEP_S))]


def test_refreshes_inside_the_lead_window() -> None:
    now = 1_000.0
    expiry = {"ms": int((now + REFRESH_LEAD_S - 10) * 1000)}
    new_ms = int((now + 8 * 3600) * 1000)

    def refresh() -> None:
        expiry["ms"] = new_ms

    stop = _Stop(limit=1)
    keep_host_token_fresh(stop, lambda: expiry["ms"], refresh, now=lambda: now)

    assert expiry["ms"] == new_ms
    # After the refresh it went back to sleeping toward the next expiry.
    assert stop.waits[0] > RETRY_S


def test_retries_when_expiry_does_not_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = 1_000.0
    expires_at_ms = int((now + 60) * 1000)
    calls: list[None] = []
    stop = _Stop(limit=3)

    keep_host_token_fresh(
        stop, lambda: expires_at_ms, lambda: calls.append(None),
        now=lambda: now,
    )

    assert len(calls) == 3
    assert stop.waits == [RETRY_S] * 3
    warnings = [r for r in caplog.records if "did not refresh" in r.message]
    assert len(warnings) == 1


def test_waits_quietly_without_a_refreshable_token() -> None:
    calls: list[None] = []
    stop = _Stop(limit=2)
    keep_host_token_fresh(stop, lambda: None, lambda: calls.append(None))
    assert calls == []
    assert stop.waits == [60, 60]


def test_refresh_command_errors_do_not_kill_the_loop() -> None:
    def boom() -> None:
        raise OSError("gone")

    stop = _Stop(limit=2)
    keep_host_token_fresh(stop, lambda: 1, boom, now=lambda: 1_000.0)
    assert stop.waits == [RETRY_S, RETRY_S]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_run_refresh_cli_invokes_mcp_get_from_an_empty_dir(
    tmp_path: Path,
) -> None:
    record = tmp_path / "record"
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        f'{{ echo "$@"; ls -A; }} > "{record}"\n'
        "exit 1\n"
    )
    stub.chmod(0o755)

    run_refresh_cli(str(stub))

    lines = record.read_text().splitlines()
    assert lines == [f"mcp get {PROBE_SERVER_NAME}"]
