"""Claude Agent SDK wrapper for OpenShrimp.

Provides an async generator interface over ClaudeSDKClient that yields
streaming messages (text chunks, tool events, results) for the caller
to consume and bridge to Telegram.
"""

import asyncio
import logging
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

# SDK message types are imported (aliased) ONLY for the translation function
# below — _to_backend_event is the single SDK-message-aware code path on
# master after step 1.  Everything downstream sees open_shrimp.backend.types.
from claude_agent_sdk import (
    AssistantMessage as _SdkAssistant,
    ResultMessage as _SdkResult,
    SystemMessage as _SdkSystem,
    TextBlock as _SdkText,
    ToolResultBlock as _SdkToolResult,
    ToolUseBlock as _SdkToolUse,
    UserMessage as _SdkUser,
)
from claude_agent_sdk.types import (
    RateLimitEvent as _SdkRateLimit,
    StreamEvent as _SdkStream,
    TaskNotificationMessage as _SdkTaskNotif,
    TaskProgressMessage as _SdkTaskProgress,
    TaskStartedMessage as _SdkTaskStarted,
)

from open_shrimp.backend import types as bt
from open_shrimp.config import ContextConfig
from open_shrimp.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    QuestionCallback,
    make_can_use_tool,
)

logger = logging.getLogger(__name__)


@dataclass
class FileAttachment:
    """A file attachment to include in the prompt (image, PDF, etc.)."""

    data: bytes  # raw file bytes
    mime_type: str  # e.g. "image/jpeg", "application/pdf"
    filename: str | None = None  # original filename, if available



# Map MIME types to file extensions.
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".tar.gz",
}


@dataclass
class AgentResult:
    """Final result from an agent invocation."""

    session_id: str
    result_text: str


# Backend-neutral union of message types yielded by run_agent.  Every SDK
# message is translated to one of these by _to_backend_event before it leaves
# this module, so consumers (stream.py, client_manager.py) never see SDK types.
AgentEvent = bt.Message


def _block(b: Any) -> Any:
    """Translate one SDK content block into its backend.types equivalent.

    Used for ``AssistantMessage.content``, which only ever holds
    Text/ToolUse/ToolResult blocks.  Any other type passes through untouched
    (defensive — should not occur on this path).
    """
    if isinstance(b, _SdkText):
        return bt.TextBlock(text=b.text)
    if isinstance(b, _SdkToolUse):
        return bt.ToolUseBlock(id=b.id, name=b.name, input=b.input)
    if isinstance(b, _SdkToolResult):
        return bt.ToolResultBlock(
            tool_use_id=b.tool_use_id, content=b.content, is_error=b.is_error
        )
    return b


def _user_content(content: Any) -> Any:
    """Translate ``UserMessage.content``, preserving its dual shape.

    The SDK type is ``str | list[block...]`` where the list may contain block
    types the shared contract does not define (ThinkingBlock, ServerTool*).
    stream.py only acts on UserMessage when content is a ``list`` and inside it
    reads only ``ToolResultBlock``s.  So: pass a ``str`` through unchanged;
    for a ``list``, translate ToolResultBlocks (so the downstream
    ``isinstance(block, backend.ToolResultBlock)`` filter still selects them)
    and pass every other block through untouched.
    """
    if not isinstance(content, list):
        return content
    return [_block(b) if isinstance(b, _SdkToolResult) else b for b in content]


def _to_backend_event(msg: Any) -> Any:
    """Convert one SDK message into the backend-neutral contract type.

    The ONLY SDK-message-aware code path on master after step 1.  Order
    matters: Task* subclass SystemMessage in the SDK, so check them first.
    Unknown types pass through raw (defensive).
    """
    if isinstance(msg, _SdkTaskStarted):
        return bt.TaskStartedMessage(
            subtype=msg.subtype,
            data=msg.data,
            task_id=msg.task_id,
            tool_use_id=msg.tool_use_id,
            description=msg.description,
            task_type=msg.task_type,
            output_file=getattr(msg, "output_file", None),
            session_id=msg.session_id,
        )
    if isinstance(msg, _SdkTaskProgress):
        return bt.TaskProgressMessage(
            subtype=msg.subtype,
            data=msg.data,
            task_id=msg.task_id,
            session_id=msg.session_id,
        )
    if isinstance(msg, _SdkTaskNotif):
        return bt.TaskNotificationMessage(
            subtype=msg.subtype,
            data=msg.data,
            task_id=msg.task_id,
            tool_use_id=msg.tool_use_id,
            output_file=msg.output_file,
            status=msg.status,
            summary=msg.summary,
            session_id=msg.session_id,
        )
    if isinstance(msg, _SdkSystem):
        return bt.SystemMessage(subtype=msg.subtype, data=msg.data)
    if isinstance(msg, _SdkAssistant):
        return bt.AssistantMessage(
            content=[_block(b) for b in msg.content],
            usage=msg.usage,
            error=msg.error,
            session_id=msg.session_id,
        )
    if isinstance(msg, _SdkUser):
        return bt.UserMessage(content=_user_content(msg.content))
    if isinstance(msg, _SdkResult):
        return bt.ResultMessage(
            session_id=msg.session_id,
            total_cost_usd=msg.total_cost_usd,
            usage=msg.usage,
            model_usage=msg.model_usage,
            num_turns=msg.num_turns,
            duration_ms=msg.duration_ms,
            errors=msg.errors,
            is_error=msg.is_error,
        )
    if isinstance(msg, _SdkStream):
        return bt.StreamEvent(event=msg.event, session_id=msg.session_id)
    if isinstance(msg, _SdkRateLimit):
        info = msg.rate_limit_info
        return bt.RateLimitEvent(
            status=info.status,
            rate_limit_type=info.rate_limit_type,
            resets_at=info.resets_at,
            utilization=info.utilization,
            session_id=msg.session_id,
        )
    logger.debug("Unknown SDK message type passed through: %s", type(msg).__name__)
    return msg


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename for use in a temp file prefix.

    Strips path separators, null bytes, and other characters that are
    unsafe in file names, keeping only alphanumerics, hyphens, underscores,
    and dots.
    """
    return re.sub(r"[^\w.\-]", "_", name)


def save_attachments(
    attachments: list[FileAttachment],
    chat_id: int,
) -> list[Path]:
    """Save file attachments to temp files and return their paths.

    Files are saved into a per-chat subdirectory of
    :data:`~open_shrimp.hooks.ATTACHMENT_TEMP_DIR` so the canUseTool hook
    can auto-approve Read access for uploaded files without granting
    access to the entire ``/tmp`` tree.  Per-chat scoping prevents one
    agent session from accessing another session's uploads.

    Files are created with delete=False so they persist for the agent to
    read.  The caller is responsible for cleanup.
    """
    from open_shrimp.hooks import ATTACHMENT_TEMP_DIR

    chat_dir = ATTACHMENT_TEMP_DIR / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for att in attachments:
        ext = _MIME_TO_EXT.get(att.mime_type, ".bin")
        # Use sanitized original filename as part of the temp name if available.
        safe_name = _sanitize_filename(att.filename) if att.filename else ""
        prefix = f"openshrimp_{safe_name}_" if safe_name else "openshrimp_"
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix=prefix, delete=False,
            dir=chat_dir,
        )
        tmp.write(att.data)
        tmp.close()
        paths.append(Path(tmp.name))
        logger.info("Saved attachment to %s (%d bytes, %s)", tmp.name, len(att.data), att.mime_type)
    return paths


def build_prompt_with_attachments(prompt: str, attachment_paths: list[Path]) -> str:
    """Prepend file references to the user prompt."""
    parts: list[str] = []
    if len(attachment_paths) == 1:
        parts.append(
            f"The user attached a file. Read it from: {attachment_paths[0]}"
        )
    else:
        parts.append("The user attached files. Read them from:")
        for p in attachment_paths:
            parts.append(f"  - {p}")
    parts.append("")
    parts.append(prompt)
    return "\n".join(parts)


def cleanup_attachments(paths: list[Path]) -> None:
    """Remove temporary attachment files."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to remove temp file %s", p)



async def run_agent(
    prompt: str,
    context: ContextConfig,
    request_approval: ApprovalCallback,
    session_id: str | None = None,
    attachments: list[FileAttachment] | None = None,
    handle_user_questions: QuestionCallback | None = None,
    is_edit_auto_approved: Callable[[], bool] | None = None,
    notify_auto_approved_edit: EditNotifyCallback | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the Claude agent and yield streaming events.

    Args:
        prompt: User message to send to Claude.
        context: Context config with directory, model, allowed_tools.
        request_approval: Async callback for interactive tool approval.
        session_id: Optional session ID to resume a previous conversation.
        attachments: Optional list of file attachments to include in the prompt.
        handle_user_questions: Optional callback for AskUserQuestion tool.
        is_edit_auto_approved: Optional callback returning True if the user
            has opted into "accept all edits" for the current session.
        notify_auto_approved_edit: Optional callback to display diffs for
            auto-approved edits without blocking the agent.

    Yields:
        AgentEvent messages (AssistantMessage, SystemMessage, ResultMessage)
        as they arrive from the SDK.

    The caller should inspect each event:
    - AssistantMessage: extract TextBlock content for streaming to Telegram.
    - SystemMessage (subtype "init"): contains session_id for new sessions.
    - ResultMessage: final result with session_id to persist.

    Supports cancellation via asyncio task cancellation — the async with
    block will clean up the client on CancelledError.
    """
    can_use_tool = make_can_use_tool(
        request_approval=request_approval,
        cwd=context.directory,
        additional_directories=context.additional_directories or None,
        handle_user_questions=handle_user_questions,
        is_edit_auto_approved=is_edit_auto_approved,
        notify_auto_approved_edit=notify_auto_approved_edit,
    )

    def _log_stderr(line: str) -> None:
        logger.info("CLI stderr: %s", line.rstrip())

    options = ClaudeAgentOptions(
        cwd=context.directory,
        model=context.model,
        effort=context.effort,
        allowed_tools=context.allowed_tools,
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
    )

    # Save attachments to temp files and build the prompt with file references.
    attachment_paths: list[Path] = []
    if attachments:
        attachment_paths = save_attachments(attachments, chat_id=0)
        actual_prompt = build_prompt_with_attachments(prompt, attachment_paths)
    else:
        actual_prompt = prompt

    if session_id:
        options.resume = session_id
        logger.info("Resuming session %s in %s", session_id, context.directory)
    else:
        logger.info("Starting new session in %s", context.directory)

    logger.info("Sending query: %s", actual_prompt[:200])

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(actual_prompt)
            async for message in client.receive_response():
                yield _to_backend_event(message)
    except Exception as e:
        if session_id:
            logger.warning(
                "Failed to resume session %s, retrying with new session: %s",
                session_id,
                e,
            )
            options.resume = None
            async with ClaudeSDKClient(options=options) as client:
                await client.query(actual_prompt)
                async for message in client.receive_response():
                    yield _to_backend_event(message)
        else:
            raise
    finally:
        # Clean up temp files
        for p in attachment_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove temp file %s", p)
