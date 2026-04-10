"""macOS sandbox-exec sandbox for isolated Claude CLI execution.

Wraps the free functions in :mod:`open_shrimp.sandbox.macos_helpers` into a
:class:`MacOSSandbox` class that implements the :class:`Sandbox` protocol.
"""

from __future__ import annotations

import logging
from pathlib import Path

from open_shrimp.sandbox.macos_helpers import (
    build_cli_wrapper as _build_cli_wrapper,
)

logger = logging.getLogger(__name__)


class MacOSSandbox:
    """macOS sandbox-exec sandbox implementing the :class:`Sandbox` protocol.

    Uses Apple's ``sandbox-exec`` to restrict the CLI process to only the
    project directory, session storage, and network access.  No image building
    or container lifecycle — the environment is always ready.
    """

    def __init__(
        self,
        context_name: str,
        project_dir: str,
        additional_directories: list[str] | None = None,
    ) -> None:
        self._context_name = context_name
        self._project_dir = project_dir
        self._additional_directories = additional_directories

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def container_name(self) -> str | None:
        return None

    def environment_ready(self) -> bool:
        return True

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        pass

    def running(self) -> bool:
        return True

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        pass

    def provision_workspace(self) -> None:
        # macOS shares the host filesystem — no provisioning needed.
        pass

    def build_cli_wrapper(self) -> tuple[str, list[str]]:
        from open_shrimp.sandbox.macos_helpers import (
            cleanup_paths_for_wrapper,
        )

        path = _build_cli_wrapper(
            context_name=self._context_name,
            project_dir=self._project_dir,
            additional_directories=self._additional_directories,
        )
        return path, cleanup_paths_for_wrapper(path)

    def stop(self) -> None:
        pass

    def get_screenshots_dir(self) -> Path | None:
        return None

    def get_vnc_port(self) -> int | None:
        return None

    def get_text_input_state_path(self) -> Path | None:
        return None

    def get_text_input_active(self) -> bool:
        return False

    def take_screenshot(self, output_path: Path) -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def send_type(self, text: str) -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def send_key(self, key_str: str) -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def focus_window(self, name: str) -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def get_clipboard(self) -> str:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    def set_clipboard(self, text: str) -> None:
        raise NotImplementedError("Computer-use not supported on macOS sandbox")

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        # macOS sandbox shares the host filesystem — no copy needed.
        return list(host_paths)
