"""Claude :class:`ImageBundle` constructor.

The bundle is the data-only description the sandbox layer reads to build
images, decide guest layout, and dispatch the in-guest installer for VM
sandboxes.  All Claude-specific knowledge lives here so the sandbox helper
stays agent-neutral.
"""

from __future__ import annotations

from open_shrimp.backend.claude_sdk.binary import find_claude_binary
from open_shrimp.backend.claude_sdk.libvirt_install import (
    install_claude_cli_via_ssh,
)
from open_shrimp.backend.claude_sdk.lima_install import (
    ensure_claude_cli_in_vm,
)
from open_shrimp.sandbox.agent_runtime import ImageBundle


def claude_image_bundle() -> ImageBundle:
    """Construct the wrapped-CLI Claude :class:`ImageBundle`.

    Resolved lazily so importing this module doesn't pull in the SDK or
    spawn the binary lookup at load time.
    """
    from open_shrimp.sandbox.docker_helpers import COMPUTER_USE_IMAGE

    return ImageBundle(
        tag_suffix="claude",
        bundled_dockerfile="Dockerfile.claude",
        binary_finder=find_claude_binary,
        context_binary_name="claude",
        build_arg=("CLAUDE_CLI", "claude"),
        guest_home="/home/claude",
        dind_user="claude",
        # Claude Code hardcodes /tmp/claude-<uid> for background-task output.
        task_tmp_prefix="claude",
        computer_use_image=COMPUTER_USE_IMAGE,
        computer_use_build_args=(("INSTALL_CLAUDE_CODE", "true"),),
        libvirt_install=install_claude_cli_via_ssh,
        lima_install=ensure_claude_cli_in_vm,
    )
