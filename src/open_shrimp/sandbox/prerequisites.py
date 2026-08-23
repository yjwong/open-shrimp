"""Declarations for every sandbox backend OpenShrimp supports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Check = Callable[[Any], tuple[bool, str]]

# These fields have the same meaning on every remaining backend.  Backend-only
# fields are added by the declaration that implements them.
COMMON_CONFIG_FIELDS = frozenset({
    "enabled",
    "computer_use",
    "memory",
    "cpus",
    "disk_size",
    "base_image",
    "provision",
    "allow_host_escape",
})


@dataclass(frozen=True)
class SandboxBackendDeclaration:
    backend: str
    label: str
    summary: str
    platform: str
    checks: tuple[tuple[str, Check], ...]
    capabilities: frozenset[str]
    base_image_placeholder: str
    unsupported_reasons: tuple[tuple[str, str], ...] = ()

    def unsupported_reason(self, field: str) -> str | None:
        return dict(self.unsupported_reasons).get(field)


# Imported after the shared type and fields are defined so each lightweight
# leaf can construct its declaration without importing a backend implementation.
from open_shrimp.sandbox.hcs_prereq import DECLARATION as HCS  # noqa: E402
from open_shrimp.sandbox.libvirt_prereq import DECLARATION as LIBVIRT  # noqa: E402
from open_shrimp.sandbox.lima_prereq import DECLARATION as LIMA  # noqa: E402

DECLARATIONS: tuple[SandboxBackendDeclaration, ...] = (LIBVIRT, LIMA, HCS)
DECLARATIONS_BY_BACKEND = {
    declaration.backend: declaration for declaration in DECLARATIONS
}
