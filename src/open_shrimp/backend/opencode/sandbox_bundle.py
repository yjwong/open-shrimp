"""OpenCode :class:`ImageBundle` constructor.

The bundle is the data-only description the sandbox layer reads to decide
guest layout and dispatch the in-guest installer.  All OpenCode-specific
knowledge lives here so the sandbox helper stays agent-neutral.
"""

from __future__ import annotations

from open_shrimp.backend.opencode.hcs_install import (
    install_opencode_cli_in_hcs,
)
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
    return ImageBundle(
        tag_suffix="opencode",
        guest_home=SANDBOX_HOME,
        guest_argv0="opencode",
        task_tmp_prefix="openshrimp",
        libvirt_install=install_opencode_cli_via_ssh,
        lima_install=ensure_opencode_cli_in_vm,
        hcs_install=install_opencode_cli_in_hcs,
    )
