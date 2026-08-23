"""Sandbox package -- isolated execution environments for the agent CLI."""

from open_shrimp.sandbox.base import Sandbox, SandboxStartupError
from open_shrimp.sandbox.launch import start_sandboxed_agent
from open_shrimp.sandbox.manager import (
    HcsSandboxManager,
    LibvirtSandboxManager,
    LimaSandboxManager,
    SandboxManager,
    create_sandbox_manager,
    create_sandbox_managers,
    referenced_backends,
)

__all__ = [
    "HcsSandboxManager",
    "LibvirtSandboxManager",
    "LimaSandboxManager",
    "Sandbox",
    "SandboxManager",
    "SandboxStartupError",
    "create_sandbox_manager",
    "create_sandbox_managers",
    "referenced_backends",
    "start_sandboxed_agent",
]
