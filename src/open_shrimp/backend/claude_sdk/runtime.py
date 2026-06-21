"""Claude :class:`AgentRuntime` factory.

The runtime carries the wrapped-CLI launch contract plus Claude's
host-side credential hooks (the long-lived watcher, the in-guest
credential copy used by VM sandboxes).  All Claude-specific knowledge is
gathered here so the sandbox layer never names an agent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from open_shrimp.backend.claude_sdk.cred_watcher import (
    host_credentials_available,
    watch_host_credentials,
    write_target,
)
from open_shrimp.backend.claude_sdk.libvirt_install import (
    provision_claude_credentials,
)
from open_shrimp.backend.claude_sdk.sandbox_bundle import claude_image_bundle
from open_shrimp.sandbox.agent_runtime import (
    AgentRuntime,
    HomeMount,
    WrappedCLI,
)

logger = logging.getLogger(__name__)


def claude_runtime(
    home_dir: Path, *, guest_dir: str = "/home/claude/.claude",
) -> AgentRuntime:
    """Build the Claude :class:`AgentRuntime`.

    ``home_dir`` is the host-side agent home from
    :meth:`SandboxManager.agent_home_dir`; for Claude the resumable session
    corpus lives under ``home_dir/projects``.  ``env`` declares the
    ``ANTHROPIC_API_KEY`` forwarding contract; the wrappers do the actual
    forwarding.
    """
    env: dict[str, str] = {}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

    return AgentRuntime(
        name="claude",
        home_mount=HomeMount(
            host_dir=home_dir,
            guest_dir=guest_dir,
            holds_session_state=True,
        ),
        inject=provision_claude_credentials,
        env=env,
        launch=WrappedCLI(),
        image_bundle=claude_image_bundle(),
        # Claude refreshes OAuth tokens independently of dispatches; a
        # sandboxed process holding a stale file silently 401s.  The watcher
        # fans host-side refreshes out to every registered sandbox home.
        watch_host_credentials=watch_host_credentials,
        host_credentials_available=host_credentials_available,
        write_cred_target=write_target,
        provision_credentials=provision_claude_credentials,
    )
