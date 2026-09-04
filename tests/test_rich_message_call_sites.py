"""Every rich-message call in ``src/`` binds to the function it names.

``rich_message`` exposes two editors that differ only in how the message is
addressed — ``edit_rich(bot, chat_id, message_id, text)`` and
``edit_message_rich(message, text)`` — and every call site sits inside a
``try/except Exception`` that falls back to stripping the keyboard.  A call
with the wrong arity therefore does not crash: the card silently keeps its
buttons and never gains its outcome line.  Eleven of them shipped that way.

Binding each call against the real signature catches the whole class at import
time instead of in a chat nobody is watching.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from open_shrimp import rich_message

SRC = pathlib.Path(rich_message.__file__).parent

CHECKED = {
    name: inspect.signature(getattr(rich_message, name))
    for name in (
        "send_rich", "reply_rich", "edit_rich", "edit_message_rich",
        "send_rich_draft",
    )
}


def _calls() -> list[tuple[pathlib.Path, ast.Call, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            if name in CHECKED:
                found.append((path, node, name))
    return found


def test_there_are_call_sites_to_check() -> None:
    """A silent zero here would make the test below vacuously green."""
    assert len(_calls()) > 30


@pytest.mark.parametrize(
    "path,node,name",
    _calls(),
    ids=[f"{p.name}:{n.lineno}:{name}" for p, n, name in _calls()],
)
def test_call_binds_to_its_signature(
    path: pathlib.Path, node: ast.Call, name: str,
) -> None:
    positional = len(node.args)
    if any(isinstance(a, ast.Starred) for a in node.args):
        pytest.skip("unpacked call; arity is not statically known")
    keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
    try:
        CHECKED[name].bind_partial(*[None] * positional, **dict.fromkeys(keywords))
    except TypeError as exc:
        pytest.fail(f"{path}:{node.lineno} {name}(...) does not bind: {exc}")

    # bind_partial accepts a call that is missing required arguments, which is
    # exactly the failure that shipped, so require the mandatory ones too.
    supplied = positional + len(keywords)
    required = [
        p for p in CHECKED[name].parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    assert supplied >= len(required), (
        f"{path}:{node.lineno} {name}(...) supplies {supplied} arguments, "
        f"but {len(required)} are required"
    )
