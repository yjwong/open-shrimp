"""Sandbox package -- isolated execution environments for the Claude CLI."""

from open_shrimp.sandbox.base import Sandbox
from open_shrimp.sandbox.manager import (
    DockerSandboxManager,
    MacOSSandboxManager,
    SandboxManager,
    create_sandbox_manager,
    create_sandbox_managers,
)

__all__ = [
    "DockerSandboxManager",
    "MacOSSandboxManager",
    "Sandbox",
    "SandboxManager",
    "create_sandbox_manager",
    "create_sandbox_managers",
]
