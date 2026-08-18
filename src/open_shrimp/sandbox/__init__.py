"""Sandbox package -- isolated execution environments for the Claude CLI."""

from open_shrimp.sandbox.base import Sandbox, SandboxStartupError
from open_shrimp.sandbox.manager import (
    DockerSandboxManager,
    HcsSandboxManager,
    LibvirtSandboxManager,
    LimaSandboxManager,
    SandboxManager,
    create_sandbox_manager,
    create_sandbox_managers,
    referenced_backends,
)

__all__ = [
    "DockerSandboxManager",
    "HcsSandboxManager",
    "LibvirtSandboxManager",
    "LimaSandboxManager",
    "Sandbox",
    "SandboxManager",
    "SandboxStartupError",
    "create_sandbox_manager",
    "create_sandbox_managers",
    "referenced_backends",
]
