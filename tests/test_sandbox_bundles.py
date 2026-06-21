"""Tests for the per-flavour ImageBundle constructors.

Verifies that each backend's bundle exposes the right computer-use image tag
and forwards the right build args, so the sandbox layer's bundle-driven
dispatch reaches the matching image without any agent-name comparisons.
"""

from open_shrimp.backend.claude_sdk.sandbox_bundle import claude_image_bundle
from open_shrimp.backend.opencode.sandbox_bundle import opencode_image_bundle
from open_shrimp.sandbox.docker_helpers import (
    COMPUTER_USE_IMAGE,
    OPENCODE_COMPUTER_USE_IMAGE,
)


def test_claude_bundle_targets_default_computer_use_image():
    bundle = claude_image_bundle()
    assert bundle.computer_use_image == COMPUTER_USE_IMAGE
    assert ("INSTALL_CLAUDE_CODE", "true") in bundle.computer_use_build_args


def test_opencode_bundle_targets_opencode_computer_use_image():
    bundle = opencode_image_bundle()
    assert bundle.computer_use_image == OPENCODE_COMPUTER_USE_IMAGE
    assert ("INSTALL_CLAUDE_CODE", "false") in bundle.computer_use_build_args


def test_claude_bundle_carries_vm_installers():
    bundle = claude_image_bundle()
    assert bundle.libvirt_install is not None
    assert bundle.lima_install is not None


def test_opencode_bundle_carries_vm_installers():
    bundle = opencode_image_bundle()
    assert bundle.libvirt_install is not None
    assert bundle.lima_install is not None
