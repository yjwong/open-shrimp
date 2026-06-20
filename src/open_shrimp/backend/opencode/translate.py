"""OpenCode SSE-event → ``backend.types`` translation (the opencode adapter).

``_iter_response`` is the single OpenCode-shape-aware code path on the receive
side.  It consumes the demultiplexed SSE event stream for one session and
yields ``backend.types`` messages only — OpenCode wire shapes never escape
this module.

Event → message map (load-bearing):

================================  ==========================================
OpenCode SSE event                ``backend.types`` emitted
================================  ==========================================
``session.error``                 raise ``ProcessError``
``message.updated`` (finish/err)  ``AssistantMessage`` (usage/error on final)
``message.part.delta`` text       ``StreamEvent`` + buffer text
``message.part.updated`` tool     ``AssistantMessage([ToolUseBlock])`` (pending/
                                  running) / ``UserMessage([ToolResultBlock])``
                                  (completed/error)
``message.part.updated`` reason   filtered (reasoning deltas dropped)
``session.idle``                  ``ResultMessage`` (``num_steps`` → ``num_turns``)
``permission.asked``              bridge dispatch (no yield)
``question.asked``                callback dispatch (no yield)
================================  ==========================================

The ``num_steps`` → ``num_turns`` rename is the one field translation:
``backend.types.ResultMessage`` carries ``num_turns`` (opencode maps its
``num_steps`` here — decision 2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from open_shrimp.backend.errors import CLIConnectionError, ProcessError
from open_shrimp.backend.opencode.permission import PermissionBridge
from open_shrimp.backend.opencode.sse import EventQueue, EventQueueClosed
from open_shrimp.backend.types import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

logger = logging.getLogger(__name__)


EVT_MESSAGE_PART_DELTA = "message.part.delta"
EVT_MESSAGE_PART_UPDATED = "message.part.updated"
EVT_MESSAGE_UPDATED = "message.updated"
EVT_PERMISSION_ASKED = "permission.asked"
EVT_QUESTION_ASKED = "question.asked"
EVT_SESSION_IDLE = "session.idle"
EVT_SESSION_ERROR = "session.error"

_TOKEN_KEYS = ("input", "output", "reasoning")

_TOOL_STATUS_PENDING = "pending"
_TOOL_STATUS_RUNNING = "running"
_TOOL_STATUS_COMPLETED = "completed"
_TOOL_STATUS_ERROR = "error"
_TOOL_STATUS_IN_FLIGHT = frozenset({_TOOL_STATUS_PENDING, _TOOL_STATUS_RUNNING})

_PART_TYPE_TOOL = "tool"
_PART_TYPE_REASONING = "reasoning"


def _resolve_part_id(props: dict[str, Any]) -> str | None:
    """Extract a part id from an SSE event's ``properties``.

    OpenCode publishes ``message.part.delta`` with the id either nested
    as ``part.id`` or flat as ``partID``; older snapshots use one, newer
    ones the other. Callers should treat them interchangeably.
    """
    part = props.get("part")
    if isinstance(part, dict):
        pid = part.get("id")
        if isinstance(pid, str):
            return pid
    pid = props.get("partID")
    return pid if isinstance(pid, str) else None


async def _iter_response(
    queue: EventQueue,
    session_id: str,
    http: httpx.AsyncClient | None,
    bridge: PermissionBridge | None,
    handle_questions,
) -> AsyncIterator[Message]:
    """Translate SSE events into backend.types messages until session.idle."""
    text_buffers: dict[str, list[str]] = {}
    part_order: list[str] = []
    tool_use_emitted: set[str] = set()
    tool_result_emitted: set[str] = set()
    # Reasoning parts surface as message.part.delta with field="text", same as
    # real text parts (opencode processor.ts emits updatePartDelta with
    # field:"text" for reasoning-delta). The delta payload itself doesn't
    # carry the part type, so we learn it from message.part.updated and
    # drop matching deltas to keep thinking traces out of Telegram.
    reasoning_part_ids: set[str] = set()
    loop = asyncio.get_running_loop()
    turn_start_ms = int(loop.time() * 1000)

    # Per-turn usage accumulation. OpenCode emits a stream of
    # ``message.updated`` events per assistant message; we treat each
    # unique assistant ``info.id`` as one "step" and finalise the
    # step's tokens/cost/error exactly once (on first sight of
    # ``finish`` or ``error`` in the info payload).
    model_usage: dict[str, dict[str, Any]] = {}
    total_cost_usd = 0.0
    errors: list[dict[str, Any]] = []
    seen_step_ids: set[str] = set()      # any assistant id we've seen at all
    finalised_steps: set[str] = set()    # ids whose tokens/error we've folded

    def _flush_text() -> list[AssistantMessage]:
        out: list[AssistantMessage] = []
        for pid in part_order:
            text = "".join(text_buffers[pid])
            if text:
                out.append(
                    AssistantMessage(content=[TextBlock(text=text)])
                )
        text_buffers.clear()
        part_order.clear()
        return out

    def _flush_step(
        usage: dict[str, Any] | None,
        error: str | None,
    ) -> list[AssistantMessage]:
        """Flush buffered text and attach the step's usage/error.

        Usage/error rides on the *final* AssistantMessage of the step.
        When the step produced no text we still emit an empty
        AssistantMessage so per-turn UI in ``stream.py`` (which reads
        ``event.usage``) sees the update.
        """
        out = _flush_text()
        if usage is None and error is None:
            return out
        if not out:
            out.append(AssistantMessage(content=[]))
        out[-1].usage = usage
        out[-1].error = error
        return out

    while True:
        try:
            evt = await queue.get()
        except EventQueueClosed:
            return

        etype = evt.get("type", "")
        props = evt.get("properties") or {}
        if not isinstance(props, dict):
            props = {}


        if etype == EVT_SESSION_ERROR:
            raise ProcessError(_extract_error_message(props))

        if etype == EVT_MESSAGE_UPDATED:
            info = props.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            step_id = info.get("id")
            if not isinstance(step_id, str):
                continue
            seen_step_ids.add(step_id)
            if step_id in finalised_steps:
                continue

            err = info.get("error")
            err_message: str | None = None
            if isinstance(err, dict):
                raw = err.get("message")
                if isinstance(raw, str) and raw:
                    err_message = raw

            finish = info.get("finish")
            if err_message is None and not isinstance(finish, str):
                # Step is still in flight — wait for finish/error.
                continue

            finalised_steps.add(step_id)
            model_id = info.get("modelID")
            tokens = info.get("tokens")
            tokens = tokens if isinstance(tokens, dict) else None
            cost = info.get("cost")
            cost = float(cost) if isinstance(cost, (int, float)) else 0.0
            if err_message is None:
                total_cost_usd += cost
                _fold_into_model_usage(
                    model_usage,
                    model_id if isinstance(model_id, str) else None,
                    tokens, cost,
                )
                for msg in _flush_step(usage=tokens, error=None):
                    yield msg
            else:
                errors.append(
                    {
                        "message": err_message,
                        "when": (info.get("time") or {}).get("completed"),
                    }
                )
                for msg in _flush_step(usage=None, error=err_message):
                    yield msg
            continue

        if etype == EVT_SESSION_IDLE:
            for msg in _flush_text():
                yield msg
            yield ResultMessage(
                session_id=session_id,
                total_cost_usd=total_cost_usd,
                usage=_aggregate_tokens(model_usage),
                model_usage=model_usage,
                num_turns=len(seen_step_ids),
                duration_ms=int(loop.time() * 1000) - turn_start_ms,
                errors=errors,
                is_error=bool(errors),
            )
            return

        if etype == EVT_MESSAGE_PART_DELTA and props.get("field") == "text":
            part_id = _resolve_part_id(props)
            if part_id in reasoning_part_ids:
                continue
            delta = props.get("delta", "")
            if part_id is not None and isinstance(delta, str):
                if part_id not in text_buffers:
                    text_buffers[part_id] = []
                    part_order.append(part_id)
                text_buffers[part_id].append(delta)
            yield StreamEvent(event=evt)
            continue

        if etype == EVT_MESSAGE_PART_UPDATED:
            part = props.get("part") or {}
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type == _PART_TYPE_REASONING:
                    pid = part.get("id")
                    if isinstance(pid, str):
                        reasoning_part_ids.add(pid)
                elif part_type == _PART_TYPE_TOOL:
                    if bridge is not None:
                        bridge.observe_tool_part(part)
                    for msg in _toolpart_messages(
                        part, tool_use_emitted, tool_result_emitted,
                        flush_text=_flush_text,
                    ):
                        yield msg
            continue

        if etype == EVT_PERMISSION_ASKED:
            if bridge is not None:
                bridge.observe_permission_asked(evt)
            continue

        if etype == EVT_QUESTION_ASKED:
            await _handle_question_asked(http, evt, handle_questions)
            continue

        logger.debug("dropping event type=%s", etype)


async def _handle_question_asked(
    http: httpx.AsyncClient | None,
    evt: dict[str, Any],
    handle_questions,
) -> None:
    props = evt.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    request_id = props.get("requestID") or props.get("id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning("question.asked missing request id: %r", evt)
        return
    if http is None:
        logger.warning("question.asked without HTTP client; rejecting %s", request_id)
        return

    questions = _extract_questions(props)
    if handle_questions is None:
        logger.warning("No native question handler set; rejecting %s", request_id)
        await _reject_question(http, request_id)
        return

    try:
        answers = await handle_questions(questions)
        await _reply_question(http, request_id, answers)
    except asyncio.CancelledError:
        await _reject_question(http, request_id)
        raise
    except Exception:
        logger.exception("Question handler failed; rejecting %s", request_id)
        await _reject_question(http, request_id)


def _extract_questions(props: dict[str, Any]) -> list[dict[str, Any]]:
    raw = props.get("questions")
    if isinstance(raw, list):
        return [q for q in raw if isinstance(q, dict)]
    raw = props.get("question")
    if isinstance(raw, dict):
        return [raw]
    return []


async def _reply_question(
    http: httpx.AsyncClient,
    request_id: str,
    answers: Any,
) -> None:
    if not isinstance(answers, list):
        answers = []
    normalised: list[list[str]] = []
    for answer in answers:
        if isinstance(answer, list):
            normalised.append([str(item) for item in answer])
        else:
            normalised.append([str(answer)])
    try:
        r = await http.post(
            f"/question/{request_id}/reply",
            json={"answers": normalised},
        )
    except httpx.HTTPError as exc:
        raise CLIConnectionError(
            f"POST /question/{request_id}/reply failed: {exc}"
        ) from exc
    if r.status_code >= 400:
        raise ProcessError(
            f"POST /question/{request_id}/reply returned "
            f"{r.status_code}: {r.text[:300]}"
        )


async def _reject_question(http: httpx.AsyncClient, request_id: str) -> None:
    try:
        r = await http.post(f"/question/{request_id}/reject")
    except httpx.HTTPError:
        logger.exception("POST /question/%s/reject failed", request_id)
        return
    if r.status_code >= 400:
        logger.warning(
            "POST /question/%s/reject returned %s: %s",
            request_id, r.status_code, r.text[:300],
        )


def _new_token_bucket() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }


def _add_tokens(dest: dict[str, Any], src: dict[str, Any]) -> None:
    """Add ``src``'s token fields into ``dest`` in place.

    Both dicts use the OpenCode-native shape produced by
    ``_new_token_bucket``. ``src`` may also be raw wire ``tokens`` —
    untrusted, so each scalar is type-guarded.
    """
    for key in _TOKEN_KEYS:
        val = src.get(key)
        if isinstance(val, (int, float)):
            dest[key] += int(val)
    cache = src.get("cache")
    if isinstance(cache, dict):
        dest_cache = dest["cache"]
        for sub in ("read", "write"):
            val = cache.get(sub)
            if isinstance(val, (int, float)):
                dest_cache[sub] += int(val)


def _fold_into_model_usage(
    model_usage: dict[str, dict[str, Any]],
    model_id: str | None,
    tokens: dict[str, Any] | None,
    cost: float,
) -> None:
    """Accumulate one step's tokens/cost into the per-model bucket."""
    if model_id is None:
        return
    bucket = model_usage.get(model_id)
    if bucket is None:
        bucket = _new_token_bucket()
        bucket["cost"] = 0.0
        model_usage[model_id] = bucket
    if tokens is not None:
        _add_tokens(bucket, tokens)
    bucket["cost"] += cost


def _aggregate_tokens(
    model_usage: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Sum per-model token buckets into a single OpenCode-native dict."""
    total = _new_token_bucket()
    for bucket in model_usage.values():
        _add_tokens(total, bucket)
    return total


def _toolpart_messages(
    part: dict[str, Any],
    tool_use_emitted: set[str],
    tool_result_emitted: set[str],
    *,
    flush_text,
) -> list[Message]:
    """Synthesise ToolUseBlock / ToolResultBlock messages from a ToolPart.

    Emit rules:
    * First sight of ``pending``/``running`` with non-empty ``input`` →
      ``AssistantMessage([ToolUseBlock])``.
    * First sight of ``completed`` → ``UserMessage([ToolResultBlock])``.
    * First sight of ``error`` → ``UserMessage([ToolResultBlock(is_error=True)])``.
    * Anything else is a repeat and dropped.
    """
    raw_call_id = part.get("callID")
    if not raw_call_id:
        return []
    call_id = str(raw_call_id)
    state = part.get("state") or {}
    if not isinstance(state, dict):
        return []
    status = state.get("status")

    if status in _TOOL_STATUS_IN_FLIGHT:
        if call_id in tool_use_emitted:
            return []
        raw_input = state.get("input")
        if not isinstance(raw_input, dict) or not raw_input:
            return []
        out: list[Message] = list(flush_text())
        out.append(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id=call_id,
                        # Native OpenCode tool name flows through; the
                        # OpenCode policy module owns the vocabulary.
                        name=str(part.get("tool") or ""),
                        input=raw_input,
                    )
                ]
            )
        )
        tool_use_emitted.add(call_id)
        return out

    if status == _TOOL_STATUS_COMPLETED:
        if call_id in tool_result_emitted:
            return []
        output = state.get("output")
        tool_result_emitted.add(call_id)
        return [
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=call_id,
                        content=output if output is not None else "",
                        is_error=False,
                    )
                ]
            )
        ]

    if status == _TOOL_STATUS_ERROR:
        if call_id in tool_result_emitted:
            return []
        err = state.get("error")
        tool_result_emitted.add(call_id)
        return [
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=call_id,
                        content=err if err is not None else "tool error",
                        is_error=True,
                    )
                ]
            )
        ]

    return []


def _extract_error_message(props: dict[str, Any]) -> str:
    err = props.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict):
            msg = data.get("message")
            if msg:
                return str(msg)
        name = err.get("name")
        if name:
            return str(name)
    return EVT_SESSION_ERROR


__all__ = ["_iter_response"]
