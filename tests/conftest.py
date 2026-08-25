"""Suite-wide guards against a test reaching the network or the real cache.

A test that walks into a first-use download would fetch tens of megabytes from
GitHub, write them into the developer's own cache, and pass — so the next run
is fast, the CI run is slow, and an offline run fails for a reason that has
nothing to do with the code under test.  ``tests/test_client_manager_backend_swap.py``
did exactly that once the opencode backend grew a host prefetch.
"""

from __future__ import annotations

import pytest

import open_shrimp.binaries as binaries
from open_shrimp.backend.opencode import install as opencode_install


@pytest.fixture(autouse=True)
def managed_bin_dir(tmp_path, monkeypatch):
    """Point the managed bin directory at a fresh temporary one.

    A test that downloads a binary must not land it in the cache every later
    run reads, where it turns an assertion about "nothing downloaded yet" into
    one that passes or fails by history.  This covers every managed binary —
    cloudflared and moonshine-stt as well as the agent CLI.
    """
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(binaries, "BIN_DIR", bin_dir)
    return bin_dir


@pytest.fixture(autouse=True)
def no_cli_download(monkeypatch):
    """Fail rather than fetch the agent CLI.

    Scoped to the one fetcher a test can reach without meaning to: the CLI is
    pulled in from ``client_manager`` and from sandbox provisioning, both of
    which plenty of tests drive for unrelated reasons.  The sandbox asset
    fetchers are not guarded because reaching one is always deliberate — their
    own tests stub ``urlopen`` and exercise the real transfer, so a guard here
    would only fight them.

    A test that genuinely exercises the CLI download overrides this with its
    own stub; one that reached it by accident gets a message naming the URL.
    """

    def refuse(url, dest, *, progress=None, **kwargs):
        raise RuntimeError(
            f"A test tried to download {url}. Stub the fetch it goes through."
        )

    monkeypatch.setattr(opencode_install, "stream_to_file", refuse)
