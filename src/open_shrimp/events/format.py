"""Rendering primitives shared by the sources that draw a transcript.

An adapter owns its own payload shape and its own card, and two of them
drawing a conversation is not a reason to merge the two renderers.  What is
shared is smaller than that: turning a message's epoch-milliseconds into
something a person reads, and counting messages in a header line.  Both were
written twice before this, in forms that could silently disagree about a
malformed timestamp.
"""

from datetime import datetime

STAMP_FORMAT = "%Y-%m-%d %H:%M"
# How much of a stamp is the date, for a card that shows days rather than
# minutes.
DATE_CHARS = len("2026-08-14")


def stamp_millis(value: object) -> str | None:
    """Epoch milliseconds as local wall-clock text, or None for anything else.

    None covers every way a store can hand over a timestamp that names no
    instant: absent, non-numeric, zero, or past what the platform's clock can
    represent.
    """
    try:
        millis = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000).strftime(STAMP_FORMAT)
    except (OverflowError, OSError, ValueError):
        return None
