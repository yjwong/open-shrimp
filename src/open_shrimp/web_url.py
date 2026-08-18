"""Shared derivation of public HTTP/WebSocket bases and the server label.

Several web surfaces (review, security-key relay, port-forward relay, VNC)
need to turn the configured ``review`` host/port/public_url into a base URL,
its WebSocket equivalent, and a human-facing server name.  Keeping this in
one place avoids the base-URL logic drifting between modules.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from open_shrimp.config import Config


def public_base(config: Config) -> str:
    if config.review.public_url:
        return config.review.public_url.rstrip("/")
    return f"https://{config.review.host}:{config.review.port}"


# Suffixes that name something on the operator's own network by definition.
# Not a filter on private addressing in general — a name can resolve anywhere,
# which is why the readiness probe resolves it rather than trusting this.
_LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")


def is_public_base(config: Config) -> bool:
    """Whether the Mini App base is an address Telegram could open.

    Telegram loads a Mini App from its own servers, so a loopback or
    private-network base renders as a perfectly normal button that does
    nothing at all when tapped — which is what a quick tunnel that failed to
    start leaves behind.

    An unset ``public_url`` is never public: :func:`public_base` then builds
    the address from ``review.host``/``port``, whose ``https`` is a fiction —
    that listener speaks plain HTTP, and TLS, where there is any, is
    terminated by whatever would have set ``public_url``.

    Cheap and synchronous, so a command handler can ask before it renders a
    button.  It answers from the URL alone: a name that resolves onto the
    operator's own network passes here and is caught by the readiness probe,
    which can afford to resolve it.
    """
    if not config.review.public_url:
        return False
    parsed = urlparse(public_base(config))
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        # is_global, and not ``not is_private``: the two are not complements
        # before 3.12.4, where 100.64/10 is absent from the private networks
        # and so reads as neither — which would pass every address a tailnet
        # hands out, the case a self-hoster is likeliest to be in.
        return ip_address(parsed.hostname).is_global
    except ValueError:
        return parsed.hostname != "localhost" and not parsed.hostname.endswith(
            _LOCAL_SUFFIXES
        )


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
