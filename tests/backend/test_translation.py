"""Equivalence tests for the claude_sdk adapter's _to_backend_event.

The step's correctness claim is "zero behavior change": each SDK message must
translate to the matching backend.types instance with the load-bearing fields
preserved.  We construct real SDK instances (so the test tracks the live SDK,
not a hand-rolled fake) and assert on the translated output.
"""

from __future__ import annotations

import claude_agent_sdk.types as sdk

from open_shrimp.backend import types as bt
from open_shrimp.backend.claude_sdk.translate import _to_backend_event


def test_assistant_message_translates_with_session_id():
    """AssistantMessage carries content blocks, usage, error, and the
    early-capture session_id."""
    msg = sdk.AssistantMessage(
        content=[sdk.TextBlock(text="hi"), sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
        model="claude-x",
        usage={"input_tokens": 3},
        error=None,
        session_id="sess-abc",
    )
    out = _to_backend_event(msg)
    assert isinstance(out, bt.AssistantMessage)
    assert out.session_id == "sess-abc"
    assert out.usage == {"input_tokens": 3}
    assert isinstance(out.content[0], bt.TextBlock) and out.content[0].text == "hi"
    assert isinstance(out.content[1], bt.ToolUseBlock)
    assert out.content[1].id == "t1"
    assert out.content[1].name == "Bash"
    assert out.content[1].input == {"command": "ls"}
    # Translated blocks are backend.types, not SDK types.
    assert not isinstance(out.content[0], sdk.TextBlock)


def test_result_message_keeps_num_turns_and_session():
    msg = sdk.ResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=900,
        is_error=False,
        num_turns=4,
        session_id="sess-r",
        total_cost_usd=0.01,
        usage={"output_tokens": 5},
        model_usage={"claude-x": {"output_tokens": 5}},
        errors=None,
    )
    out = _to_backend_event(msg)
    assert isinstance(out, bt.ResultMessage)
    assert out.session_id == "sess-r"
    assert out.num_turns == 4  # decision 2: keep num_turns
    assert out.duration_ms == 1200
    assert out.total_cost_usd == 0.01
    assert out.usage == {"output_tokens": 5}
    assert out.model_usage == {"claude-x": {"output_tokens": 5}}
    assert out.is_error is False


def test_system_message_has_no_session_id_attr():
    msg = sdk.SystemMessage(subtype="init", data={"session_id": "x"})
    out = _to_backend_event(msg)
    assert isinstance(out, bt.SystemMessage)
    assert out.subtype == "init"
    assert out.data == {"session_id": "x"}
    # SystemMessage intentionally has no session_id field; the getattr guard
    # in stream.py/client_manager.py must keep returning None.
    assert getattr(out, "session_id", None) is None


def test_text_delta_carries_text_and_session():
    msg = sdk.StreamEvent(
        uuid="u1",
        session_id="sess-s",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        },
    )
    out = _to_backend_event(msg)
    assert isinstance(out, bt.TextDeltaEvent)
    assert out.text == "hello"
    assert out.session_id == "sess-s"


def test_non_text_stream_event_returns_none():
    """Non-text ``_SdkStream`` events drop out of the translator.

    The new contract is: only the populated ``content_block_delta`` →
    ``text_delta`` shape becomes a ``TextDeltaEvent``.  Anything else
    (pings, tool-use deltas, malformed envelopes) returns ``None`` so
    consumers can skip without a shape check.
    """
    msg = sdk.StreamEvent(uuid="u1", session_id="sess-s", event={"type": "ping"})
    assert _to_backend_event(msg) is None


def test_rate_limit_event_flattens_rate_limit_info():
    """The SDK nests fields under rate_limit_info; the contract is flat."""
    info = sdk.RateLimitInfo(
        status="allowed_warning",
        resets_at=1700,
        rate_limit_type="five_hour",
        utilization=0.83,
    )
    msg = sdk.RateLimitEvent(rate_limit_info=info, uuid="u", session_id="sess-rl")
    out = _to_backend_event(msg)
    assert isinstance(out, bt.RateLimitEvent)
    assert out.status == "allowed_warning"
    assert out.rate_limit_type == "five_hour"
    assert out.resets_at == 1700
    assert out.utilization == 0.83
    assert out.session_id == "sess-rl"
    # No nested rate_limit_info survives onto the flat contract type.
    assert not hasattr(out, "rate_limit_info")


def test_task_started_is_systemmessage_subclass_and_preserves_fields():
    msg = sdk.TaskStartedMessage(
        subtype="task_started",
        data={"k": "v"},
        task_id="task-1",
        description="do a thing",
        uuid="u",
        session_id="sess-t",
        tool_use_id="tu-1",
        task_type="bash",
    )
    out = _to_backend_event(msg)
    assert isinstance(out, bt.TaskStartedMessage)
    # decision 4: Task* mirror SDK inheritance so stream.py's nested dispatch
    # (Task* checks inside the isinstance(SystemMessage) branch) is untouched.
    assert isinstance(out, bt.SystemMessage)
    assert out.task_id == "task-1"
    assert out.tool_use_id == "tu-1"
    assert out.description == "do a thing"
    assert out.task_type == "bash"
    assert out.session_id == "sess-t"
    assert out.subtype == "task_started"
    assert out.data == {"k": "v"}
    # output_file is read defensively (SDK TaskStartedMessage has no such
    # field) — must default to None without raising.
    assert out.output_file is None


def test_task_progress_translates():
    usage = {}  # TaskUsage is a TypedDict; an empty dict satisfies it
    msg = sdk.TaskProgressMessage(
        subtype="task_progress",
        data={"pct": 50},
        task_id="task-2",
        description="working",
        usage=usage,
        uuid="u",
        session_id="sess-tp",
    )
    out = _to_backend_event(msg)
    assert isinstance(out, bt.TaskProgressMessage)
    assert isinstance(out, bt.SystemMessage)
    assert out.task_id == "task-2"
    assert out.data == {"pct": 50}
    assert out.session_id == "sess-tp"


def test_task_notification_translates():
    msg = sdk.TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-3",
        status="completed",
        output_file="/tmp/out.log",
        summary="done",
        uuid="u",
        session_id="sess-tn",
        tool_use_id="tu-3",
    )
    out = _to_backend_event(msg)
    assert isinstance(out, bt.TaskNotificationMessage)
    assert isinstance(out, bt.SystemMessage)
    assert out.task_id == "task-3"
    assert out.status == "completed"
    assert out.output_file == "/tmp/out.log"
    assert out.summary == "done"
    assert out.tool_use_id == "tu-3"
    assert out.session_id == "sess-tn"


def test_user_message_str_content_passes_through():
    msg = sdk.UserMessage(content="plain string prompt")
    out = _to_backend_event(msg)
    assert isinstance(out, bt.UserMessage)
    assert out.content == "plain string prompt"


def test_user_message_list_with_thinking_block_surfaces_tool_result():
    """The list may contain block types the contract does not define
    (ThinkingBlock). Translation must not crash and must still surface the
    ToolResultBlock so stream.py's filter selects exactly what it does today.
    """
    thinking = sdk.ThinkingBlock(thinking="hmm", signature="sig")
    tool_result = sdk.ToolResultBlock(
        tool_use_id="tu-9", content="output text", is_error=False
    )
    msg = sdk.UserMessage(content=[thinking, tool_result])
    out = _to_backend_event(msg)
    assert isinstance(out, bt.UserMessage)
    assert isinstance(out.content, list) and len(out.content) == 2
    # The ToolResultBlock is translated to the contract type so downstream
    # isinstance(block, backend.ToolResultBlock) still matches.
    results = [b for b in out.content if isinstance(b, bt.ToolResultBlock)]
    assert len(results) == 1
    assert results[0].tool_use_id == "tu-9"
    assert results[0].content == "output text"
    # The unknown block passes through untouched (not dropped, not crashed).
    assert thinking in out.content


def test_parent_tool_use_id_round_trips():
    assistant = _to_backend_event(
        sdk.AssistantMessage(
            content=[sdk.TextBlock(text="hi")],
            model="claude-x",
            parent_tool_use_id="parent-1",
        )
    )
    assert isinstance(assistant, bt.AssistantMessage)
    assert assistant.parent_tool_use_id == "parent-1"

    user = _to_backend_event(
        sdk.UserMessage(content="sub", parent_tool_use_id="parent-2")
    )
    assert isinstance(user, bt.UserMessage)
    assert user.parent_tool_use_id == "parent-2"

    stream = _to_backend_event(
        sdk.StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "x"},
            },
            parent_tool_use_id="parent-3",
        )
    )
    assert isinstance(stream, bt.TextDeltaEvent)
    assert stream.parent_tool_use_id == "parent-3"


def test_unknown_message_passes_through():
    sentinel = object()
    assert _to_backend_event(sentinel) is sentinel
