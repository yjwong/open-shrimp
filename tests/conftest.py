"""Keep every test off the real binary cache.

The other half of this — keeping tests off the network — is `pytest-socket`,
configured in `pyproject.toml`'s `addopts`. Both guard the same silent
failure: several components download themselves on first use, so a test that
walks into one fetches tens of megabytes from GitHub, writes them into the
developer's own cache, and *passes*. The next run is then fast, a CI run is
slow, an offline run fails for a reason unrelated to the code under test, and
an assertion about "nothing downloaded yet" starts passing on history rather
than on behaviour.

`tests/test_client_manager_backend_swap.py` did exactly that once the opencode
backend grew a host prefetch: it drove `get_or_create_session` to test
something else entirely and pulled 60 MB down on every run.
"""

from __future__ import annotations

import pytest

import open_shrimp.binaries as binaries


@pytest.fixture(autouse=True)
def managed_bin_dir(tmp_path, monkeypatch):
    """Point the managed bin directory at a fresh temporary one.

    Covers every downloaded binary — cloudflared and moonshine-stt as well as
    the agent CLI. `pytest-socket` stops a test fetching one; this stops a test
    reading or writing the copy a developer's own machine has.
    """
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(binaries, "BIN_DIR", bin_dir)
    return bin_dir
