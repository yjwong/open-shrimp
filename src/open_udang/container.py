"""Docker container support for isolated Claude CLI execution.

When a context has ``containerize: true``, the Claude CLI subprocess runs
inside a Docker container instead of directly on the host.  This provides
strong filesystem isolation — the agent can only access the bind-mounted
project directory and its own session storage.

Containers are **persistent**: one long-lived container per context name,
shared across all sessions and threads.  The first invocation starts the
container with ``docker run -d`` (using ``sleep infinity`` as the keep-alive
process), and subsequent invocations use ``docker exec -i`` to run the
Claude CLI inside the already-running container.  This eliminates the 2-5s
``docker run`` overhead per invocation (and 10-30s for DinD contexts where
the rootless Docker daemon now stays warm).

Testcontainers Ryuk is used as a crash-safety net: a TCP connection to Ryuk
acts as a liveness signal for the bot process.  If the bot dies without
graceful shutdown, Ryuk reaps all labelled containers after a short timeout.

The SDK's ``cli_path`` option is pointed at a wrapper script that does the
``docker exec`` (with fallback to ``docker run -d`` if the container isn't
running).  All other SDK machinery (stdin/stdout streaming, canUseTool
callbacks, MCP) works unchanged.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import socket
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

# Docker image name for computer-use (GUI) contexts.
COMPUTER_USE_IMAGE = "openudang-computer-use:latest"

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


def ensure_computer_use_image(
    image_name: str = COMPUTER_USE_IMAGE,
) -> None:
    """Ensure the computer-use container image exists, building if necessary.

    Builds the base ``openudang-claude`` image first (if needed), then
    layers ``Dockerfile.computer-use`` on top with labwc, wlrctl, grim,
    wayvnc, and Chromium.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Computer-use image %s already exists", image_name)
        return

    # Ensure the base image exists first.
    ensure_image(image_name=CONTAINER_IMAGE, dockerfile=None)

    logger.info("Computer-use image %s not found, building...", image_name)

    repo_root = Path(__file__).resolve().parent.parent.parent
    repo_dockerfile = repo_root / "Dockerfile.computer-use"
    computer_use_dir = repo_root / "computer-use"

    if repo_dockerfile.is_file() and computer_use_dir.is_dir():
        # Build from the repo root so COPY computer-use/* works.
        _docker_build(
            image_name=image_name,
            build_dir=str(repo_root),
            dockerfile_name="Dockerfile.computer-use",
        )
    else:
        # Installed wheel / PyApp — extract assets to a temp dir.
        with tempfile.TemporaryDirectory(
            prefix="openudang-computer-use-build-"
        ) as build_dir:
            build_path = Path(build_dir)
            pkg = _pkg_files("open_udang")

            # Copy Dockerfile.
            dockerfile_text = pkg.joinpath(
                "Dockerfile.computer-use"
            ).read_text()
            (build_path / "Dockerfile.computer-use").write_text(
                dockerfile_text
            )

            # Copy computer-use assets.
            cu_dir = build_path / "computer-use"
            cu_dir.mkdir()
            for asset_name in ("entrypoint.sh", "rc.xml", "autostart"):
                asset = pkg.joinpath("computer-use", asset_name)
                (cu_dir / asset_name).write_text(asset.read_text())

            _docker_build(
                image_name=image_name,
                build_dir=str(build_path),
                dockerfile_name="Dockerfile.computer-use",
            )

    logger.info("Successfully built computer-use image %s", image_name)


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


def get_screenshots_dir(context_name: str) -> Path:
    """Return the host-side screenshots directory for a computer-use context."""
    return CONTAINER_STATE_DIR / context_name / "screenshots"


def get_text_input_active(context_name: str) -> bool:
    """Check if a text input field is focused inside a computer-use container.

    Reads /tmp/text-input-state written by seat-keyboard's input-method-v2
    monitor.  Returns True if a text field is active, False otherwise.
    """
    name = _container_name(context_name)
    result = subprocess.run(
        ["docker", "exec", name, "cat", "/tmp/text-input-state"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def get_vnc_port(context_name: str) -> int | None:
    """Return the host-mapped VNC port for a computer-use container, or None.

    The computer-use container exposes port 5900 with dynamic host mapping.
    This function queries Docker for the actual mapped host port.
    """
    name = _container_name(context_name)
    result = subprocess.run(
        ["docker", "port", name, "5900"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    # Output is like "0.0.0.0:32768" or "[::]:32768\n0.0.0.0:32768".
    for line in result.stdout.strip().splitlines():
        port_str = line.rsplit(":", 1)[-1]
        try:
            return int(port_str)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Ryuk reaper — crash-safe container cleanup
# ---------------------------------------------------------------------------

RYUK_IMAGE = "testcontainers/ryuk:0.11.0"
_CONTAINER_LABEL = "openudang"

_ryuk_socket: socket.socket | None = None
_ryuk_container_id: str | None = None


def start_ryuk() -> None:
    """Start Testcontainers Ryuk and register a label filter.

    Ryuk is a container reaper that watches a TCP connection as a liveness
    signal.  As long as the connection is open, labelled containers are kept
    alive.  When the connection drops (bot crash / exit), Ryuk reaps them.

    This function is a no-op if Docker is not available or Ryuk fails to
    start (the bot continues without crash cleanup, matching pre-Ryuk
    behaviour).
    """
    global _ryuk_socket, _ryuk_container_id  # noqa: PLW0603

    if not check_docker_available():
        return

    try:
        # Start Ryuk container with a random host port.
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", "openudang-ryuk",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-p", "8080",
                "--label", f"{_CONTAINER_LABEL}.ryuk=true",
                RYUK_IMAGE,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Container name conflict — Ryuk may already be running from
            # a previous bot instance.  Remove it and retry.
            if "Conflict" in result.stderr or "already in use" in result.stderr:
                subprocess.run(
                    ["docker", "rm", "-f", "openudang-ryuk"],
                    capture_output=True,
                )
                result = subprocess.run(
                    [
                        "docker", "run", "-d",
                        "--name", "openudang-ryuk",
                        "-v", "/var/run/docker.sock:/var/run/docker.sock",
                        "-p", "8080",
                        "--label", f"{_CONTAINER_LABEL}.ryuk=true",
                        RYUK_IMAGE,
                    ],
                    capture_output=True,
                    text=True,
                )
            if result.returncode != 0:
                logger.warning(
                    "Failed to start Ryuk container: %s", result.stderr.strip()
                )
                return

        _ryuk_container_id = result.stdout.strip()
        logger.info("Started Ryuk container: %s", _ryuk_container_id[:12])

        # Discover the mapped host port.
        port_result = subprocess.run(
            ["docker", "port", "openudang-ryuk", "8080"],
            capture_output=True,
            text=True,
        )
        if port_result.returncode != 0:
            logger.warning("Failed to get Ryuk port: %s", port_result.stderr.strip())
            _cleanup_ryuk_container()
            return

        # Output is like "0.0.0.0:32768" or "[::]:32768".
        port_str = port_result.stdout.strip().rsplit(":", 1)[-1]
        port = int(port_str)

        # Connect to Ryuk and register our label filter.  Docker's
        # port forwarding accepts TCP connections before Ryuk's Go
        # server has called Accept(), so the connect() succeeds but
        # the subsequent send/recv gets ConnectionResetError.  Retry
        # the full connect+send+recv sequence until Ryuk is ready.
        import time as _time

        filter_msg = f"label={_CONTAINER_LABEL}=true\n".encode()
        sock: socket.socket | None = None
        for _attempt in range(10):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(("127.0.0.1", port))
                sock.sendall(filter_msg)
                ack = sock.recv(1024).decode().strip()
                if ack == "ACK":
                    break
                logger.warning("Unexpected Ryuk response: %s", ack)
                sock.close()
                sock = None
            except (ConnectionResetError, ConnectionRefusedError, OSError):
                if sock is not None:
                    sock.close()
                    sock = None
                _time.sleep(0.2)
        else:
            logger.warning("Could not connect to Ryuk after retries")
            _cleanup_ryuk_container()
            return

        # Keep the socket open — this is the liveness signal.
        sock.settimeout(None)
        _ryuk_socket = sock
        logger.info("Ryuk connected on port %d, label filter registered", port)

    except Exception:
        logger.warning("Failed to start Ryuk (continuing without crash cleanup)", exc_info=True)
        _cleanup_ryuk_container()


def stop_ryuk() -> None:
    """Close the Ryuk connection and remove the Ryuk container.

    On graceful shutdown the caller should stop managed containers
    explicitly via :func:`stop_all_containers` *before* calling this,
    so Ryuk only serves as a crash-safety net.
    """
    global _ryuk_socket, _ryuk_container_id  # noqa: PLW0603

    if _ryuk_socket is not None:
        try:
            _ryuk_socket.close()
        except OSError:
            pass
        _ryuk_socket = None

    _cleanup_ryuk_container()


def _cleanup_ryuk_container() -> None:
    global _ryuk_container_id  # noqa: PLW0603
    if _ryuk_container_id is not None:
        subprocess.run(
            ["docker", "rm", "-f", "openudang-ryuk"],
            capture_output=True,
        )
        logger.info("Removed Ryuk container")
        _ryuk_container_id = None


# ---------------------------------------------------------------------------
# Persistent container lifecycle
# ---------------------------------------------------------------------------

def _container_name(context_name: str) -> str:
    """Return the fixed Docker container name for a context."""
    return f"openudang-{context_name}"


def _get_container_state(name: str) -> str | None:
    """Return the container state ('running', 'exited', …) or None."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def stop_all_containers() -> None:
    """Stop and remove all OpenUdang-managed containers (graceful shutdown)."""
    result = subprocess.run(
        [
            "docker", "ps", "-a",
            "--filter", f"label={_CONTAINER_LABEL}=true",
            "--format", "{{.Names}}",
        ],
        capture_output=True,
        text=True,
    )
    for name in result.stdout.strip().splitlines():
        name = name.strip()
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            logger.info("Removed container %s", name)


# Shell script that starts rootless Docker daemon inside the container,
# waits for it to be ready, then keeps the container alive.
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

# Keep the container alive.  CLI invocations arrive via `docker exec`.
exec sleep infinity
"""


def _build_docker_run_argv(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str = CONTAINER_IMAGE,
) -> tuple[list[str], str]:
    """Build the ``docker run -d`` argv for creating a persistent container.

    Returns ``(docker_run_argv, container_name)`` where *docker_run_argv*
    is a complete argv list ending with the image and keep-alive command.
    """
    state_dir = _ensure_state_dir(context_name)
    uid = os.getuid()
    gid = os.getgid()
    container_name = _container_name(context_name)

    host_credentials = Path.home() / ".claude" / ".credentials.json"

    # Git identity — baked into the container at creation time.
    git_env_args: list[str] = []
    for git_key, env_vars in [
        ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
        ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
    ]:
        try:
            value = subprocess.check_output(
                ["git", "config", "--global", git_key],
                text=True,
            ).strip()
            if value:
                for env_var in env_vars:
                    git_env_args.append(f"{env_var}={value}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    docker_argv: list[str] = [
        "docker", "run", "-d",
        "--name", container_name,
        "--label", f"{_CONTAINER_LABEL}=true",
        "--label", f"{_CONTAINER_LABEL}.context={context_name}",
        "--user", f"{uid}:{gid}",
        "-e", "HOME=/home/claude",
    ]
    for env_arg in git_env_args:
        docker_argv.extend(["-e", env_arg])

    # Mount Claude CLI tmp dir inside the container so background task
    # outputs are written to the host-visible state directory.
    claude_tmp_dir = state_dir / "tmp"
    claude_tmp_dir.mkdir(exist_ok=True)
    docker_argv.extend([
        "-v", f"{project_dir}:{project_dir}",
        "-v", f"{state_dir}:/home/claude/.claude",
        "-v", f"{claude_tmp_dir}:/tmp/claude-{uid}",
    ])
    if host_credentials.exists():
        docker_argv.extend([
            "-v",
            f"{host_credentials}:/home/claude/.claude/.credentials.json:ro",
        ])
    for extra_dir in additional_directories or []:
        docker_argv.extend(["-v", f"{extra_dir}:{extra_dir}"])

    if docker_in_docker:
        docker_argv.extend([
            "--cap-add", "SYS_ADMIN",
            "--security-opt", "apparmor=unconfined",
            "--security-opt", "systempaths=unconfined",
            "--security-opt", f"seccomp={_find_seccomp_profile()}",
            "--device", "/dev/net/tun",
            "--sysctl", "net.ipv4.ip_forward=1",
        ])
        docker_data_dir = state_dir / "docker-data"
        docker_data_dir.mkdir(exist_ok=True)
        docker_argv.extend([
            "-v", f"{docker_data_dir}:/home/claude/.local/share/docker",
        ])
        if not computer_use:
            # Standalone DinD: use the dedicated entrypoint script.
            entrypoint_path = state_dir / "dind-entrypoint.sh"
            entrypoint_path.write_text(_DIND_ENTRYPOINT)
            entrypoint_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                                  | stat.S_IROTH | stat.S_IXOTH)
            docker_argv.extend([
                "-v",
                f"{entrypoint_path}:/usr/local/bin/dind-entrypoint.sh:ro",
            ])

    if computer_use:
        # Headless Wayland compositor environment.
        docker_argv.extend([
            "-e", "WLR_BACKENDS=headless",
            "-e", "WLR_RENDERER=pixman",
            "-e", "WLR_HEADLESS_OUTPUTS=1",
            "-e", "WAYLAND_DISPLAY=wayland-0",
            "-e", f"XDG_RUNTIME_DIR=/tmp/runtime-{uid}",
        ])
        # Bind-mount screenshots directory for host access.
        screenshots_dir = state_dir / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)
        docker_argv.extend([
            "-v", f"{screenshots_dir}:/tmp/screenshots",
        ])
        # Expose VNC port (dynamic mapping to avoid conflicts).
        docker_argv.extend(["-p", "5900"])
        # When both computer_use and DinD are enabled, the computer-use
        # entrypoint handles dockerd startup via ENABLE_DIND=1.
        if docker_in_docker:
            docker_argv.extend(["-e", "ENABLE_DIND=1"])

    docker_argv.extend(["-w", project_dir])

    # Image and keep-alive command.
    docker_argv.append(image_name)
    if computer_use:
        # The computer-use entrypoint handles both compositor and
        # optional DinD (via ENABLE_DIND env var).
        docker_argv.append("/usr/local/bin/computer-use-entrypoint.sh")
    elif docker_in_docker:
        docker_argv.append("/usr/local/bin/dind-entrypoint.sh")
    else:
        docker_argv.extend(["sleep", "infinity"])

    return docker_argv, container_name


def ensure_container_running(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str = CONTAINER_IMAGE,
) -> str:
    """Ensure a persistent container is running for the given context.

    If the container already exists and is running, this is a fast no-op
    (a single ``docker inspect``).  Otherwise it creates a new detached
    container.

    Race-safe: if two threads try to create the container simultaneously,
    one will get a name-conflict error and fall through to the running
    container.

    Returns:
        The container name (e.g. ``openudang-dev``).
    """
    name = _container_name(context_name)
    state = _get_container_state(name)
    if state == "running":
        logger.info("Container %s already running", name)
        return name

    # Remove stale container (exited, dead, created).
    if state is not None:
        logger.info("Removing stale container %s (state=%s)", name, state)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    docker_argv, _ = _build_docker_run_argv(
        context_name=context_name,
        project_dir=project_dir,
        additional_directories=additional_directories,
        docker_in_docker=docker_in_docker,
        computer_use=computer_use,
        image_name=image_name,
    )

    result = subprocess.run(docker_argv, capture_output=True, text=True)
    if result.returncode != 0:
        # Race: another invocation may have created it.
        if _get_container_state(name) == "running":
            logger.info("Container %s started by another invocation", name)
            return name
        raise RuntimeError(
            f"Failed to start container {name}: {result.stderr.strip()}"
        )

    logger.info("Started persistent container %s", name)

    # For DinD, wait for the inner Docker daemon to be ready.
    if docker_in_docker:
        _wait_for_dind(name)

    # For computer-use, wait for the Wayland compositor.
    if computer_use:
        _wait_for_compositor(name)

    return name


def _wait_for_dind(container_name: str, timeout: int = 30) -> None:
    """Wait for the rootless Docker daemon inside a DinD container."""
    import time

    uid = os.getuid()
    docker_host = f"unix:///tmp/runtime-{uid}/docker.sock"
    for i in range(timeout):
        result = subprocess.run(
            [
                "docker", "exec",
                "-e", f"DOCKER_HOST={docker_host}",
                container_name,
                "docker", "info",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("DinD ready in container %s after %ds", container_name, i)
            return
        time.sleep(1)
    logger.warning(
        "DinD not ready in container %s after %ds", container_name, timeout
    )


def _wait_for_compositor(container_name: str, timeout: int = 15) -> None:
    """Wait for labwc to create the Wayland socket inside the container."""
    import time

    uid = os.getuid()
    wayland_socket = f"/tmp/runtime-{uid}/wayland-0"
    for i in range(timeout * 5):  # Check every 0.2s
        result = subprocess.run(
            ["docker", "exec", container_name, "test", "-S", wayland_socket],
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info(
                "Compositor ready in container %s after %.1fs",
                container_name,
                i * 0.2,
            )
            return
        time.sleep(0.2)
    logger.warning(
        "Compositor not ready in container %s after %ds",
        container_name,
        timeout,
    )


def build_cli_wrapper(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str = CONTAINER_IMAGE,
) -> str:
    """Generate a wrapper script that runs the Claude CLI via ``docker exec``.

    The wrapper checks if the persistent container is running and falls
    back to creating it if needed (crash recovery).  Each CLI invocation
    uses ``docker exec -i`` into the shared container.

    Args:
        context_name: Context name (used for container naming).
        project_dir: Absolute path to the project directory on the host.
        additional_directories: Optional extra directories to bind-mount.
        docker_in_docker: When True, the container runs a rootless Docker
            daemon (started once at container creation, stays warm).
        image_name: Docker image to use.

    Returns:
        Absolute path to the generated wrapper script.
    """
    container_name = _container_name(context_name)

    # Build the docker run argv for the fallback creation path.
    # This is embedded in the wrapper so it can self-heal if the
    # container was removed externally.
    docker_run_argv, _ = _build_docker_run_argv(
        context_name=context_name,
        project_dir=project_dir,
        additional_directories=additional_directories,
        docker_in_docker=docker_in_docker,
        computer_use=computer_use,
        image_name=image_name,
    )
    quoted_run_args = " \\\n  ".join(shlex.quote(a) for a in docker_run_argv)

    # For DinD, we need to wait for dockerd after container creation and
    # set DOCKER_HOST on exec so the claude CLI can use the inner daemon.
    uid = os.getuid()
    dind_wait = ""
    dind_exec_env = ""
    if docker_in_docker:
        docker_host = f"unix:///tmp/runtime-{uid}/docker.sock"
        dind_wait = (
            f'\n    # Wait for inner Docker daemon to be ready.\n'
            f'    for _i in $(seq 1 30); do\n'
            f'      if docker exec -e DOCKER_HOST={shlex.quote(docker_host)}'
            f' {shlex.quote(container_name)} docker info > /dev/null 2>&1; then\n'
            f'        break\n'
            f'      fi\n'
            f'      sleep 1\n'
            f'    done\n'
        )
        dind_exec_env = f" -e DOCKER_HOST={shlex.quote(docker_host)}"

    compositor_wait = ""
    computer_use_exec_env = ""
    if computer_use:
        wayland_socket = f"/tmp/runtime-{uid}/wayland-0"
        compositor_wait = (
            f'\n    # Wait for Wayland compositor to be ready.\n'
            f'    for _i in $(seq 1 75); do\n'
            f'      if docker exec {shlex.quote(container_name)}'
            f' test -S {shlex.quote(wayland_socket)}; then\n'
            f'        break\n'
            f'      fi\n'
            f'      sleep 0.2\n'
            f'    done\n'
        )
        # Pass Wayland env vars so Playwright (and other child processes
        # spawned by the CLI) can optionally render in the compositor.
        computer_use_exec_env = (
            f" -e WAYLAND_DISPLAY=wayland-0"
            f" -e XDG_RUNTIME_DIR=/tmp/runtime-{uid}"
        )

    script = (
        f"#!/bin/bash\n"
        f"# Auto-generated by OpenUdang for containerized context "
        f"'{context_name}'.\n"
        f"# Do not edit — this file is recreated on each session.\n"
        f'CONTAINER={shlex.quote(container_name)}\n'
        f'\n'
        f'# Ensure the persistent container is running.\n'
        f'STATE=$(docker inspect --format '
        f"'{{{{.State.Status}}}}' \"$CONTAINER\" 2>/dev/null)\n"
        f'if [ "$STATE" != "running" ]; then\n'
        f'    docker rm -f "$CONTAINER" 2>/dev/null\n'
        f'    DOCKER_RUN_ARGS=(\n'
        f'      {quoted_run_args}\n'
        f'    )\n'
        f'    "${{DOCKER_RUN_ARGS[@]}}" || {{\n'
        f'        # Race: another invocation may have started the container.\n'
        f'        sleep 0.5\n'
        f'        STATE=$(docker inspect --format '
        f"'{{{{.State.Status}}}}' \"$CONTAINER\" 2>/dev/null)\n"
        f'        if [ "$STATE" != "running" ]; then\n'
        f'            echo "Failed to start container $CONTAINER" >&2\n'
        f'            exit 1\n'
        f'        fi\n'
        f'    }}'
        f'{dind_wait}'
        f'{compositor_wait}\n'
        f'fi\n'
        f'\n'
        f'exec docker exec -i \\\n'
        f'  -e ANTHROPIC_API_KEY{dind_exec_env}{computer_use_exec_env} \\\n'
        f'  "$CONTAINER" \\\n'
        f'  /usr/local/bin/claude "$@"\n'
    )

    fd, wrapper_path = tempfile.mkstemp(
        prefix=f"openudang-docker-{context_name}-",
        suffix=".sh",
    )
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(wrapper_path, stat.S_IRWXU)

    logger.info(
        "Generated Docker exec wrapper for context '%s'%s: %s",
        context_name,
        (" (DinD + computer-use)" if docker_in_docker and computer_use
         else " (DinD)" if docker_in_docker
         else " (computer-use)" if computer_use else ""),
        wrapper_path,
    )
    return wrapper_path


def cleanup_wrapper(wrapper_path: str) -> None:
    """Remove a previously generated wrapper script."""
    try:
        os.unlink(wrapper_path)
    except OSError:
        logger.debug("Failed to remove wrapper script: %s", wrapper_path)
