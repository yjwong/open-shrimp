"""Sandbox abstraction for isolated Claude CLI execution.

Defines the :class:`Sandbox` protocol that encapsulates different isolation
backends (Docker containers, Lima/libvirt VMs, etc.) behind a
common lifecycle interface.  The SDK's ``cli_path`` option is pointed at a
wrapper script produced by :meth:`Sandbox.build_cli_wrapper`; all other SDK
machinery (stdin/stdout streaming, canUseTool callbacks, MCP) works unchanged.

Use :meth:`SandboxManager.create_sandbox
<open_shrimp.sandbox.manager.SandboxManager.create_sandbox>` to instantiate
the appropriate backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Sandbox(Protocol):
    """Isolated execution environment for the Claude CLI.

    A single instance represents one VM/container and is shared across
    multiple sessions (ChatScopes) using the same context.  Instances
    are cached by context name in the :class:`SandboxManager`.

    Lifecycle:
        1. ``ensure_environment()`` — build image / provision VM (slow, idempotent)
        2. ``ensure_running()`` — start container / check SSH (fast when warm)
        3. ``provision_workspace()`` — sync files into sandbox (idempotent)
        4. ``build_cli_wrapper()`` — generate shell script for ``cli_path``
        5. ``stop()`` — tear down runtime (VM, container, daemons)
    """

    @property
    def context_name(self) -> str:
        """The context name this sandbox belongs to."""
        ...

    @property
    def container_name(self) -> str | None:
        """Docker container name, or ``None`` for non-container backends."""
        ...

    @property
    def host_address(self) -> str:
        """IP or hostname the sandbox should use to reach the host.

        Each backend has its own network topology — Docker containers
        use ``host.docker.internal``, libvirt VMs use the SLIRP gateway
        ``10.0.2.2``, and Lima VMs use ``192.168.5.2``.
        """
        ...

    def environment_ready(self) -> bool:
        """Return ``True`` if the environment is already built.

        Used by the caller to decide whether to show a "building..." progress
        message before calling :meth:`ensure_environment`.
        """
        ...

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        """Build image, provision VM, or similar one-time setup.

        Idempotent — safe to call on every invocation.  Only does real work
        when the environment is missing or outdated.  May be slow on first
        call.

        Args:
            log_file: Optional path where build output is streamed
                line-by-line (for the terminal mini app).
        """
        ...

    def running(self) -> bool:
        """Return ``True`` if the runtime is already up.

        Used by the caller to decide whether to show a "starting..." progress
        message before calling :meth:`ensure_running`.  Must be cheap (no
        side effects, no waiting).
        """
        ...

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        """Ensure the runtime is up (container started, SSH reachable, etc.).

        Called before each CLI invocation.  Fast path when already running.

        Args:
            log_file: Optional path where startup output is streamed
                line-by-line (continuation of the build log).
        """
        ...

    def provision_workspace(self) -> None:
        """Provision the workspace filesystem inside the sandbox.

        Called after :meth:`ensure_running` and before
        :meth:`build_cli_wrapper`.  For backends where the workspace is
        already available (bind mounts, shared filesystems), this is a
        no-op.  VM backends may use this to clone repositories or sync
        files.

        Idempotent — safe to call on every session start.
        """
        ...

    def build_cli_wrapper(self) -> tuple[str, list[str]]:
        """Generate a shell script that execs into the sandbox.

        The script must:
        - Accept Claude CLI args as ``"$@"``
        - Forward stdin/stdout (interactive, ``-i``)
        - Forward ``ANTHROPIC_API_KEY``
        - Self-heal if the runtime died

        Returns:
            A ``(cli_path, cleanup_paths)`` tuple.  *cli_path* is the
            absolute path to the wrapper script.  *cleanup_paths* lists
            all temp files (including the wrapper) that should be deleted
            when the session ends.
        """
        ...

    def stop(self) -> None:
        """Tear down the runtime (stop container, terminate VM, etc.)."""
        ...

    def get_screenshots_dir(self) -> Path | None:
        """Return host-side screenshots directory, or ``None`` if N/A."""
        ...

    def get_vnc_port(self) -> int | None:
        """Return VNC port for computer-use, or ``None`` if N/A."""
        ...

    def get_text_input_state_path(self) -> Path | None:
        """Return host-side path to the text-input-state file, or ``None``."""
        ...

    def get_text_input_active(self) -> bool:
        """Return ``True`` if a text input field is focused in the sandbox."""
        ...

    def take_screenshot(self, output_path: Path) -> None:
        """Take a screenshot and save as PNG to *output_path*.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        """Click at screen coordinates (*x*, *y*) with *button*.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_type(self, text: str) -> None:
        """Type *text* as keyboard input.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_key(self, key_str: str) -> None:
        """Press a key or combo (e.g. ``"ctrl+a"``) .

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        """Scroll at screen coordinates (*x*, *y*).

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def focus_window(self, name: str) -> None:
        """Focus a window by name or title substring.

        Raises :class:`NotImplementedError` for backends without this capability.
        """
        ...

    def get_clipboard(self) -> str:
        """Get the Wayland clipboard contents via ``wl-paste``.

        Returns the clipboard text, or an empty string if unavailable.
        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def set_clipboard(self, text: str) -> None:
        """Set the Wayland clipboard contents via ``wl-copy``.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files from the host into the sandbox.

        Returns a list of sandbox-side paths (same order/length as
        *host_paths*).  If a copy fails for a particular file, the
        original host path is kept as a fallback.

        For non-container backends where host and sandbox share a
        filesystem, this is a no-op that returns *host_paths* unchanged.
        """
        ...


