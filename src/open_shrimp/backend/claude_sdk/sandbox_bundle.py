"""Claude :class:`ImageBundle` constructor.

The bundle is the data-only description the sandbox layer reads to decide
guest layout and dispatch the in-guest installer.  All Claude-specific
knowledge lives here so the sandbox helper stays agent-neutral.
"""

from __future__ import annotations

from open_shrimp.backend.claude_sdk.libvirt_install import (
    install_claude_cli_via_ssh,
)
from open_shrimp.backend.claude_sdk.lima_install import (
    ensure_claude_cli_in_vm,
)
from open_shrimp.backend.claude_sdk.hcs_install import (
    install_claude_cli_in_hcs,
)
from open_shrimp.sandbox.agent_runtime import ImageBundle


def claude_image_bundle() -> ImageBundle:
    """Construct the wrapped-CLI Claude :class:`ImageBundle`."""
    return ImageBundle(
        tag_suffix="claude",
        guest_home="/home/claude",
        # Claude Code hardcodes /tmp/claude-<uid> for background-task output.
        task_tmp_prefix="claude",
        libvirt_install=install_claude_cli_via_ssh,
        lima_install=ensure_claude_cli_in_vm,
        hcs_install=install_claude_cli_in_hcs,
    )
