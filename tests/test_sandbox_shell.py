"""``openshrimp sandbox shell CONTEXT``: exec a terminal into a context's guest."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from open_shrimp import main
from open_shrimp.sandbox import manager as sandbox_manager
from open_shrimp.sandbox.libvirt import LibvirtSandbox


def test_libvirt_shell_allocates_a_pty_and_starts_in_the_project_dir(tmp_path):
    sb = object.__new__(LibvirtSandbox)
    sb._sdir = tmp_path / "vm"
    sb._ssh_port = 2222
    sb._project_dir = "/home/me/my project"

    argv = sb.shell_argv()

    assert argv[0] == "ssh"
    assert "-t" in argv
    assert argv[argv.index("-i") + 1] == str(tmp_path / "vm" / "ssh_key")
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-3:] == [
        "openshrimp@localhost", "--",
        "cd '/home/me/my project' && exec bash -l",
    ]


class _FakeSandbox:
    def __init__(
        self, *, built: bool = True, running: bool = True,
        shell: BaseException | None = None,
    ) -> None:
        self.built = built
        self.is_running = running
        self.shell = shell
        self.started = False

    def environment_ready(self) -> bool:
        return self.built

    def running(self) -> bool:
        return self.is_running

    def ensure_running(self) -> None:
        self.started = True

    def shell_argv(self) -> list[str]:
        if self.shell is not None:
            raise self.shell
        return ["ssh", "guest"]


class _FakeManager:
    def __init__(self, sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox
        self.prefix: str | None = "unset"
        self.stopped = False

    def set_instance_prefix(self, instance_name: str | None) -> None:
        self.prefix = instance_name

    def start_backend(self) -> None:
        pass

    def stop_backend(self) -> None:
        self.stopped = True

    def create_sandbox(self, context_name: str, context: Any) -> _FakeSandbox:
        return self.sandbox


@pytest.fixture
def cli(monkeypatch):
    """Run the subcommand against a two-context config; capture the exec."""
    contexts = {
        "boxed": SimpleNamespace(
            directory="/work/boxed",
            sandbox=SimpleNamespace(enabled=True, backend="libvirt"),
        ),
        "bare": SimpleNamespace(directory="/work/bare", sandbox=None),
    }
    config = SimpleNamespace(instance_name="staging", contexts=contexts)
    monkeypatch.setattr(main, "load_config", lambda path: config)
    monkeypatch.setattr(main, "init_paths", lambda name: None)
    execs: list[list[str]] = []
    monkeypatch.setattr(
        main.os, "execvp", lambda file, argv: execs.append(argv),
    )

    def run(context: str, sandbox: _FakeSandbox | None = None) -> Any:
        mgr = _FakeManager(sandbox or _FakeSandbox())
        monkeypatch.setattr(
            sandbox_manager, "create_sandbox_manager", lambda backend: mgr,
        )
        rc = main._run_sandbox_shell(context_name=context, config_path="x")
        return SimpleNamespace(rc=rc, mgr=mgr, execs=execs)

    return run


def test_execs_the_sandbox_shell_under_the_configs_instance_prefix(cli):
    result = cli("boxed")

    assert result.execs == [["ssh", "guest"]]
    assert result.mgr.prefix == "staging"
    # The libvirt connection is closed before exec hands the fd table on.
    assert result.mgr.stopped


def test_boots_a_stopped_guest(cli):
    sandbox = _FakeSandbox(running=False)

    result = cli("boxed", sandbox)

    assert sandbox.started
    assert result.execs == [["ssh", "guest"]]


def test_refuses_a_guest_never_built(cli, capsys):
    sandbox = _FakeSandbox(built=False, running=False)

    result = cli("boxed", sandbox)

    assert result.rc == 1
    assert not sandbox.started
    assert result.execs == []
    assert result.mgr.stopped
    assert "has not been built" in capsys.readouterr().err


@pytest.mark.parametrize("context", ["bare", "missing"])
def test_refuses_a_context_with_no_guest(cli, context):
    assert cli(context).rc == 1


def test_reports_a_backend_with_no_pty(cli, capsys):
    sandbox = _FakeSandbox(shell=NotImplementedError("no guest PTY"))

    result = cli("boxed", sandbox)

    assert result.rc == 1
    assert result.execs == []
    assert "no guest PTY" in capsys.readouterr().err
