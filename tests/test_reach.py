"""Tests for the ``reach()`` sandbox primitive.

``reach(guest_port)`` exposes a guest TCP port to the host and returns
``"127.0.0.1:<host_port>"``.  The WrappedCLI launch flavour has no caller
for it — these tests exercise the primitive so it is covered before the
served-endpoint flavour wires it in.

Each backend is constructed via ``object.__new__`` to bypass the heavyweight
``__init__`` (which would boot a VM / talk to Docker); only the attributes
``reach`` touches are set, and the underlying primitive it delegates to is
mocked.
"""

from unittest.mock import MagicMock, patch

import pytest

from open_shrimp.sandbox.base import PortForward
from open_shrimp.sandbox.docker import DockerSandbox
from open_shrimp.sandbox.libvirt import LibvirtSandbox
from open_shrimp.sandbox.lima import LimaSandbox


# -- Docker: published-port lookup ------------------------------------------


def test_docker_reach_returns_published_host_port():
    """Docker resolves the dynamically-mapped host port via ``docker port``."""
    sb = object.__new__(DockerSandbox)
    sb._context_name = "demo"

    with patch("open_shrimp.sandbox.docker.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="0.0.0.0:32768\n", stderr="",
        )
        endpoint = sb.reach(5900)

    assert endpoint == "127.0.0.1:32768"
    # Queried the guest port on this context's container.
    argv = mock_run.call_args[0][0]
    assert argv[:3] == ["docker", "port", "openshrimp-demo"]
    assert argv[3] == "5900"


def test_docker_reach_handles_ipv6_first_line():
    """A leading ``[::]:PORT`` line is skipped in favour of a parseable port."""
    sb = object.__new__(DockerSandbox)
    sb._context_name = "demo"

    with patch("open_shrimp.sandbox.docker.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="[::]:49160\n0.0.0.0:49160\n", stderr="",
        )
        endpoint = sb.reach(8080)

    assert endpoint == "127.0.0.1:49160"


def test_docker_reach_raises_when_port_not_published():
    """A non-zero ``docker port`` exit (no mapping) raises."""
    sb = object.__new__(DockerSandbox)
    sb._context_name = "demo"

    with patch("open_shrimp.sandbox.docker.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="No such port",
        )
        with pytest.raises(RuntimeError):
            sb.reach(5900)


# -- libvirt / lima: ssh -L tunnel via add_port_forward ----------------------


@pytest.mark.parametrize("cls", [LibvirtSandbox, LimaSandbox])
def test_vm_reach_opens_forward_and_returns_endpoint(cls):
    """VM backends delegate to ``add_port_forward`` and return its host port."""
    sb = object.__new__(cls)
    sb.add_port_forward = MagicMock(
        return_value=PortForward(
            id="f1", guest_port=4096, host_port=54321, scope_key=None,
        )
    )

    endpoint = sb.reach(4096)

    assert endpoint == "127.0.0.1:54321"
    sb.add_port_forward.assert_called_once()
    kwargs = sb.add_port_forward.call_args.kwargs
    assert kwargs["guest_port"] == 4096
    # reach picks a free host port and is not scoped to any conversation.
    assert kwargs["requested_host_port"] is None
    assert kwargs["scope_key"] is None
