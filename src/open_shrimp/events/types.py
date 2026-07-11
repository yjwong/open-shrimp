"""Event type shared by all inbound event source adapters."""

from dataclasses import dataclass


@dataclass
class Event:
    source: str  # config source name, e.g. "lark"
    sender: str | None  # human-readable sender ("Alice", "group Foo / Bob")
    text: str | None  # extracted plain text, if the payload had one
    raw: dict | None  # full payload for the JSON fallback
    dedup_key: str | None = None  # platform event/message id
    # Adapter-specific routing for replying to this event (opaque to
    # everything but the adapter's ``reply``); None if the source can't
    # route a reply back.
    reply_ref: dict | None = None
