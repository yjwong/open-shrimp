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
import subprocess
import tempfile
from importlib.resources import files as _pkg_files
from pathlib import Path

from platformdirs import user_data_path

def find_claude_binary() -> str:
    """Find the Claude CLI binary on disk.

    Resolution order:
    1. Bundled binary inside the claude_agent_sdk package
    2. System PATH via shutil.which
    3. Common installation locations

    Returns:
        Absolute path to the Claude CLI binary.

    Raises:
        RuntimeError: If no binary is found on disk.
    """
    # 1. Bundled binary in the SDK package.
    try:
        import claude_agent_sdk
        bundled = (
            Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        )
        if bundled.exists() and bundled.is_file():
            return str(bundled)
    except (ImportError, AttributeError):
        pass

    # 2. System PATH.
    system_claude = shutil.which("claude")
    if system_claude:
        return system_claude

    # 3. Common install locations.
    home = Path.home()
    for location in [
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ]:
        if location.exists() and location.is_file():
            return str(location)

    raise RuntimeError(
        "Could not find the Claude CLI binary. Searched: "
        "claude_agent_sdk bundled binary, system PATH, "
        "~/.npm-global/bin/claude, /usr/local/bin/claude, "
        "~/.local/bin/claude, ~/node_modules/.bin/claude, "
        "~/.yarn/bin/claude, ~/.claude/local/claude. "
        "Install claude-agent-sdk or ensure 'claude' is on your PATH."
    )


logger = logging.getLogger(__name__)

# Docker image name used for containerized contexts.
CONTAINER_IMAGE = "openudang-claude:latest"

# Base directory for per-context container state (session storage, etc.).
CONTAINER_STATE_DIR = user_data_path("openudang") / "containers"

# Custom seccomp profile for DinD: Docker's default + keyctl (inner runc
# session keyrings) + pivot_root (inner container rootfs setup).
def _find_seccomp_profile() -> Path:
    """Locate the DinD seccomp profile.

    Tries the repo root first (dev/editable installs), then falls back to
    importlib.resources (installed wheels/PyApp).  The profile must be
    written to a real file on disk because ``docker run --security-opt
    seccomp=`` requires a filesystem path.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    repo_profile = repo_root / "seccomp-dind.json"
    if repo_profile.is_file():
        return repo_profile

    # Installed wheel / PyApp — extract via importlib.resources.
    pkg_profile = _pkg_files("open_udang").joinpath("seccomp-dind.json")
    # importlib.resources may return a MultiplexedPath or similar; we need
    # a real filesystem path for docker's --security-opt.
    if hasattr(pkg_profile, "is_file") and pkg_profile.is_file():
        return Path(str(pkg_profile))

    # As a last resort try importlib.resources.as_file for zip-backed resources.
    from importlib.resources import as_file
    with as_file(pkg_profile) as p:
        # Copy to a persistent temp location so docker can read it after
        # the context manager exits.
        persistent = Path(tempfile.gettempdir()) / "openudang-seccomp-dind.json"
        if not persistent.exists():
            shutil.copy2(p, persistent)
        return persistent


def ensure_image(
    image_name: str = CONTAINER_IMAGE,
    dockerfile: str | None = None,
) -> None:
    """Ensure the container image exists, building it if necessary.

    When *dockerfile* is ``None`` (the default), builds the base
    ``openudang-claude`` image from the bundled ``Dockerfile.claude``.
    When a custom *dockerfile* path is provided, builds from that file
    instead — the Claude CLI binary is still copied into the build
    context as ``claude`` and available via the ``CLAUDE_CLI`` build arg.

    Args:
        image_name: Docker image tag to build/check.
        dockerfile: Optional path to a custom Dockerfile.  When set,
            the build context is the directory containing the
            Dockerfile (so ``COPY`` instructions work relative to it).

    Raises:
        RuntimeError: If the Claude CLI binary cannot be found or if
            the Docker build fails.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Container image %s already exists", image_name)
        return

    logger.info("Container image %s not found, building...", image_name)

    cli_binary = find_claude_binary()
    logger.info("Using Claude CLI binary: %s", cli_binary)

    if dockerfile is not None:
        # Ensure the base image exists before building a custom image
        # that likely depends on it (e.g. FROM openudang-claude:latest).
        if image_name != CONTAINER_IMAGE:
            ensure_image(image_name=CONTAINER_IMAGE, dockerfile=None)

        # Custom Dockerfile: use its parent directory as the build
        # context, copying the CLI binary in alongside it.
        dockerfile_path = Path(dockerfile).resolve()
        if not dockerfile_path.is_file():
            raise RuntimeError(
                f"Custom Dockerfile not found: {dockerfile_path}"
            )
        build_dir_path = dockerfile_path.parent
        # Copy CLI binary into the build context (if not already there).
        cli_dest = build_dir_path / "claude"
        if not cli_dest.exists() or not cli_dest.samefile(Path(cli_binary)):
            shutil.copy2(cli_binary, cli_dest)
        _docker_build(
            image_name=image_name,
            build_dir=str(build_dir_path),
            dockerfile_name=dockerfile_path.name,
        )
    else:
        # Default: bundled Dockerfile.claude in a temp build context.
        repo_root = Path(__file__).resolve().parent.parent.parent
        repo_dockerfile = repo_root / "Dockerfile.claude"
        if repo_dockerfile.is_file():
            dockerfile_text = repo_dockerfile.read_text()
        else:
            dockerfile_text = (
                _pkg_files("open_udang")
                .joinpath("Dockerfile.claude")
                .read_text()
            )

        with tempfile.TemporaryDirectory(
            prefix="openudang-build-"
        ) as build_dir:
            build_path = Path(build_dir)
            shutil.copy2(cli_binary, build_path / "claude")
            (build_path / "Dockerfile").write_text(dockerfile_text)
            _docker_build(image_name=image_name, build_dir=build_dir)

    logger.info("Successfully built container image %s", image_name)


def _docker_build(
    image_name: str,
    build_dir: str,
    dockerfile_name: str = "Dockerfile",
) -> None:
    """Run ``docker build`` and stream output to the logger.

    Raises:
        RuntimeError: If the build fails.
    """
    process = subprocess.Popen(
        [
            "docker", "build",
            "-t", image_name,
            "-f", dockerfile_name,
            "--build-arg", "CLAUDE_CLI=claude",
            ".",
        ],
        cwd=build_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        output_lines.append(line)
        logger.info("docker build: %s", line)
    returncode = process.wait()

    if returncode != 0:
        output = "\n".join(output_lines)
        raise RuntimeError(
            f"Failed to build container image {image_name}. "
            f"Docker build output:\n{output}"
        )


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

# Disable slirp4netns's internal sandbox and seccomp — these try to
# create mount namespaces/apply seccomp filters which are blocked by the
# outer container's security profile.  The outer container already
# provides isolation.
export DOCKERD_ROOTLESS_ROOTLESSKIT_SLIRP4NETNS_SANDBOX=false
export DOCKERD_ROOTLESS_ROOTLESSKIT_SLIRP4NETNS_SECCOMP=false

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
    image_name: str = CONTAINER_IMAGE,
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
            Uses rootless Docker with fuse-overlayfs and slirp4netns.
            Adds ``CAP_SYS_ADMIN``, ``apparmor=unconfined``,
            ``systempaths=unconfined``, and a custom seccomp profile
            (Docker default + ``keyctl`` + ``pivot_root``).

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
    # container.  Rootless Docker needs:
    #   - CAP_SYS_ADMIN: for user namespaces and mount inside the container
    #   - apparmor=unconfined: docker-default blocks mount/newuidmap
    #   - systempaths=unconfined: allows mounting proc/sys in nested userns
    #   - Custom seccomp profile: default + keyctl + pivot_root (for inner runc)
    entrypoint_path: Path | None = None
    if docker_in_docker:
        docker_args.extend([
            "--cap-add SYS_ADMIN",
            "--security-opt apparmor=unconfined",
            "--security-opt systempaths=unconfined",
            f"--security-opt seccomp={_find_seccomp_profile()}",
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
        image_name,
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
