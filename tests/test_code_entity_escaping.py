"""A Windows path renders as written, on both message formats.

An approval card exists to show a person exactly what they are agreeing
to, so a path that renders as something other than the path being used is
the one failure it cannot have.  Every Windows context directory is such
a path, and the two formats need opposite treatment to get there.

MarkdownV2 consumes a lone backslash as an escape character, so every one
has to leave doubled:

    doubled  ->  directory: C:\\\\Users\\\\ada\\\\my-project   (correct)
    lone     ->  directory: C:Usersadamy-project    (wrong)

A rich message takes a fenced block literally, so the same doubling would
show the user two backslashes where the path has one.
"""

from __future__ import annotations

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
    """A rich fence is taken literally, so the path goes in undoubled.

    The MarkdownV2 requirement inverts here: doubling a backslash for a rich
    message shows the user two of them.
    """
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
            "Bash", {"command": rf"copy {WINDOWS_PATH} D:\c"}, None,
        )

    assert "\\\\" not in text
    assert WINDOWS_PATH in text


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
    assert WINDOWS_PATH in text
    assert "\\\\" not in text


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
    # The fence outgrows the longest backtick run in the payload, so the
    # payload's own triple cannot close it.
    assert body.startswith("**📥 lark · someone**\n````json\n")
    assert body.endswith("\n````")
    assert "```\\n*not bold*\\n```" in body
    # json.dumps doubles the backslash to represent one, and the fence takes
    # what it is given, so the JSON reads as json.dumps wrote it.
    assert r"C:\\tmp" in body
