"""Shared derivation of public HTTP/WebSocket bases and the server label.

Several web surfaces (review, security-key relay, port-forward relay, VNC)
need to turn the configured ``review`` host/port/public_url into a base URL,
its WebSocket equivalent, and a human-facing server name.  Keeping this in
one place avoids the base-URL logic drifting between modules.
"""

from __future__ import annotations

from urllib.parse import urlparse

from open_shrimp.config import Config


def public_base(config: Config) -> str:
    if config.review.public_url:
        return config.review.public_url.rstrip("/")
    return f"https://{config.review.host}:{config.review.port}"


def phone_websocket_base(config: Config) -> str:
    base = public_base(config)
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


def is_displayable_host(host: str | None) -> bool:
    return bool(host and host not in {"0.0.0.0", "::", "*"})


def openshrimp_server_label(config: Config) -> str:
    if config.instance_name:
        return config.instance_name
    if config.review.public_url:
        parsed = urlparse(config.review.public_url)
        if is_displayable_host(parsed.hostname):
            return parsed.hostname or "OpenShrimp"
    if is_displayable_host(config.review.host):
        return config.review.host
    return "OpenShrimp"
