"""macOS sandbox-exec support for isolated Claude CLI execution.

When a context has ``containerize: true`` and the host is macOS (where Docker
would run a Linux VM and break the native Claude CLI binary), we use Apple's
``sandbox-exec`` to restrict the CLI process to only the project directory,
session storage, and network access.

The approach mirrors ``container.py``: a thin wrapper script is generated that
invokes ``sandbox-exec -f <profile>`` with the right paths.  The SDK's
``cli_path`` option points at this wrapper, so everything else (stdin/stdout
streaming, canUseTool callbacks, MCP) works unchanged.

Note: ``sandbox-exec`` is technically deprecated by Apple but remains
functional on all current macOS versions.
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


def _find_claude_binary() -> str:
    """Find the Claude CLI binary, mirroring the SDK's resolution order.

    1. Bundled binary inside the claude_agent_sdk package
    2. System PATH via shutil.which
    3. Common installation locations
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

    # Last resort — hope it's on PATH at runtime.
    return "claude"

# Base directory for per-context sandbox state (session storage, etc.).
SANDBOX_STATE_DIR = user_data_path("openudang") / "containers"


def _ensure_state_dir(context_name: str) -> Path:
    """Create and return the sandbox state directory for a context.

    This directory serves as the Claude session storage (equivalent to
    ``~/.claude``) for the sandboxed process, giving each context its own
    isolated session history.
    """
    state_dir = SANDBOX_STATE_DIR / context_name
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def check_sandbox_available() -> bool:
    """Return True if sandbox-exec is available on the host."""
    return shutil.which("sandbox-exec") is not None


def _build_sandbox_profile(
    project_dir: str,
    state_dir: Path,
    claude_bin: str,
    additional_directories: list[str] | None = None,
) -> str:
    """Build a sandbox profile string (.sb format).

    The profile follows a deny-by-default policy and only allows:
    - Read/write to the project directory
    - Read/write to the sandbox state directory (session storage)
    - Read-only access to the host's Claude credentials
    - Read access to system libraries, executables, and frameworks
    - Process forking and execution (for subprocesses)
    - Outbound network access (for the Anthropic API)
    - Temp file access
    - Required Mach IPC for system services (DNS, dyld, etc.)
    """
    home = str(Path.home())
    host_credentials = Path.home() / ".claude" / ".credentials.json"

    rules: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        "; --- Process execution ---",
        "(allow process-fork)",
        "(allow process-exec)",
        "",
        "; --- Signals ---",
        "(allow signal (target self))",
        "",
        "; --- System read access (libraries, frameworks, binaries) ---",
        '(allow file-read* (subpath "/System"))',
        '(allow file-read* (subpath "/Library"))',
        '(allow file-read* (subpath "/usr"))',
        '(allow file-read* (subpath "/bin"))',
        '(allow file-read* (subpath "/sbin"))',
        '(allow file-read* (subpath "/opt"))',
        '(allow file-read* (subpath "/private/etc"))',
        '(allow file-read* (subpath "/dev"))',
        '(allow file-read* (subpath "/AppleInternal"))',
        f'(allow file-read* (subpath "{home}/.nix-profile"))',
        f'(allow file-read* (subpath "{home}/.local"))',
        "",
        f"; --- Claude CLI binary ---",
        f'(allow file-read* (subpath "{Path(claude_bin).parent}"))',
        "",
        "; --- Homebrew ---",
        '(allow file-read* (subpath "/opt/homebrew"))',
        '(allow file-read* (subpath "/usr/local"))',
        "",
        "; --- Nix ---",
        '(allow file-read* (subpath "/nix"))',
        '(allow file-read* (literal "/etc/nix"))',
        "",
        "; --- Project directory (read/write) ---",
        f'(allow file-read* (subpath "{project_dir}"))',
        f'(allow file-write* (subpath "{project_dir}"))',
        "",
        "; --- Session storage (read/write) ---",
        f'(allow file-read* (subpath "{state_dir}"))',
        f'(allow file-write* (subpath "{state_dir}"))',
        "",
        "; --- Host credentials (read-only) ---",
    ]
    if host_credentials.exists():
        rules.append(
            f'(allow file-read-data (literal "{host_credentials}"))'
        )
    else:
        rules.append("; (no credentials file found)")

    rules.extend([
        "",
        "; --- Temp files ---",
        '(allow file-read* (subpath "/tmp"))',
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-read* (subpath "/private/tmp"))',
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-read* (subpath "/var/tmp"))',
        '(allow file-write* (subpath "/var/tmp"))',
        '(allow file-read* (subpath "/private/var/tmp"))',
        '(allow file-write* (subpath "/private/var/tmp"))',
        '(allow file-read* (regex #"^/var/folders/"))',
        '(allow file-write* (regex #"^/var/folders/"))',
        '(allow file-read* (regex #"^/private/var/folders/"))',
        '(allow file-write* (regex #"^/private/var/folders/"))',
    ])

    # Additional directories.
    if additional_directories:
        rules.append("")
        rules.append("; --- Additional directories (read/write) ---")
        for extra_dir in additional_directories:
            rules.append(f'(allow file-read* (subpath "{extra_dir}"))')
            rules.append(f'(allow file-write* (subpath "{extra_dir}"))')

    rules.extend([
        "",
        "; --- Network (outbound for API, MCP, etc.) ---",
        "(allow network-outbound)",
        "(allow network-inbound)",  # MCP servers may listen locally
        "(allow network-bind)",
        "(allow system-socket)",
        "",
        "; --- Mach IPC (required for DNS, dyld, system services) ---",
        "(allow mach-lookup)",
        "(allow mach-register)",
        "",
        "; --- Sysctl / system info ---",
        "(allow sysctl-read)",
        "(allow sysctl-write)",  # some tools need sysctl writes
        "",
        "; --- IPC ---",
        "(allow ipc-posix-shm)",
        "(allow ipc-posix-sem)",
        "(allow ipc-posix-shm-read-data)",
        "(allow ipc-posix-shm-write-data)",
        "",
        "; --- Misc ---",
        "(allow process-info-pidinfo)",
        "(allow process-info-setcontrol)",
        "(allow process-info-dirtycontrol)",
        "(allow user-preference-read)",
        "(allow file-ioctl)",
        "",
    ])

    return "\n".join(rules) + "\n"


def build_cli_wrapper(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
) -> str:
    """Generate a wrapper script that runs the Claude CLI under sandbox-exec.

    The wrapper:
    - Writes a sandbox profile (.sb) restricting filesystem access to the
      project directory, session storage, and system paths.
    - Invokes ``sandbox-exec -f <profile> claude "$@"`` so the CLI runs
      within the sandbox.
    - Passes ``ANTHROPIC_API_KEY`` through the environment.

    Args:
        context_name: Context name (used for state directory naming).
        project_dir: Absolute path to the project directory.
        additional_directories: Optional extra directories to allow
            read/write access to.

    Returns:
        Absolute path to the generated wrapper script.
    """
    state_dir = _ensure_state_dir(context_name)

    # Find the claude binary using the same resolution logic as the SDK:
    # bundled binary first, then system PATH, then common install locations.
    claude_bin = _find_claude_binary()

    profile = _build_sandbox_profile(
        project_dir=project_dir,
        state_dir=state_dir,
        claude_bin=claude_bin,
        additional_directories=additional_directories,
    )

    # Write the sandbox profile to a temp file.
    profile_fd, profile_path = tempfile.mkstemp(
        prefix=f"openudang-sandbox-{context_name}-",
        suffix=".sb",
    )
    with os.fdopen(profile_fd, "w") as f:
        f.write(profile)

    script = (
        f"#!/bin/bash\n"
        f"# Auto-generated by OpenUdang for sandboxed context "
        f"'{context_name}'.\n"
        f"# Do not edit — this file is recreated on each session.\n"
        f'exec sandbox-exec -f "{profile_path}" "{claude_bin}" "$@"\n'
    )

    # Write the wrapper script.
    fd, wrapper_path = tempfile.mkstemp(
        prefix=f"openudang-sandbox-{context_name}-",
        suffix=".sh",
    )
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(wrapper_path, stat.S_IRWXU)

    logger.info(
        "Generated sandbox wrapper for context '%s': %s (profile: %s)",
        context_name,
        wrapper_path,
        profile_path,
    )
    return wrapper_path


def cleanup_wrapper(wrapper_path: str) -> None:
    """Remove a previously generated wrapper script and its profile."""
    # The profile path is embedded in the wrapper — extract and clean it up.
    try:
        with open(wrapper_path) as f:
            content = f.read()
        # Extract profile path from: sandbox-exec -f "<profile_path>"
        for line in content.splitlines():
            if "sandbox-exec -f" in line:
                # Parse: exec sandbox-exec -f "/path/to/profile.sb" ...
                parts = line.split('"')
                for i, part in enumerate(parts):
                    if part.endswith(".sb"):
                        try:
                            os.unlink(part)
                        except OSError:
                            logger.debug(
                                "Failed to remove sandbox profile: %s", part
                            )
                        break
    except OSError:
        logger.debug("Failed to read wrapper for profile cleanup: %s", wrapper_path)

    try:
        os.unlink(wrapper_path)
    except OSError:
        logger.debug("Failed to remove wrapper script: %s", wrapper_path)
