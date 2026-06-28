from __future__ import annotations

import logging

from open_shrimp.sandbox.libvirt import LibvirtSandbox
from open_shrimp.sandbox.lima import LimaSandbox


def test_libvirt_provision_continues_when_security_key_helper_install_fails(
    caplog,
) -> None:
    sandbox = LibvirtSandbox.__new__(LibvirtSandbox)
    sandbox._ssh_port = 12345
    sandbox._computer_use = True
    sandbox._runtime = None
    sandbox._context_name = "test-context"

    def fail_install() -> None:
        raise RuntimeError("download failed")

    sandbox._install_security_key_helper = fail_install

    with caplog.at_level(logging.WARNING):
        sandbox.provision_workspace()

    assert "continuing without security-key forwarding" in caplog.text


def test_lima_provision_continues_when_security_key_helper_install_fails(
    caplog,
) -> None:
    sandbox = LimaSandbox.__new__(LimaSandbox)
    sandbox._computer_use = True
    sandbox._runtime = None
    sandbox._context_name = "test-context"

    def fail_install() -> None:
        raise RuntimeError("download failed")

    sandbox._install_security_key_helper = fail_install

    with caplog.at_level(logging.WARNING):
        sandbox.provision_workspace()

    assert "continuing without security-key forwarding" in caplog.text
