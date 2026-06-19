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
import stat
import subprocess
import tempfile
from importlib.resources import files as _pkg_files
from pathlib import Path

from open_shrimp.paths import data_dir as _data_dir
from open_shrimp.sandbox.agent_runtime import GuestMount, ImageBundle
from open_shrimp.sandbox.skill_paths import existing_global_skill_dirs

logger = logging.getLogger(__name__)

def _image_created_ts(image_name: str) -> str | None:
    """Return the creation timestamp of a Docker image, or None if missing."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_name,
         "--format", "{{.Created}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _container_image_id(container_name: str) -> str | None:
    """Return the image ID a running container was created from."""
    result = subprocess.run(
        ["docker", "inspect", container_name,
         "--format", "{{.Image}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _image_id(image_name: str) -> str | None:
    """Return the ID of a Docker image."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_name,
         "--format", "{{.Id}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# Live image-prefix root, rewritten by ``DockerSandboxManager.set_instance_prefix``
# so per-instance deployments don't collide on image names.  Image tags are
# derived as ``f"{_IMAGE_PREFIX}-{tag_suffix}:latest"``.
_IMAGE_PREFIX = "openshrimp"

# Back-compat alias for the wrapped-CLI bundle's live tag (the default
# bundle).  Code that pre-dates the bundle plumbing (tests, computer-use
# helpers) keeps reading this name; the value is kept in sync by
# ``set_image_prefix``.
CONTAINER_IMAGE = f"{_IMAGE_PREFIX}-claude:latest"

# Docker image name for computer-use (GUI) contexts.
COMPUTER_USE_IMAGE = f"{_IMAGE_PREFIX}-computer-use:latest"


def set_image_prefix(prefix: str) -> None:
    """Update the live image-name prefix.

    Called by ``DockerSandboxManager.set_instance_prefix`` so that, for
    multi-instance deployments, image tags are namespaced.  Every bundle's
    tag is derived from this prefix + the bundle's ``tag_suffix``.
    """
    global _IMAGE_PREFIX, CONTAINER_IMAGE, COMPUTER_USE_IMAGE
    _IMAGE_PREFIX = prefix
    CONTAINER_IMAGE = f"{prefix}-claude:latest"
    COMPUTER_USE_IMAGE = f"{prefix}-computer-use:latest"


def claude_image_bundle() -> ImageBundle:
    """Construct the wrapped-CLI (Claude) :class:`ImageBundle`.

    The computer-use image build, the PTY+JSONL spike, and tests call this
    explicitly when they need the Claude base image; production sandboxes
    receive a bundle from the runtime and never invoke this.  Resolved
    lazily so this module's import stays light (no SDK imports at load).
    """
    from open_shrimp.backend.claude_sdk.binary import find_claude_binary

    return ImageBundle(
        tag_suffix="claude",
        bundled_dockerfile="Dockerfile.claude",
        binary_finder=find_claude_binary,
        context_binary_name="claude",
        build_arg=("CLAUDE_CLI", "claude"),
        guest_home="/home/claude",
        dind_user="claude",
    )


def _base_image_for(bundle: ImageBundle) -> str:
    """Return the live base image tag for *bundle*.

    The current ``_IMAGE_PREFIX`` (rewritten by ``set_image_prefix``)
    namespaces the tag for multi-instance deployments.
    """
    return f"{_IMAGE_PREFIX}-{bundle.tag_suffix}:latest"

# Base directory for per-context container state (session storage, etc.).
def container_state_dir() -> Path:
    """Return the base directory for per-context Docker sandbox state."""
    return _data_dir() / "containers"


# Custom seccomp profile for DinD: Docker's default + keyctl (inner runc
# session keyrings) + pivot_root (inner container rootfs setup).
def _find_seccomp_profile() -> Path:
    """Locate the DinD seccomp profile.

    Tries the repo root first (dev/editable installs), then falls back to
    importlib.resources (installed wheels/PyApp).  The profile must be
    written to a real file on disk because ``docker run --security-opt
    seccomp=`` requires a filesystem path.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    repo_profile = repo_root / "seccomp-dind.json"
    if repo_profile.is_file():
        return repo_profile

    # Installed wheel / PyApp — extract via importlib.resources.
    pkg_profile = _pkg_files("open_shrimp").joinpath("seccomp-dind.json")
    # importlib.resources may return a MultiplexedPath or similar; we need
    # a real filesystem path for docker's --security-opt.
    if hasattr(pkg_profile, "is_file") and pkg_profile.is_file():
        return Path(str(pkg_profile))

    # As a last resort try importlib.resources.as_file for zip-backed resources.
    from importlib.resources import as_file
    with as_file(pkg_profile) as p:
        # Copy to a persistent temp location so docker can read it after
        # the context manager exits.
        persistent = Path(tempfile.gettempdir()) / "openshrimp-seccomp-dind.json"
        if not persistent.exists():
            shutil.copy2(p, persistent)
        return persistent


def ensure_image(
    *,
    bundle: ImageBundle,
    image_name: str | None = None,
    dockerfile: str | None = None,
    base_image: str | None = None,
    log_file: Path | None = None,
) -> None:
    """Ensure the container image exists, building it if necessary.

    *bundle* carries every per-image build input — the bundled Dockerfile,
    the binary finder, the build-arg and the binary name copied into the
    build context — so this function has no agent-name branching.

    Args:
        bundle: The :class:`ImageBundle` describing how to build this image.
            Required so callers state the agent explicitly; ``ensure_image``
            never assumes a default.
        image_name: Docker image tag to build/check.  Defaults to the
            bundle's base tag (``_base_image_for(bundle)``).
        dockerfile: Optional path to a custom Dockerfile.  When set,
            the build context is the directory containing the
            Dockerfile (so ``COPY`` instructions work relative to it).
        base_image: When set with a custom *dockerfile*, ensure this
            image exists (instead of the default base) before building.
            Useful for layering a custom Dockerfile on top of the
            computer-use image.

    Raises:
        RuntimeError: If the agent binary cannot be found or if the Docker
            build fails.
    """
    base_default = _base_image_for(bundle)
    if image_name is None:
        image_name = base_default

    image_exists = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
    ).returncode == 0

    # Check if the base image is newer than the derived image.
    needs_rebuild = False
    if image_exists and dockerfile is not None:
        effective_base = base_image or (
            base_default if image_name != base_default else None
        )
        if effective_base:
            base_ts = _image_created_ts(effective_base)
            derived_ts = _image_created_ts(image_name)
            if base_ts and derived_ts and base_ts > derived_ts:
                logger.info(
                    "Base image %s (%s) is newer than %s (%s), rebuilding",
                    effective_base, base_ts, image_name, derived_ts,
                )
                needs_rebuild = True

    if image_exists and not needs_rebuild:
        logger.info("Container image %s already exists", image_name)
        return

    if needs_rebuild:
        logger.info("Rebuilding container image %s...", image_name)
    else:
        logger.info("Container image %s not found, building...", image_name)

    cli_binary = bundle.binary_finder()
    logger.info("Using %s binary: %s", bundle.context_binary_name, cli_binary)

    if dockerfile is not None:
        # Ensure the base image exists before building a custom image
        # that likely depends on it (e.g. FROM openshrimp-claude:latest).
        if base_image:
            # Caller explicitly specified which base to ensure (e.g.
            # the computer-use image).  That base's own dependencies
            # should already be satisfied by the caller.
            subprocess.run(
                ["docker", "image", "inspect", base_image],
                capture_output=True,
                check=True,
            )
        elif image_name != base_default:
            ensure_image(image_name=base_default, dockerfile=None, bundle=bundle)

        # Custom Dockerfile: use its parent directory as the build
        # context, copying the CLI binary in alongside it.
        dockerfile_path = Path(dockerfile).resolve()
        if not dockerfile_path.is_file():
            raise RuntimeError(
                f"Custom Dockerfile not found: {dockerfile_path}"
            )
        build_dir_path = dockerfile_path.parent
        # Copy CLI binary into the build context (if not already there).
        cli_dest = build_dir_path / bundle.context_binary_name
        if not cli_dest.exists() or not cli_dest.samefile(Path(cli_binary)):
            shutil.copy2(cli_binary, cli_dest)
        extra_args = None
        if base_image:
            extra_args = ["--build-arg", f"BASE_IMAGE={base_image}"]
        _docker_build(
            image_name=image_name,
            build_dir=str(build_dir_path),
            dockerfile_name=dockerfile_path.name,
            extra_build_args=extra_args,
            build_arg=bundle.build_arg,
            log_file=log_file,
        )
    else:
        # Default: bundled Dockerfile in a temp build context.
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        repo_dockerfile = repo_root / bundle.bundled_dockerfile
        if repo_dockerfile.is_file():
            dockerfile_text = repo_dockerfile.read_text(encoding="utf-8")
        else:
            dockerfile_text = (
                _pkg_files("open_shrimp")
                .joinpath(bundle.bundled_dockerfile)
                .read_text(encoding="utf-8")
            )

        with tempfile.TemporaryDirectory(
            prefix="openshrimp-build-"
        ) as build_dir:
            build_path = Path(build_dir)
            shutil.copy2(cli_binary, build_path / bundle.context_binary_name)
            (build_path / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
            _docker_build(
                image_name=image_name,
                build_dir=build_dir,
                build_arg=bundle.build_arg,
                log_file=log_file,
            )

    logger.info("Successfully built container image %s", image_name)


def ensure_computer_use_image(
    image_name: str = COMPUTER_USE_IMAGE,
    log_file: Path | None = None,
) -> None:
    """Ensure the computer-use container image exists, building if necessary.

    Builds the base ``openshrimp-claude`` image first (if needed), then
    layers ``Dockerfile.computer-use`` on top with labwc, wlrctl, grim,
    wayvnc, and Chromium.
    """
    image_exists = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
    ).returncode == 0

    # Check if the base image is newer than the computer-use image.
    needs_rebuild = False
    if image_exists:
        base_ts = _image_created_ts(CONTAINER_IMAGE)
        derived_ts = _image_created_ts(image_name)
        if base_ts and derived_ts and base_ts > derived_ts:
            logger.info(
                "Base image %s (%s) is newer than %s (%s), rebuilding",
                CONTAINER_IMAGE, base_ts, image_name, derived_ts,
            )
            needs_rebuild = True

    if image_exists and not needs_rebuild:
        logger.info("Computer-use image %s already exists", image_name)
        return

    # Ensure the Claude base image exists first; computer-use layers on top
    # of it.  The bundle is constructed explicitly so this call site is not
    # relying on a silent default in ``ensure_image``.
    ensure_image(
        bundle=claude_image_bundle(),
        image_name=CONTAINER_IMAGE,
        dockerfile=None,
        log_file=log_file,
    )

    if needs_rebuild:
        logger.info("Rebuilding computer-use image %s...", image_name)
    else:
        logger.info("Computer-use image %s not found, building...", image_name)

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    repo_dockerfile = repo_root / "Dockerfile.computer-use"
    computer_use_dir = repo_root / "computer-use"

    if repo_dockerfile.is_file() and computer_use_dir.is_dir():
        # Build from the repo root so COPY computer-use/* works.
        _docker_build(
            image_name=image_name,
            build_dir=str(repo_root),
            dockerfile_name="Dockerfile.computer-use",
            log_file=log_file,
        )
    else:
        # Installed wheel / PyApp — extract assets to a temp dir.
        with tempfile.TemporaryDirectory(
            prefix="openshrimp-computer-use-build-"
        ) as build_dir:
            build_path = Path(build_dir)
            pkg = _pkg_files("open_shrimp")

            # Copy Dockerfile.
            dockerfile_text = pkg.joinpath(
                "Dockerfile.computer-use"
            ).read_text(encoding="utf-8")
            (build_path / "Dockerfile.computer-use").write_text(
                dockerfile_text, encoding="utf-8",
            )

            # Copy computer-use assets.
            cu_dir = build_path / "computer-use"
            cu_dir.mkdir()
            for asset_name in ("entrypoint.sh", "rc.xml", "autostart"):
                asset = pkg.joinpath("computer-use", asset_name)
                (cu_dir / asset_name).write_text(asset.read_text(encoding="utf-8"), encoding="utf-8")

            _docker_build(
                image_name=image_name,
                build_dir=str(build_path),
                dockerfile_name="Dockerfile.computer-use",
                log_file=log_file,
            )

    logger.info("Successfully built computer-use image %s", image_name)


def _docker_build(
    *,
    image_name: str,
    build_dir: str,
    build_arg: tuple[str, str] | None = None,
    dockerfile_name: str = "Dockerfile",
    extra_build_args: list[str] | None = None,
    log_file: Path | None = None,
) -> None:
    """Run ``docker build`` and stream output to the logger.

    Args:
        build_arg: The ``(name, value)`` of the agent-binary build arg the
            Dockerfile reads (e.g. ``("CLAUDE_CLI", "claude")``).  ``None``
            for derived Dockerfiles that take no ARG (e.g. the computer-use
            image, which only ``FROM``s the already-built base).
        log_file: Optional path to a file where build output is also
            written line-by-line (with flush) for the terminal mini app.

    Raises:
        RuntimeError: If the build fails.
    """
    cmd = [
        "docker", "build",
        "-t", image_name,
        "-f", dockerfile_name,
    ]
    if build_arg is not None:
        cmd.extend(["--build-arg", f"{build_arg[0]}={build_arg[1]}"])
    if extra_build_args:
        cmd.extend(extra_build_args)
    cmd.append(".")
    process = subprocess.Popen(
        cmd,
        cwd=build_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines: list[str] = []
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            logger.info("docker build: %s", line)
            if log_fh is not None:
                log_fh.write(line + "\n")
                log_fh.flush()
        returncode = process.wait()
    finally:
        if log_fh is not None:
            log_fh.close()

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
    state_dir = container_state_dir() / context_name
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def check_docker_available() -> bool:
    """Return True if Docker is available on the host."""
    return shutil.which("docker") is not None


def get_screenshots_dir(context_name: str) -> Path:
    """Return the host-side screenshots directory for a computer-use context."""
    return container_state_dir() / context_name / "screenshots"


def get_text_input_state_path(context_name: str) -> Path:
    """Return the host-side text-input-state file for a computer-use context."""
    return container_state_dir() / context_name / "text-input-state"


def get_text_input_active(context_name: str) -> bool:
    """Check if a text input field is focused inside a computer-use container.

    Reads the bind-mounted text-input-state file written by seat-keyboard's
    input-method-v2 monitor.  Returns True if active, False otherwise.
    """
    path = get_text_input_state_path(context_name)
    try:
        return path.read_text(encoding="utf-8").strip() == "1"
    except (FileNotFoundError, OSError):
        return False


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

# These globals are set by ``DockerSandboxManager.set_instance_prefix()``
# so that the free functions below (called by ``DockerSandbox``) see the
# correct prefix/label.  New code should access these values through the
# manager instead.
_CONTAINER_LABEL = "openshrimp"
_INSTANCE_PREFIX = "openshrimp"



# ---------------------------------------------------------------------------
# Persistent container lifecycle
# ---------------------------------------------------------------------------

def container_name(context_name: str) -> str:
    """Return the fixed Docker container name for a context."""
    return f"{_INSTANCE_PREFIX}-{context_name}"


# Keep private alias for internal callers.
_container_name = container_name


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

# XDG_RUNTIME_DIR is required by rootless dockerd.  It must be outside
# /run because rootlesskit's --copy-up=/run overlays /run with a tmpfs
# inside its namespace, shadowing anything the outer namespace writes there.
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

# Symlink the rootless socket to the standard path so that tools like
# Testcontainers/Ryuk find the daemon at /var/run/docker.sock without
# needing DOCKER_HOST.
mkdir -p /var/run
ln -sf "${XDG_RUNTIME_DIR}/docker.sock" /var/run/docker.sock

# Also create a Docker context so that `docker exec` sessions (which don't
# inherit runtime env vars or the symlink's XDG target) can find the daemon.
docker context create rootless --docker "host=unix://${XDG_RUNTIME_DIR}/docker.sock" 2>/dev/null || true
docker context use rootless 2>/dev/null || true

# Add masquerade rules for container outbound networking.
# rootless dockerd runs with --iptables=false (required in nested containers),
# so we must manually add NAT rules for all bridge subnets (docker0 + any
# docker-compose br-* networks created later).  Run in a background loop so
# dynamically-created networks (e.g. docker compose up) get rules too.
_nsenter() {
    CHILD_PID=$(cat "${XDG_RUNTIME_DIR}/dockerd-rootless/child_pid" 2>/dev/null)
    [ -z "$CHILD_PID" ] && return 1
    nsenter --preserve-credentials -U -n -t "$CHILD_PID" "$@"
}

ensure_masquerade() {
    _nsenter true 2>/dev/null || return
    for BRIDGE in $(_nsenter ip -o link show type bridge 2>/dev/null \
            | grep -oP '(?<=: )\S+(?=:)'); do
        SUBNET=$(_nsenter ip -4 addr show "$BRIDGE" 2>/dev/null \
            | grep -oP 'inet \K[\d./]+')
        if [ -n "$SUBNET" ]; then
            _nsenter iptables -t nat -C POSTROUTING \
                -s "$SUBNET" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || \
            _nsenter iptables -t nat -A POSTROUTING \
                -s "$SUBNET" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || true
        fi
    done
}

cleanup_masquerade() {
    _nsenter true 2>/dev/null || return
    BRIDGES=$(_nsenter ip -o link show type bridge 2>/dev/null \
        | grep -oP '(?<=: )\S+(?=:)')
    # Walk POSTROUTING rules; delete any whose output bridge no longer exists.
    _nsenter iptables -t nat -S POSTROUTING 2>/dev/null \
        | grep 'MASQUERADE' | while read -r RULE; do
        RULE_BRIDGE=$(echo "$RULE" | sed -n 's/.* -o \([^ ]*\).*/\1/p')
        RULE_SUBNET=$(echo "$RULE" | sed -n 's/.*-s \([^ ]*\).*/\1/p')
        [ -z "$RULE_BRIDGE" ] || [ -z "$RULE_SUBNET" ] && continue
        if ! echo "$BRIDGES" | grep -qxF "$RULE_BRIDGE"; then
            _nsenter iptables -t nat -D POSTROUTING \
                -s "$RULE_SUBNET" ! -o "$RULE_BRIDGE" -j MASQUERADE \
                2>/dev/null || true
        fi
    done
}

# Run once immediately, then react to network events via docker events.
ensure_masquerade
docker events --filter type=network --format '{{.Action}}' 2>/dev/null | \
    while read -r event; do
        case "$event" in
            create|connect) ensure_masquerade ;;
            destroy) cleanup_masquerade ;;
        esac
    done &

# Keep the container alive.  CLI invocations arrive via `docker exec`.
exec sleep infinity
"""


def _dind_entrypoint_text(bundle: ImageBundle) -> str:
    """Return the standalone-DinD entrypoint script for *bundle*.

    The canonical script registers a ``claude`` user with home ``/home/claude``
    (the wrapped-CLI bundle).  For any other bundle, rewrite the three
    user/home lines (passwd, subuid, subgid) so the in-container user matches
    the bundle's ``dind_user`` + ``guest_home`` — otherwise rootless Docker's
    newuidmap setup fails (the user the entrypoint launches sub-processes as
    isn't the one declared in passwd/subuid/subgid).
    """
    if bundle.dind_user == "claude" and bundle.guest_home == "/home/claude":
        return _DIND_ENTRYPOINT
    user = bundle.dind_user
    home = bundle.guest_home
    return (
        _DIND_ENTRYPOINT
        .replace(
            'echo "claude:x:${MY_UID}:${MY_GID}::/home/claude:/bin/bash"',
            f'echo "{user}:x:${{MY_UID}}:${{MY_GID}}::{home}:/bin/bash"',
        )
        .replace('echo "claude:x:${MY_GID}:"', f'echo "{user}:x:${{MY_GID}}:"')
        .replace('echo "claude:100000:65536"', f'echo "{user}:100000:65536"')
    )


def _build_docker_run_argv(
    *,
    bundle: ImageBundle,
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str | None = None,
    served_home_mounts: tuple[GuestMount, ...] = (),
    served_guest_port: int | None = None,
) -> tuple[list[str], str]:
    """Build the ``docker run -d`` argv for creating a persistent container.

    Returns ``(docker_run_argv, container_name)`` where *docker_run_argv*
    is a complete argv list ending with the image and keep-alive command.

    *bundle* selects guest user/home, the DinD-entrypoint passwd rewrite,
    and (when no *served_guest_port* is given) the wrapped-CLI shape with
    the per-context claude-home mount and credentials copy-in.  When
    *served_guest_port* is set, the served-endpoint shape is produced:
    *served_home_mounts* are bind-mounted in, the port is published for
    ``reach()``, the unified skill dirs are sub-mounted, and the
    credentials copy-in is skipped (the runtime's ``inject`` syncs creds
    into the served home).  *image_name* defaults to the bundle's base tag.
    """
    if image_name is None:
        image_name = _base_image_for(bundle)
    served = served_guest_port is not None

    state_dir = _ensure_state_dir(context_name)
    uid = os.getuid()
    gid = os.getgid()
    container_name = _container_name(context_name)

    host_credentials = Path.home() / ".claude" / ".credentials.json"
    home_dir = bundle.guest_home

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
        "-e", f"HOME={home_dir}",
    ]
    for env_arg in git_env_args:
        docker_argv.extend(["-e", env_arg])

    # Ensure host.docker.internal resolves inside the container.
    # This is automatic on Docker Desktop but needs --add-host on
    # native/rootless Docker (Linux).  The MCP proxy listens on the
    # host and sandboxed contexts reach it via this hostname.
    docker_argv.extend(["--add-host", "host.docker.internal:host-gateway"])

    if served:
        # Served-endpoint shape: extra host dirs come from the launch's
        # ``home_mounts`` (the runtime's data home, plugin-config dir, …),
        # plus a published guest serve port so ``reach()`` (a ``docker
        # port`` lookup) can resolve a host port.  No credential copy-in
        # (the runtime's ``inject`` writes creds into the served home).
        task_tmp_dir = state_dir / "tmp"
        task_tmp_dir.mkdir(exist_ok=True)
        tmp_target = f"/tmp/{bundle.dind_user}-{uid}"
        docker_argv.extend([
            "-v", f"{project_dir}:{project_dir}",
            "-v", f"{task_tmp_dir}:{tmp_target}",
        ])
        for mount in served_home_mounts:
            docker_argv.extend([
                "-v", f"{mount.host_dir}:{mount.guest_mount_point}",
            ])
        # Expose the sandbox-owned agent server to the host via loopback.
        docker_argv.extend(["-p", f"127.0.0.1::{served_guest_port}"])
        # Unified global skill dirs; read-only sub-mounts.
        for host_skills, guest_skills in existing_global_skill_dirs(
            guest_home=home_dir,
        ):
            docker_argv.extend([
                "--mount",
                f"type=bind,source={host_skills},target={guest_skills},readonly",
            ])
    else:
        # Wrapped-CLI shape: per-context home dir mounted as the agent's
        # `.claude` (background task outputs land in the host-visible
        # state directory), credentials copied in for token refresh.
        claude_tmp_dir = state_dir / "tmp"
        claude_tmp_dir.mkdir(exist_ok=True)
        docker_argv.extend([
            "-v", f"{project_dir}:{project_dir}",
            "-v", f"{state_dir}:{home_dir}/.claude",
            "-v", f"{claude_tmp_dir}:/tmp/{bundle.dind_user}-{uid}",
        ])
        # Sub-mount on top of state_dir; Docker resolves nested binds in flag order.
        host_skills = Path.home() / ".claude" / "skills"
        if host_skills.is_dir():
            docker_argv.extend([
                "-v", f"{host_skills}:{home_dir}/.claude/skills:ro",
            ])
        # Copy credentials into the state dir (which is directory-mounted as
        # `{home_dir}/.claude`) instead of bind-mounting the file directly.
        # File bind mounts break when the host replaces the file via atomic
        # rename (new inode) — the container stays pinned to the stale inode.
        # The wrapper script also copies before each `docker exec` to pick up
        # host-side token refreshes.
        if host_credentials.exists():
            shutil.copy2(str(host_credentials), str(state_dir / ".credentials.json"))
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
            "-v", f"{docker_data_dir}:{home_dir}/.local/share/docker",
        ])
        if not computer_use:
            # Standalone DinD: use the dedicated entrypoint script.
            entrypoint_path = state_dir / "dind-entrypoint.sh"
            entrypoint_path.write_text(
                _dind_entrypoint_text(bundle), encoding="utf-8",
            )
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
        # Bind-mount text-input-state file for host-side inotify watching.
        # seat-keyboard writes "1"/"0" here when text fields gain/lose focus.
        text_input_state_file = state_dir / "text-input-state"
        text_input_state_file.touch()
        docker_argv.extend([
            "-v", f"{text_input_state_file}:/tmp/text-input-state",
        ])
        # Expose VNC port (dynamic mapping to avoid conflicts).
        docker_argv.extend(["-p", "127.0.0.1::5900"])
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
    *,
    bundle: ImageBundle,
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str | None = None,
    served_home_mounts: tuple[GuestMount, ...] = (),
    served_guest_port: int | None = None,
) -> str:
    """Ensure a persistent container is running for the given context.

    If the container already exists and is running, this is a fast no-op
    (a single ``docker inspect``).  Otherwise it creates a new detached
    container.

    Race-safe: if two threads try to create the container simultaneously,
    one will get a name-conflict error and fall through to the running
    container.

    Returns:
        The container name (e.g. ``openshrimp-dev``).
    """
    name = _container_name(context_name)
    if image_name is None:
        image_name = _base_image_for(bundle)

    state = _get_container_state(name)
    if state == "running":
        # Check if the container's image matches the current image tag.
        container_img = _container_image_id(name)
        current_img = _image_id(image_name)
        if container_img and current_img and container_img != current_img:
            logger.info(
                "Container %s is running an outdated image, recreating",
                name,
            )
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True,
            )
        else:
            logger.info("Container %s already running", name)
            return name
    elif state is not None:
        # Remove stale container (exited, dead, created).
        logger.info("Removing stale container %s (state=%s)", name, state)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    docker_argv, _ = _build_docker_run_argv(
        bundle=bundle,
        context_name=context_name,
        project_dir=project_dir,
        additional_directories=additional_directories,
        docker_in_docker=docker_in_docker,
        computer_use=computer_use,
        image_name=image_name,
        served_home_mounts=served_home_mounts,
        served_guest_port=served_guest_port,
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

    for i in range(timeout):
        result = subprocess.run(
            [
                "docker", "exec",
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
    state_dir = _ensure_state_dir(context_name)
    host_credentials = Path.home() / ".claude" / ".credentials.json"

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

    # For DinD, we need to wait for dockerd after container creation.
    # The entrypoint creates a Docker context so no DOCKER_HOST is needed.
    uid = os.getuid()
    dind_wait = ""
    if docker_in_docker:
        dind_wait = (
            f'\n    # Wait for inner Docker daemon to be ready.\n'
            f'    for _i in $(seq 1 30); do\n'
            f'      if docker exec'
            f' {shlex.quote(container_name)} docker info > /dev/null 2>&1; then\n'
            f'        break\n'
            f'      fi\n'
            f'      sleep 1\n'
            f'    done\n'
        )

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
        f"# Auto-generated by OpenShrimp for containerized context "
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
        f'# Refresh credentials in the state dir so the container sees the\n'
        f'# latest host token (avoids stale-inode bind mount issues).\n'
        f'HOST_CREDS={shlex.quote(str(host_credentials))}\n'
        f'STATE_CREDS={shlex.quote(str(state_dir / ".credentials.json"))}\n'
        f'[ -f "$HOST_CREDS" ] && cp "$HOST_CREDS" "$STATE_CREDS" 2>/dev/null\n'
        f'\n'
        f'exec docker exec -i \\\n'
        f'  -e ANTHROPIC_API_KEY{computer_use_exec_env} \\\n'
        f'  "$CONTAINER" \\\n'
        f'  /usr/local/bin/claude "$@"\n'
    )

    fd, wrapper_path = tempfile.mkstemp(
        prefix=f"openshrimp-docker-{context_name}-",
        suffix=".sh",
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
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
