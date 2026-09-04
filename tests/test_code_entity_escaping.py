"""A Windows path renders as written.

An approval card exists to show a person exactly what they are agreeing
to, so a path that renders as something other than the path being used is
the one failure it cannot have.  Every Windows context directory is such
a path.

Two mechanisms get it there, and they pull in opposite directions.  A fence
is taken literally, so a path inside one goes in untouched — doubling a
backslash there would show the user two.  Prose is not, so a path in prose
leaves escaped, or the parser eats the backslash and shows
``C:Usersadamy-project``.
"""

from __future__ import annotations

import pytest

from open_shrimp import markdown
from open_shrimp.markdown import escape_rich, rich_code_block

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
    """The property that keeps a path in prose intact.

    Enumerated from the module rather than listed, so an escaper added
    later is held to it without anyone remembering to come back here.
    """
    assert escaper(WINDOWS_PATH).count("\\\\") == 3


def test_escape_rich_leaves_the_path_readable():
    assert escape_rich(WINDOWS_PATH) == r"C:\\Users\\ada\\my-project"


def test_a_fence_takes_a_diff_exactly_as_written():
    """Escaping inside a fence would print the backslashes."""
    diff = "@@ -1 +1 @@\n-  old: 1.5\n+  new: 2.0"
    assert rich_code_block(diff, "diff") == f"```diff\n{diff}\n```"


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
