"""Tests for the per-agent ImageBundle constructors.

Verifies that each agent's bundle names a distinct guest identity and carries
an installer for every backend, so the sandbox layer's bundle-driven dispatch
reaches the right guest layout without any agent-name comparisons.
"""

from open_shrimp.backend.claude_sdk.sandbox_bundle import claude_image_bundle
from open_shrimp.backend.opencode.sandbox_bundle import opencode_image_bundle


def test_bundles_have_distinct_identities():
    """``tag_suffix`` is the key a backend uses to tell two guests apart, so
    two agents sharing one would silently reuse each other's guest."""
    assert claude_image_bundle().tag_suffix != opencode_image_bundle().tag_suffix


def test_claude_bundle_carries_guest_installers():
    bundle = claude_image_bundle()
    assert bundle.libvirt_install is not None
    assert bundle.lima_install is not None
    assert bundle.hcs_install is not None


def test_opencode_bundle_carries_guest_installers():
    bundle = opencode_image_bundle()
    assert bundle.libvirt_install is not None
    assert bundle.lima_install is not None
    assert bundle.hcs_install is not None


def test_task_tmp_prefix_follows_the_agent_not_the_guest_user():
    """Claude Code hardcodes ``/tmp/claude-<uid>`` whoever runs it, so the
    prefix is the agent's convention rather than the guest username."""
    assert claude_image_bundle().guest_task_tmp(1000) == "/tmp/claude-1000"
    assert opencode_image_bundle().guest_task_tmp(1000) == "/tmp/openshrimp-1000"
