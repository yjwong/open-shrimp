"""OpenShrimp-owned compatibility implementation of Claude Code's Agent tool."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    split_provider_model,
)
from open_shrimp.tools import OpenShrimpTool

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "general"


@dataclass(frozen=True)
class AgentArgs:
    description: str
    prompt: str
    subagent_type: str
    model: str | None = None


@dataclass(frozen=True)
class AgentToolContext:
    client_getter: Callable[[], OpenCodeClient | None]
    cwd: str | None = None


def create_agent_tool(ctx: AgentToolContext) -> OpenShrimpTool:
    async def handler(raw_args: dict[str, Any]) -> dict[str, Any]:
        try:
            args = validate_agent_args(raw_args)
        except ValueError as exc:
            return _text_result(f"Error: {exc}", is_error=True)
        try:
            text = await run_agent_foreground(args, ctx)
        except Exception as exc:
            logger.exception("Agent tool failed")
            return _text_result(f"Error running agent: {exc}", is_error=True)
        return _text_result(text)

    return OpenShrimpTool(
        name="agent",
        description=(
            "Launch a specialized subagent in a child OpenCode session and return "
            "its final answer. Use this for independent research or focused work. "
            "If subagent_type is omitted, 'general' is used. Available common "
            "agent types include 'general' and 'explore'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "The type of specialized agent to use for this task",
                },
                "model": {
                    "type": "string",
                    "description": "Optional provider/model override for this agent",
                },
            },
            "required": ["description", "prompt"],
        },
        read_only=True,
        handler=handler,
    )


def validate_agent_args(raw_args: dict[str, Any]) -> AgentArgs:
    description = str(raw_args.get("description", "")).strip()
    prompt = str(raw_args.get("prompt", "")).strip()
    subagent_type = str(raw_args.get("subagent_type", "")).strip() or _DEFAULT_AGENT
    model_raw = raw_args.get("model")
    model = str(model_raw).strip() if model_raw is not None else None
    if not description:
        raise ValueError("description is required")
    if not prompt:
        raise ValueError("prompt is required")
    return AgentArgs(
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        model=model or None,
    )


async def run_agent_foreground(args: AgentArgs, ctx: AgentToolContext) -> str:
    client = ctx.client_getter()
    if client is None:
        raise ProcessError("parent OpenCode client is not available")
    parent_session_id = client.session_id
    if parent_session_id is None:
        raise ProcessError("parent OpenCode session is not available")

    child_model: dict[str, Any] | None = None
    prompt_provider: str | None = None
    prompt_model: str | None = None
    if args.model:
        prompt_provider, prompt_model = split_provider_model(args.model)
        child_model = {"providerID": prompt_provider, "modelID": prompt_model}

    child_session_id = await client.create_session(
        directory=ctx.cwd,
        permission_rules=client.permission_rules,
        parent_id=parent_session_id,
        title=f"{args.description} (@{args.subagent_type} subagent)",
        agent=args.subagent_type,
        model=child_model,
    )
    queue = client.subscribe_session(child_session_id)
    bridge = client.create_permission_bridge(child_session_id)
    text_parts: list[str] = []
    result: ResultMessage | None = None
    try:
        await client.prompt_session(
            child_session_id,
            parts=[{"type": "text", "text": args.prompt}],
            provider=prompt_provider,
            model=prompt_model,
            agent=args.subagent_type,
        )
        async for message in client.iter_session_response(
            child_session_id, queue, bridge=bridge,
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, (ToolUseBlock, ToolResultBlock)):
                        continue
            elif isinstance(message, ResultMessage):
                result = message
    finally:
        if bridge is not None:
            await bridge.stop()
        client.unsubscribe_session(child_session_id)

    final_text = "".join(text_parts).strip()
    if result is not None and result.is_error:
        error = _format_errors(result.errors)
        if final_text:
            return f"{final_text}\n\nAgent completed with errors: {error}"
        return f"Agent completed with errors: {error}"
    return final_text or "Agent completed without a text response."


def _format_errors(errors: list[dict[str, Any]] | None) -> str:
    if not errors:
        return "unknown error"
    messages = [str(err.get("message", "")).strip() for err in errors]
    messages = [msg for msg in messages if msg]
    return "; ".join(messages) if messages else "unknown error"


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result
