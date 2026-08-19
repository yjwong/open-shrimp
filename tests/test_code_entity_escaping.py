"""Text inside a Telegram code entity is escaped by ``escape_code``.

Measured against the live Bot API, because the documented rule and the
observed behaviour differ, and only one of the two differences matters.

Telegram is *lenient* about over-escaping inside a ``pre``/``code``
entity: ``\\+``, ``\\-`` and ``\\.`` are stripped and render correctly, so
the prose escaper is harmless there.  It is the one character the prose
escaper does **not** escape that bites — ``\\`` is absent from its set, so
Telegram consumes it:

    escape_code    ->  directory: C:\\\\Users\\\\ada\\\\my-project   (correct)
    prose escaper  ->  directory: C:Usersadamy-project    (wrong)

An approval card exists to show a person exactly what they are agreeing
to, so a path that renders as something other than the path being used is
the one failure it cannot have.  Every Windows context directory is such
a path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from open_shrimp.handlers.utils import _escape_mdv2
from open_shrimp.markdown import escape_code

WINDOWS_PATH = r"C:\Users\ada\my-project"

SRC = Path(__file__).resolve().parent.parent / "src" / "open_shrimp"

# ``  `{_escape_mdv2(x)}`  `` — the prose escaper inside an inline code span.
_INLINE_PROSE = re.compile(r"`\{_escape_mdv2\(")

# ```lang\n{_escape_mdv2(x)}\n``` — the prose escaper inside a fence.
_FENCE_PROSE = re.compile(r"```[a-z]*\\n\{_escape_mdv2\(")


def test_the_two_escapers_differ_only_on_a_backslash():
    assert escape_code(WINDOWS_PATH) == r"C:\\Users\\ada\\my-project"
    # The prose escaper leaves the backslash alone, which is what makes
    # Telegram eat it.
    assert "\\\\" not in _escape_mdv2(WINDOWS_PATH)


def test_a_diff_survives_escape_code_unchanged_apart_from_backslashes():
    diff = "@@ -1 +1 @@\n-  old: 1.5\n+  new: 2.0"
    assert escape_code(diff) == diff


def test_no_code_entity_in_the_tree_uses_the_prose_escaper():
    """The rule, enforced mechanically rather than remembered.

    Whether a given value can contain a backslash today is not the test:
    ``escape_code`` is correct inside a code entity by definition, and a
    site that gets it wrong is invisible until someone points a context
    at a Windows directory.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _INLINE_PROSE.search(line) or _FENCE_PROSE.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert not offenders, (
        "these render inside a Telegram code entity and must use "
        "markdown.escape_code:\n" + "\n".join(offenders)
    )


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
