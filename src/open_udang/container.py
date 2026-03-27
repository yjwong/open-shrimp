"""Docker container support for isolated Claude CLI execution.

When a context has ``containerize: true``, the Claude CLI subprocess runs
inside a Docker container instead of directly on the host.  This provides
strong filesystem isolation — the agent can only access the bind-mounted
project directory and its own session storage.

The approach works by generating a thin wrapper script that invokes
``docker run`` with the right mounts and forwarding all CLI arguments.
The SDK's ``cli_path`` option is pointed at this wrapper, so the rest of
the SDK machinery (stdin/stdout streaming, canUseTool callbacks, MCP)
works unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from platformdirs import user_data_path

logger = logging.getLogger(__name__)

# Docker image name used for containerized contexts.
CONTAINER_IMAGE = "openudang-claude:latest"

# Base directory for per-context container state (session storage, etc.).
CONTAINER_STATE_DIR = user_data_path("openudang") / "containers"


def _ensure_state_dir(context_name: str) -> Path:
    """Create and return the container state directory for a context.

    This directory is bind-mounted as ``~/.claude`` inside the container,
    giving each context its own isolated session storage.
    """
    state_dir = CONTAINER_STATE_DIR / context_name
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def check_docker_available() -> bool:
    """Return True if Docker is available on the host."""
    return shutil.which("docker") is not None


# Shell script that starts rootless Docker daemon inside the container,
# waits for it to be ready, then execs the Claude CLI.
_DIND_ENTRYPOINT = r"""#!/bin/bash
set -eu

# Ensure the current uid exists in /etc/passwd — rootless Docker's
# newuidmap/newgidmap require a valid passwd entry.
MY_UID=$(id -u)
MY_GID=$(id -g)
if ! getent passwd "$MY_UID" > /dev/null 2>&1; then
    echo "claude:x:${MY_UID}:${MY_GID}::/home/claude:/bin/bash" >> /etc/passwd
fi
if ! getent group "$MY_GID" > /dev/null 2>&1; then
    echo "claude:x:${MY_GID}:" >> /etc/group
fi

# Register subordinate uid/gid ranges for the current (non-root) user.
echo "claude:100000:65536" > /etc/subuid
echo "claude:100000:65536" > /etc/subgid

# XDG_RUNTIME_DIR is required by rootless dockerd.
export XDG_RUNTIME_DIR="/tmp/runtime-${MY_UID}"
mkdir -p "$XDG_RUNTIME_DIR"

# Patch dockerd-rootless.sh to tolerate sysctl failures (ip_forward is
# already set via the container's --sysctl flag).
sed 's/sysctl -w \(.*\)$/sysctl -w \1 || true/' /usr/bin/dockerd-rootless.sh \
    > /tmp/dockerd-rootless.sh
chmod +x /tmp/dockerd-rootless.sh

# Start rootless Docker daemon (no iptables in nested containers).
SKIP_IPTABLES=1 /tmp/dockerd-rootless.sh --iptables=false \
    > /tmp/dockerd.log 2>&1 &

# Wait for Docker to be ready (up to 30s).
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"
for _i in $(seq 1 30); do
    if docker info > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Hand off to the Claude CLI.
exec "$@"
"""


def build_cli_wrapper(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
) -> str:
    """Generate a wrapper script that runs the Claude CLI inside Docker.

    The wrapper:
    - Bind-mounts the project directory at the same path inside the
      container (no path remapping needed).
    - Bind-mounts per-context state as ``/home/claude/.claude`` for
      session persistence.
    - Runs as the host user's uid/gid to avoid root-owned files.
    - Passes ``ANTHROPIC_API_KEY`` into the container.
    - Forwards all CLI arguments via ``"$@"``.

    Args:
        context_name: Context name (used for state directory and
            container naming).
        project_dir: Absolute path to the project directory on the host.
        additional_directories: Optional extra directories to bind-mount
            (read-write, same path inside and outside).
        docker_in_docker: When True, start a rootless Docker daemon
            inside the container so the agent can run docker commands.
            Adds ``--cap-add SYS_ADMIN`` and relaxed seccomp/AppArmor.

    Returns:
        Absolute path to the generated wrapper script.
    """
    state_dir = _ensure_state_dir(context_name)
    uid = os.getuid()
    gid = os.getgid()

    # Mount the host's credentials file (read-only) so the CLI can use
    # the authenticated OAuth session (Claude.ai Pro/Teams) without
    # exposing the rest of ~/.claude.
    host_credentials = Path.home() / ".claude" / ".credentials.json"

    # Build the full docker run command as a list of arguments, then
    # join them with line-continuation backslashes for readability.
    docker_args = [
        "exec docker run --rm -i",
        f"--user {uid}:{gid}",
        "-e HOME=/home/claude",
        "-e ANTHROPIC_API_KEY",
        f"-v {project_dir}:{project_dir}",
        f"-v {state_dir}:/home/claude/.claude",
    ]
    if host_credentials.exists():
        docker_args.append(
            f"-v {host_credentials}:/home/claude/.claude/.credentials.json:ro"
        )
    for extra_dir in additional_directories or []:
        docker_args.append(f"-v {extra_dir}:{extra_dir}")

    # When DinD is enabled, write an entrypoint script into the state
    # directory (persists across sessions) and mount it into the
    # container.  Also add the capabilities and security-opt flags
    # needed for rootless Docker.
    entrypoint_path: Path | None = None
    if docker_in_docker:
        docker_args.extend([
            "--cap-add SYS_ADMIN",
            "--cap-add NET_ADMIN",
            "--security-opt seccomp=unconfined",
            "--security-opt apparmor=unconfined",
            "--security-opt systempaths=unconfined",
            "--device /dev/net/tun",
            "--sysctl net.ipv4.ip_forward=1",
        ])
        # Persist the inner Docker's image/container storage across sessions.
        docker_data_dir = state_dir / "docker-data"
        docker_data_dir.mkdir(exist_ok=True)
        docker_args.append(
            f"-v {docker_data_dir}:/home/claude/.local/share/docker"
        )
        # Write the DinD entrypoint script alongside the state dir.
        entrypoint_path = state_dir / "dind-entrypoint.sh"
        entrypoint_path.write_text(_DIND_ENTRYPOINT)
        entrypoint_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                              | stat.S_IROTH | stat.S_IXOTH)
        docker_args.append(
            f"-v {entrypoint_path}:/usr/local/bin/dind-entrypoint.sh:ro"
        )

    docker_args.extend([
        f"-w {project_dir}",
        f"--name openudang-{context_name}-$$",
        CONTAINER_IMAGE,
    ])

    if docker_in_docker:
        docker_args.append(
            '/usr/local/bin/dind-entrypoint.sh /usr/local/bin/claude "$@"'
        )
    else:
        docker_args.append('/usr/local/bin/claude "$@"')

    docker_cmd = " \\\n  ".join(docker_args)
    script = (
        f"#!/bin/bash\n"
        f"# Auto-generated by OpenUdang for containerized context "
        f"'{context_name}'.\n"
        f"# Do not edit — this file is recreated on each session.\n"
        f"{docker_cmd}\n"
    )

    # Write to a temp file that persists for the session lifetime.
    # The caller is responsible for cleanup (or it's cleaned up on reboot).
    fd, wrapper_path = tempfile.mkstemp(
        prefix=f"openudang-docker-{context_name}-",
        suffix=".sh",
    )
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(wrapper_path, stat.S_IRWXU)

    logger.info(
        "Generated Docker wrapper for context '%s'%s: %s",
        context_name,
        " (DinD)" if docker_in_docker else "",
        wrapper_path,
    )
    return wrapper_path


def cleanup_wrapper(wrapper_path: str) -> None:
    """Remove a previously generated wrapper script."""
    try:
        os.unlink(wrapper_path)
    except OSError:
        logger.debug("Failed to remove wrapper script: %s", wrapper_path)
