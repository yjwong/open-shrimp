"""OpenCode :class:`AgentRuntime` factory.

The runtime carries the served-endpoint launch contract — the ``opencode
serve`` argv, the per-context home/data dirs the sandbox bind-mounts in,
and the host→guest ``auth.json``/plugin-config sync that runs before each
launch.  All OpenCode-specific knowledge is gathered here so the sandbox
layer never names an agent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_shrimp.backend.opencode.sandbox_bundle import opencode_image_bundle
from open_shrimp.sandbox.agent_runtime import (
    AgentRuntime,
    GuestMount,
    HomeMount,
    ServedEndpoint,
)
from open_shrimp.sandbox.skill_paths import SANDBOX_HOME

if TYPE_CHECKING:
    import subprocess

logger = logging.getLogger(__name__)


def opencode_runtime(
    *, context_name: str, provider_id: str | None,
) -> AgentRuntime:
    """Build the OpenCode :class:`AgentRuntime` (the served-endpoint flavour).

    The OpenCode home and the managed-plugin data dir are derived from
    ``context_name`` via the *same* per-context host dirs the sandbox actually
    bind-mounts (``get_opencode_home_dir`` / ``get_openshrimp_data_dir``) — so
    the injected ``auth.json`` and plugin config land where the guest sees
    them.

    Contributions:

    * ``home_mount`` — the OpenCode data home (``get_opencode_home_dir``),
      mapped to ``{SANDBOX_HOME}/.local/share/opencode`` in the guest; it holds
      the resumable session corpus.
    * ``inject`` — sync the provider-filtered host ``auth.json`` into the
      home dir, and prepare the managed plugin config under the per-context
      openshrimp-data dir the guest sources ``OPENCODE_CONFIG`` from.
    * ``env`` — ``OPENCODE_CONFIG`` (managed plugin config path, in the guest)
      and ``OPENCODE_SERVER_PASSWORD`` (the served-endpoint Basic-auth secret;
      the live password is minted by the sandbox's served body per launch).
    * ``launch`` — :class:`ServedEndpoint` running ``opencode serve`` on
      ``OPENCODE_GUEST_PORT``.
    """
    from open_shrimp.backend.opencode.process import OpenCodeEndpoint
    from open_shrimp.sandbox.opencode_plugins import (
        ensure_opencode_plugin_config,
    )
    from open_shrimp.sandbox.opencode_runtime import (
        OPENCODE_GUEST_PORT,
        _drain_opencode_output,
        _sync_opencode_auth,
        _wait_for_opencode_ready,
        get_opencode_home_dir,
        get_openshrimp_data_dir,
    )

    opencode_home = get_opencode_home_dir(context_name)
    openshrimp_data = get_openshrimp_data_dir(context_name)
    guest_home = f"{SANDBOX_HOME}/.local/share/opencode"
    guest_config = (
        f"{SANDBOX_HOME}/.local/share/openshrimp/"
        "managed-opencode/plugin-config.json"
    )

    def inject(target_home: Path) -> None:
        # ``target_home`` is the OpenCode data home (host_dir below).
        _sync_opencode_auth(provider_id, target_home)
        try:
            ensure_opencode_plugin_config(openshrimp_data)
        except Exception:
            logger.warning(
                "Failed to prepare OpenShrimp OpenCode plugin config",
                exc_info=True,
            )

    def make_endpoint(base_url: str, auth_header: str, owner: object) -> Any:
        return OpenCodeEndpoint(
            base_url=base_url, auth_header=auth_header, owner=owner,
        )

    def drain_output(proc: "subprocess.Popen[str]") -> None:
        _drain_opencode_output(proc, None)

    serve_argv = [
        "opencode",
        "serve",
        "--hostname",
        "0.0.0.0",
        "--port",
        str(OPENCODE_GUEST_PORT),
        "--print-logs",
    ]

    return AgentRuntime(
        name="opencode",
        home_mount=HomeMount(
            host_dir=opencode_home,
            guest_dir=guest_home,
            holds_session_state=True,
        ),
        inject=inject,
        env={"OPENCODE_CONFIG": guest_config},
        launch=ServedEndpoint(
            serve_argv=serve_argv,
            guest_port=OPENCODE_GUEST_PORT,
            home_mounts=(
                GuestMount(
                    host_dir=opencode_home,
                    guest_mount_point=f"{SANDBOX_HOME}/.local/share/opencode",
                ),
                GuestMount(
                    host_dir=openshrimp_data,
                    guest_mount_point=f"{SANDBOX_HOME}/.local/share/openshrimp",
                ),
            ),
            auth_username="opencode",
            password_env_var="OPENCODE_SERVER_PASSWORD",
            make_endpoint=make_endpoint,
            wait_ready=_wait_for_opencode_ready,
            drain_output=drain_output,
        ),
        image_bundle=opencode_image_bundle(),
        # For OAuth-backed providers ``auth.json`` is rewritten on refresh,
        # but ``opencode serve`` re-reads it per request, so a per-dispatch
        # re-inject is sufficient — no long-lived watcher needed.
        re_inject_on_dispatch=True,
    )
