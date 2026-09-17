"""Keep every test off the real binary cache and off each other's rate budgets.

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
from open_shrimp.rich_message import _draft_budgets


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


@pytest.fixture(autouse=True)
def clean_draft_budgets():
    """Clear the per-chat draft budgets, which are module state.

    ``send_rich_draft`` charges ``_draft_budgets[chat_id]`` on the real
    monotonic clock, so a suite that runs enough streamed turns against one
    ``chat_id`` spends the tier and every later draft is refused — tests
    asserting on drafts then fail on test order rather than behaviour.
    """
    _draft_budgets.clear()
    yield
    _draft_budgets.clear()
