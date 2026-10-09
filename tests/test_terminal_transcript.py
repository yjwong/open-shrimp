"""Agent transcripts parsed into events for the terminal Mini App."""

from __future__ import annotations

import json

from open_shrimp.terminal.transcript import (
    TOOL_RESULT_MAX_CHARS,
    parse_transcript,
    parse_transcript_lines,
)


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _user(content: object) -> str:
    return _line({"type": "user", "message": {"content": content}})


def _assistant(*blocks: dict) -> str:
    return _line({"type": "assistant", "message": {"content": list(blocks)}})


def test_prompt_is_kept_whole() -> None:
    prompt = "Audit every handler.\n" + "x" * 5000

    assert parse_transcript(_user(prompt)) == [
        {"kind": "prompt", "text": prompt},
    ]


def test_tool_call_carries_its_full_input() -> None:
    command = "cd /srv/app && " + " && ".join(f"step{i}" for i in range(80))
    transcript = _assistant(
        {"type": "text", "text": "Running the build."},
        {"type": "thinking", "thinking": "hidden"},
        {"type": "tool_use", "id": "tu1", "name": "Bash",
         "input": {"command": command}},
    )

    text, tool = parse_transcript(transcript)

    assert text == {"kind": "text", "text": "Running the build."}
    assert tool["kind"] == "tool"
    assert tool["id"] == "tu1"
    assert tool["name"] == "Bash"
    assert tool["input"] == {"command": command}
    assert tool["summary"]


def test_tool_result_is_matched_by_id_and_capped() -> None:
    big = "y" * (TOOL_RESULT_MAX_CHARS + 10)
    transcript = _user([
        {"type": "tool_result", "tool_use_id": "tu1",
         "content": [{"type": "text", "text": big}]},
        {"type": "tool_result", "tool_use_id": "tu2",
         "content": "exit 1", "is_error": True},
    ])

    assert parse_transcript(transcript) == [
        {"kind": "tool_result", "id": "tu1",
         "text": big[:TOOL_RESULT_MAX_CHARS], "is_error": False,
         "truncated": True},
        {"kind": "tool_result", "id": "tu2", "text": "exit 1",
         "is_error": True, "truncated": False},
    ]


def test_records_without_content_are_skipped() -> None:
    transcript = (
        _line({"type": "system", "subtype": "init"})
        + _line({"type": "result", "result": "ok"})
        + _line(["not", "an", "object"])
    )

    assert parse_transcript(transcript) == []


def test_unfinished_line_is_held_back() -> None:
    whole = _user("Go.")
    partial = '{"type": "assistant", "mess'

    events, remainder = parse_transcript_lines(whole + partial)

    assert events == [{"kind": "prompt", "text": "Go."}]
    assert remainder == partial
