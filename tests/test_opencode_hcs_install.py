"""The OpenCode in-guest installer for the HCS backend.

No guest: ``guest_exec`` is a scripted fake keyed on the argv it is given, and
release resolution is stubbed (the real one calls GitHub).
"""

from __future__ import annotations

import pytest

from open_shrimp.backend.opencode import hcs_install as I
from open_shrimp.backend.opencode.sandbox_bundle import opencode_image_bundle

_MISSING = (127, "")
_PROBE = ["opencode", "--version"]


class _FakeSandbox:
    """Scriptable ``guest_exec``.

    *replies* maps a key — the argv's first token, or ``"install"`` for the
    ``sh -c`` install script — to a list of successive ``(code, output)``
    results; the last one repeats.  Every call is recorded.
    """

    def __init__(self, **replies: list[tuple[int, str]]):
        self.replies = replies
        self.calls: list[list[str]] = []

    def guest_exec(self, argv, *, read_timeout=120.0):
        self.calls.append(list(argv))
        key = "install" if argv[0] == "/bin/sh" else argv[0]
        results = self.replies.get(key)
        if not results:
            return _MISSING
        return results.pop(0) if len(results) > 1 else results[0]


@pytest.fixture(autouse=True)
def _pinned_release(monkeypatch):
    monkeypatch.setattr(I, "resolve_opencode_version", lambda: "1.2.3")


def test_an_image_that_already_ships_opencode_installs_nothing():
    sandbox = _FakeSandbox(opencode=[(0, "opencode 1.2.3\n")])

    I.install_opencode_cli_in_hcs(sandbox)

    assert sandbox.calls == [_PROBE]


def test_a_missing_binary_is_installed_from_the_linux_release():
    sandbox = _FakeSandbox(
        opencode=[_MISSING, (0, "opencode 1.2.3\n")],
        uname=[(0, "x86_64\n")],
        install=[(0, "")],
    )

    I.install_opencode_cli_in_hcs(sandbox)

    assert sandbox.calls[0] == _PROBE
    assert sandbox.calls[1] == ["uname", "-m"]
    install = sandbox.calls[2]
    assert install[:2] == ["/bin/sh", "-c"]
    assert install[-1] == (
        "https://github.com/anomalyco/opencode/releases/download/"
        "v1.2.3/opencode-linux-x64.tar.gz"
    )
    # The host's own binary is never donated — a Windows host has no Linux one.
    assert "install -m 755 /tmp/opencode /usr/local/bin/opencode" in install[2]
    # The probe repeats after the install, so a silent failure cannot pass.
    assert sandbox.calls[3] == _PROBE


def test_an_arm_guest_gets_the_arm_asset():
    sandbox = _FakeSandbox(
        # A zero exit with no output is "absent", not "installed".
        opencode=[(0, "")],
        uname=[(0, "aarch64\n")],
        install=[(0, "")],
    )

    with pytest.raises(RuntimeError, match="still not runnable"):
        I.install_opencode_cli_in_hcs(sandbox)

    assert sandbox.calls[2][-1].endswith("opencode-linux-arm64.tar.gz")


def test_an_unknown_guest_architecture_is_refused():
    sandbox = _FakeSandbox(uname=[(0, "riscv64\n")])

    with pytest.raises(RuntimeError, match="riscv64"):
        I.install_opencode_cli_in_hcs(sandbox)


def test_an_undetectable_architecture_is_refused():
    sandbox = _FakeSandbox(uname=[(1, "boom")])

    with pytest.raises(RuntimeError, match="architecture"):
        I.install_opencode_cli_in_hcs(sandbox)


def test_a_failed_download_names_the_url():
    sandbox = _FakeSandbox(
        uname=[(0, "x86_64\n")],
        install=[(1, "curl: (22) 404")],
    )

    with pytest.raises(RuntimeError, match="opencode-linux-x64.tar.gz"):
        I.install_opencode_cli_in_hcs(sandbox)


def test_the_bundle_wires_the_hook_up():
    assert opencode_image_bundle().hcs_install is I.install_opencode_cli_in_hcs
