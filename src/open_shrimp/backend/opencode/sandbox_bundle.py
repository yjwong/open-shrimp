"""OpenCode :class:`ImageBundle` constructor.

The bundle is the data-only description the sandbox layer reads to build
images, decide guest layout, and dispatch the in-guest installer for VM
sandboxes.  All OpenCode-specific knowledge lives here so the sandbox helper
stays agent-neutral.
"""

from __future__ import annotations

from open_shrimp.backend.opencode.binary import find_opencode_binary
from open_shrimp.backend.opencode.libvirt_install import (
    install_opencode_cli_via_ssh,
)
from open_shrimp.backend.opencode.lima_install import (
    ensure_opencode_cli_in_vm,
)
from open_shrimp.sandbox.agent_runtime import ImageBundle
from open_shrimp.sandbox.skill_paths import SANDBOX_HOME


def opencode_image_bundle() -> ImageBundle:
    """Construct the served-endpoint OpenCode :class:`ImageBundle`."""
    from open_shrimp.sandbox.docker_helpers import OPENCODE_COMPUTER_USE_IMAGE

    return ImageBundle(
        tag_suffix="opencode",
        bundled_dockerfile="Dockerfile.opencode",
        binary_finder=find_opencode_binary,
        context_binary_name="opencode",
        build_arg=("OPENCODE_BIN", "opencode"),
        guest_home=SANDBOX_HOME,
        dind_user="openshrimp",
        task_tmp_prefix="openshrimp",
        computer_use_image=OPENCODE_COMPUTER_USE_IMAGE,
        computer_use_build_args=(("INSTALL_CLAUDE_CODE", "false"),),
        libvirt_install=install_opencode_cli_via_ssh,
        lima_install=ensure_opencode_cli_in_vm,
    )
