"""A backslash reaches Telegram doubled, whichever escaper ran.

Measured against the live Bot API, because the documented rule and the
observed behaviour differ, and only one of the two differences matters.

Telegram is *lenient* about over-escaping inside a ``pre``/``code``
entity: ``\\+``, ``\\-`` and ``\\.`` are stripped and render correctly, so
reaching for the prose escaper inside a code span costs bytes, not
meaning.  It is an escaper that omits ``\\`` from its set that bites —
Telegram reads the lone backslash as an escape and consumes it:

    doubled  ->  directory: C:\\\\Users\\\\ada\\\\my-project   (correct)
    lone     ->  directory: C:Usersadamy-project    (wrong)

An approval card exists to show a person exactly what they are agreeing
to, so a path that renders as something other than the path being used is
the one failure it cannot have.  Every Windows context directory is such
a path.
"""

from __future__ import annotations

import re

import pytest

from open_shrimp import markdown
from open_shrimp.markdown import escape, escape_code

WINDOWS_PATH = r"C:\Users\ada\my-project"

_ESCAPERS = [
    (name, fn)
    for name, fn in sorted(vars(markdown).items())
    if name.startswith("escape") and callable(fn)
]


@pytest.mark.parametrize(
    "escaper", [fn for _, fn in _ESCAPERS], ids=[name for name, _ in _ESCAPERS]
)
def test_every_escaper_doubles_a_backslash(escaper):
    """The property that makes the choice between them a matter of bytes.

    Enumerated from the module rather than listed, so an escaper added
    later is held to it without anyone remembering to come back here.
    """
    assert escaper(WINDOWS_PATH).count("\\\\") == 3


def test_the_two_escapers_differ_only_in_how_much_they_escape():
    assert escape_code(WINDOWS_PATH) == r"C:\\Users\\ada\\my-project"
    assert escape(WINDOWS_PATH) == r"C:\\Users\\ada\\my\-project"


def test_a_diff_survives_escape_code_unchanged_apart_from_backslashes():
    diff = "@@ -1 +1 @@\n-  old: 1.5\n+  new: 2.0"
    assert escape_code(diff) == diff


@pytest.mark.parametrize(
    "render",
    [
        pytest.param("edit", id="Edit approval"),
        pytest.param("write", id="Write approval"),
        pytest.param("bash", id="Bash approval"),
    ],
)
def test_an_approval_card_shows_a_windows_path_as_written(render: str):
    from open_shrimp.backend.claude_sdk.policy import ClaudeSdkPolicy

    policy = ClaudeSdkPolicy()
    if render == "edit":
        text = policy.format_approval_text(
            "Edit",
            {
                "file_path": WINDOWS_PATH + r"\app.py",
                "old_string": r"path = 'a\b'",
                "new_string": r"path = 'a\c'",
            },
            None,
        )
    elif render == "write":
        text = policy.format_approval_text(
            "Write",
            {"file_path": WINDOWS_PATH, "content": r"root = C:\srv"},
            None,
        )
    else:
        text = policy.format_approval_text(
            "Bash", {"command": r"copy C:\a\b D:\c"}, None,
        )

    # Inside the fence every backslash must be doubled: a lone one is the
    # form Telegram eats.
    fenced = text.split("```")[-2]
    assert "\\\\" in fenced
    assert not re.search(r"(?<!\\)\\(?!\\)", fenced)


def test_an_opencode_card_shows_a_windows_path_as_written():
    from open_shrimp.backend.opencode.policy import OpenCodePolicy

    text = OpenCodePolicy().format_approval_text(
        "edit",
        {
            "filePath": WINDOWS_PATH + r"\app.py",
            "oldString": r"a\b",
            "newString": r"a\c",
        },
        None,
    )
    assert "\\\\" in text


def test_an_inbound_payload_cannot_break_out_of_its_code_block():
    # The one string rendered here that nobody vetted: a provider's own
    # payload.  A backtick run in it would close the fence and let the
    # remainder render as markup.
    from open_shrimp.events.sink import _render
    from open_shrimp.events.types import Event

    event = Event(
        source="lark",
        sender="someone",
        text=None,
        raw={"note": "```\n*not bold*\n```", "win": r"C:\tmp"},
    )
    body = _render(event)[0]
    # The payload's own backticks are escaped, so the only unescaped pair
    # is the fence the sink opened and closed.
    assert body.count("```") == 2
    assert "*not bold*" in body
    # json.dumps already doubled the backslash to represent one; escape_code
    # doubles each of those again, so Telegram renders the JSON as written.
    assert r"C:\\\\tmp" in body
