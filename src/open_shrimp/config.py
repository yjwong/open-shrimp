"""Config loading and validation for OpenShrimp."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_path


DEFAULT_CONFIG_PATH = user_config_path("openshrimp") / "config.yaml"


@dataclass
class TelegramConfig:
    token: str


@dataclass
class ContainerConfig:
    enabled: bool = True
    docker_in_docker: bool = False
    dockerfile: str | None = None
    computer_use: bool = False


@dataclass
class SandboxConfig:
    """Unified sandbox configuration for all backends."""

    backend: str  # "docker", "libvirt", "macos"
    enabled: bool = True

    # Docker-specific
    docker_in_docker: bool = False
    dockerfile: str | None = None
    computer_use: bool = False


# Valid values for sandbox config fields.
_SANDBOX_BACKENDS = {"docker", "libvirt", "macos"}


@dataclass
class ContextConfig:
    directory: str
    description: str
    allowed_tools: list[str]
    model: str | None = None
    additional_directories: list[str] = field(default_factory=list)
    default_for_chats: list[int] = field(default_factory=list)
    locked_for_chats: list[int] = field(default_factory=list)
    container: ContainerConfig | None = None
    sandbox: SandboxConfig | None = None


@dataclass
class ReviewConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str | None = None
    tunnel: str | None = None  # "cloudflared" or None


@dataclass
class Config:
    telegram: TelegramConfig
    allowed_users: list[int]
    contexts: dict[str, ContextConfig]
    default_context: str
    review: ReviewConfig = field(default_factory=ReviewConfig)
    instance_name: str | None = None


def _validate_raw(raw: dict) -> None:
    """Validate raw YAML dict has all required fields."""
    if not isinstance(raw, dict):
        raise ValueError("Config must be a YAML mapping")

    # Top-level required fields
    for key in ("telegram", "allowed_users", "contexts", "default_context"):
        if key not in raw:
            raise ValueError(f"Missing required config field: {key}")

    # telegram.token
    telegram = raw["telegram"]
    if not isinstance(telegram, dict) or "token" not in telegram:
        raise ValueError("Missing required field: telegram.token")

    # allowed_users
    users = raw["allowed_users"]
    if not isinstance(users, list) or not users:
        raise ValueError("allowed_users must be a non-empty list of integers")
    for u in users:
        if not isinstance(u, int):
            raise ValueError(f"allowed_users entries must be integers, got: {u!r}")

    # contexts
    contexts = raw["contexts"]
    if not isinstance(contexts, dict) or not contexts:
        raise ValueError("contexts must be a non-empty mapping")
    for name, ctx in contexts.items():
        if not isinstance(ctx, dict):
            raise ValueError(f"Context '{name}' must be a mapping")
        for field_name in ("directory", "description", "allowed_tools"):
            if field_name not in ctx:
                raise ValueError(
                    f"Context '{name}' missing required field: {field_name}"
                )
        if not isinstance(ctx["allowed_tools"], list):
            raise ValueError(f"Context '{name}': allowed_tools must be a list")
        add_dirs = ctx.get("additional_directories", [])
        if not isinstance(add_dirs, list):
            raise ValueError(
                f"Context '{name}': additional_directories must be a list"
            )
        for d in add_dirs:
            if not isinstance(d, str):
                raise ValueError(
                    f"Context '{name}': additional_directories entries must "
                    f"be strings, got: {d!r}"
                )

    # Validate container config
    for name, ctx in contexts.items():
        container = ctx.get("container")
        if container is not None:
            if not isinstance(container, (dict, bool)):
                raise ValueError(
                    f"Context '{name}': container must be a mapping or boolean"
                )
            if isinstance(container, dict):
                dockerfile = container.get("dockerfile")
                if dockerfile is not None and not isinstance(dockerfile, str):
                    raise ValueError(
                        f"Context '{name}': container.dockerfile must be "
                        f"a string"
                    )

    # Validate sandbox config
    for name, ctx in contexts.items():
        sandbox = ctx.get("sandbox")
        if sandbox is None:
            continue

        # Cannot specify both container and sandbox
        if ctx.get("container") is not None:
            raise ValueError(
                f"Context '{name}': cannot specify both 'container' and "
                f"'sandbox' — use 'sandbox' (the 'container' key is a "
                f"backwards-compatible alias for sandbox.backend: docker)"
            )

        if not isinstance(sandbox, dict):
            raise ValueError(
                f"Context '{name}': sandbox must be a mapping"
            )

        backend = sandbox.get("backend")
        if backend is None:
            raise ValueError(
                f"Context '{name}': sandbox.backend is required"
            )
        if backend not in _SANDBOX_BACKENDS:
            raise ValueError(
                f"Context '{name}': sandbox.backend must be one of "
                f"{sorted(_SANDBOX_BACKENDS)}, got: {backend!r}"
            )

        dockerfile = sandbox.get("dockerfile")
        if dockerfile is not None and not isinstance(dockerfile, str):
            raise ValueError(
                f"Context '{name}': sandbox.dockerfile must be a string"
            )

    # default_context references a defined context
    default = raw["default_context"]
    if default not in contexts:
        raise ValueError(
            f"default_context '{default}' not found in contexts: "
            f"{list(contexts.keys())}"
        )


def _parse_sandbox_config(raw: dict) -> SandboxConfig:
    """Parse a sandbox config dict into a SandboxConfig dataclass."""
    return SandboxConfig(
        backend=raw["backend"],
        enabled=bool(raw.get("enabled", True)),
        docker_in_docker=bool(raw.get("docker_in_docker", False)),
        dockerfile=raw.get("dockerfile"),
        computer_use=bool(raw.get("computer_use", False)),
    )


def _parse(raw: dict) -> Config:
    """Parse validated raw dict into Config dataclass."""
    contexts = {}
    for name, ctx in raw["contexts"].items():
        # Parse container config: presence of the key implies enabled.
        container_raw = ctx.get("container")
        container: ContainerConfig | None = None
        sandbox: SandboxConfig | None = None

        if container_raw is not None:
            if isinstance(container_raw, dict):
                container = ContainerConfig(
                    enabled=bool(container_raw.get("enabled", True)),
                    docker_in_docker=bool(
                        container_raw.get("docker_in_docker", False)
                    ),
                    dockerfile=container_raw.get("dockerfile"),
                    computer_use=bool(
                        container_raw.get("computer_use", False)
                    ),
                )
            else:
                # e.g. `container: true` as shorthand
                container = ContainerConfig(enabled=bool(container_raw))

            # Also create a SandboxConfig from the container config
            # for forward compatibility.
            sandbox = SandboxConfig(
                backend="docker",
                enabled=container.enabled,
                docker_in_docker=container.docker_in_docker,
                dockerfile=container.dockerfile,
                computer_use=container.computer_use,
            )

        # Parse sandbox config (new-style, takes precedence).
        sandbox_raw = ctx.get("sandbox")
        if sandbox_raw is not None:
            sandbox = _parse_sandbox_config(sandbox_raw)
            # Also populate ContainerConfig for backward compatibility
            # when the backend is Docker.
            if sandbox.backend == "docker":
                container = ContainerConfig(
                    enabled=sandbox.enabled,
                    docker_in_docker=sandbox.docker_in_docker,
                    dockerfile=sandbox.dockerfile,
                    computer_use=sandbox.computer_use,
                )

        contexts[name] = ContextConfig(
            directory=ctx["directory"],
            description=ctx["description"],
            allowed_tools=ctx["allowed_tools"],
            model=ctx.get("model"),
            additional_directories=ctx.get("additional_directories", []),
            default_for_chats=ctx.get("default_for_chats", []),
            locked_for_chats=ctx.get("locked_for_chats", []),
            container=container,
            sandbox=sandbox,
        )

    # Parse optional review config.
    review_raw: dict[str, Any] = raw.get("review", {})
    tunnel_raw = review_raw.get("tunnel")
    if tunnel_raw is not None and tunnel_raw not in ("cloudflared",):
        raise ValueError(
            f"Unsupported review.tunnel value: {tunnel_raw!r} "
            f"(supported: 'cloudflared')"
        )

    review = ReviewConfig(
        host=str(review_raw.get("host", "127.0.0.1")),
        port=int(review_raw.get("port", 8080)),
        public_url=review_raw.get("public_url"),
        tunnel=tunnel_raw,
    )

    return Config(
        telegram=TelegramConfig(token=raw["telegram"]["token"]),
        allowed_users=raw["allowed_users"],
        contexts=contexts,
        default_context=raw["default_context"],
        review=review,
        instance_name=raw.get("instance_name"),
    )


def load_config(path: str | None = None) -> Config:
    """Load and validate config from a YAML file.

    Args:
        path: Path to config file. Defaults to platform-specific config directory.

    Returns:
        Parsed and validated Config.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config is invalid.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text())
    _validate_raw(raw)
    return _parse(raw)


def write_config(config_path: Path, config_dict: dict[str, Any]) -> None:
    """Write a config dictionary to a YAML file.

    Creates parent directories if needed.

    Args:
        config_path: Path to write the config file.
        config_dict: Config dictionary matching the expected YAML schema.

    Raises:
        OSError: If the file cannot be written.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.dump(config_dict, default_flow_style=False, sort_keys=False)
    )
