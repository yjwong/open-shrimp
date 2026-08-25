"""Suite-wide guards: no test reaches the network, or the real binary cache.

Both failures are silent. A test that walks into a first-use download fetches
tens of megabytes from GitHub, writes them into the developer's own cache, and
*passes* — so the next run is fast, a CI run is slow, an offline run fails for
a reason unrelated to the code under test, and an assertion about "nothing
downloaded yet" starts passing on history rather than on behaviour. Nothing
about that surfaces except as wall-clock, which is why it is turned into an
error here instead of left to whoever notices the traffic.

``tests/test_client_manager_backend_swap.py`` did exactly that once the
opencode backend grew a host prefetch: it drove ``get_or_create_session`` to
test something else entirely and pulled 60 MB down every run.
"""

from __future__ import annotations

import socket

import pytest

import open_shrimp.binaries as binaries

#: Taken before anything can replace it, so the guard delegates to the real
#: method rather than to another test's leftover.
_real_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def managed_bin_dir(tmp_path, monkeypatch):
    """Point the managed bin directory at a fresh temporary one.

    Covers every downloaded binary — cloudflared and moonshine-stt as well as
    the agent CLI — so no test can land one in the cache that later runs read.
    """
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(binaries, "BIN_DIR", bin_dir)
    return bin_dir


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """Refuse any connection that leaves the machine.

    Guarding the socket rather than a fetcher is what makes this hold: patching
    ``stream_to_file`` in the modules that import it covers one library and
    breaks the moment a module is added or switches to a qualified call, while
    ``tunnel.py`` and ``stt.py`` download over httpx and would not be covered at
    all. Everything reaches the network through ``connect``.

    Loopback is allowed, because several tests serve a real archive over it and
    exercise the transfer end to end. Tests that instead keep a real GitHub URL
    and stub ``urlopen`` never create a socket, so they pass through untouched —
    which is why the rule is about connections rather than about URLs.

    A ``RuntimeError``, deliberately: an ``OSError`` here would be caught by the
    fetchers' own "the network is down" handling and turn a blocked test into a
    silently-passing fallback path.
    """

    def guard(self, address):
        # AF_UNIX addresses are paths, and a Unix socket cannot leave the host.
        if not isinstance(address, tuple):
            return _real_connect(self, address)
        host = str(address[0])
        if host == "::1" or host == "localhost" or host.startswith("127."):
            return _real_connect(self, address)
        raise RuntimeError(
            f"A test tried to connect to {host}. Tests must not reach the "
            f"network: stub the fetch the traceback above names, or serve the "
            f"asset over loopback."
        )

    monkeypatch.setattr(socket.socket, "connect", guard)
