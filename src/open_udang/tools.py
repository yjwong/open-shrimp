"""Custom MCP tools exposed to the Claude agent via SDK MCP servers.

Provides in-process tools that Claude can call directly, with access to
the Telegram Bot instance for sending files, images, etc.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from telegram import Bot

logger = logging.getLogger(__name__)

# Telegram Bot API limits.
_MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # 50 MB
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB

# MIME types that Telegram can display as inline photos.
_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """Build a standard MCP tool result."""
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        result["is_error"] = True
    return result


def _guess_mime(path: str) -> str | None:
    """Guess the MIME type from the file extension."""
    mime, _ = mimetypes.guess_type(path)
    return mime


def create_openudang_mcp_server(
    bot: Bot,
    chat_id: int,
    thread_id: int | None = None,
) -> McpSdkServerConfig:
    """Create an in-process MCP server with OpenUdang-specific tools.

    The returned server is bound to a specific *bot*, *chat_id*, and
    optional *thread_id* so tool handlers can send files directly to the
    correct Telegram chat or forum thread.

    Args:
        bot: Telegram Bot instance.
        chat_id: Telegram chat ID to send files to.
        thread_id: Optional message_thread_id for forum topics.

    Returns:
        An ``McpSdkServerConfig`` ready for ``ClaudeAgentOptions.mcp_servers``.
    """

    # Build common kwargs for message_thread_id support.
    _thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        _thread_kwargs["message_thread_id"] = thread_id

    @tool(
        "send_file",
        "Send a file to the user via Telegram. Use this when the user asks "
        "you to send, share, or deliver a file. The file must exist on the "
        "local filesystem. Maximum size is 50 MB. Images (JPEG, PNG, GIF, "
        "WebP) under 10 MB are automatically sent as inline photos unless "
        "type is set to 'document'.",
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption to display with the file.",
                },
                "type": {
                    "type": "string",
                    "enum": ["auto", "photo", "document"],
                    "description": (
                        "How to send the file. 'photo' sends as an inline "
                        "image (max 10 MB, JPEG/PNG/GIF/WebP only). "
                        "'document' sends as a file attachment. 'auto' "
                        "(default) picks photo for eligible images, "
                        "document otherwise."
                    ),
                },
            },
            "required": ["file_path"],
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        file_path = args.get("file_path", "")
        caption = args.get("caption")
        send_type = args.get("type", "auto")

        if not file_path:
            return _text_result("Error: file_path is required.", is_error=True)

        path = os.path.abspath(file_path)

        if not os.path.isfile(path):
            return _text_result(
                f"Error: File not found: {path}", is_error=True,
            )

        size = os.path.getsize(path)
        if size == 0:
            return _text_result("Error: File is empty.", is_error=True)

        if size > _MAX_DOCUMENT_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: File too large ({mb:.1f} MB). "
                f"Telegram limit is 50 MB.",
                is_error=True,
            )

        # Decide whether to send as photo or document.
        mime = _guess_mime(path)
        use_photo = False
        if send_type == "photo":
            use_photo = True
        elif send_type == "auto" and mime in _PHOTO_MIME_TYPES:
            use_photo = size <= _MAX_PHOTO_SIZE

        if use_photo and size > _MAX_PHOTO_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: Photo too large ({mb:.1f} MB). "
                f"Telegram limit for photos is 10 MB. "
                f"Use type='document' for larger images.",
                is_error=True,
            )

        filename = os.path.basename(path)

        try:
            with open(path, "rb") as f:
                if use_photo:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption,
                        **_thread_kwargs,
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=filename,
                        caption=caption,
                        **_thread_kwargs,
                    )
            method = "photo" if use_photo else "document"
            logger.info("Sent %s %s to chat %d", method, path, chat_id)
            return _text_result(f"File sent successfully: {filename}")
        except Exception as exc:
            logger.exception("Failed to send file %s to chat %d", path, chat_id)
            return _text_result(
                f"Error sending file: {exc}", is_error=True,
            )

    # --- set_topic_title (forum topics only) ---
    # Only register this tool when the chat is a forum topic, so Claude
    # can set/update the thread title.
    tools_list: list[Any] = [send_file]

    if thread_id is not None:

        @tool(
            "set_topic_title",
            "Set or update the title of the current Telegram forum topic. "
            "Use this after your first response to set a concise title that "
            "summarizes the conversation topic, and again later if the topic "
            "changes significantly. Maximum 128 characters.",
            {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "The title for the forum topic. Should be a short, "
                            "descriptive summary (max 128 characters)."
                        ),
                    },
                },
                "required": ["title"],
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def set_topic_title(args: dict[str, Any]) -> dict[str, Any]:
            title = args.get("title", "").strip()
            if not title:
                return _text_result(
                    "Error: title is required.", is_error=True,
                )
            if len(title) > 128:
                title = title[:128]

            try:
                await bot.edit_forum_topic(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    name=title,
                )
                logger.info(
                    "Set topic title to %r in chat %d thread %d",
                    title, chat_id, thread_id,
                )
                return _text_result(f"Topic title set to: {title}")
            except Exception as exc:
                logger.exception(
                    "Failed to set topic title in chat %d thread %d",
                    chat_id, thread_id,
                )
                return _text_result(
                    f"Error setting topic title: {exc}", is_error=True,
                )

        tools_list.append(set_topic_title)

    return create_sdk_mcp_server(
        name="openudang",
        tools=tools_list,
    )
