"""Message handling and agent dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiosqlite
from telegram import Bot, Update
from telegram.ext import ContextTypes

from open_shrimp.agent import (
    FileAttachment,
)
from open_shrimp.stt import transcribe as stt_transcribe
from open_shrimp.prompt_suggestion import supersede_prompt_suggestion
from open_shrimp.config import Config, ContextConfig
from open_shrimp.db import ChatScope
from open_shrimp.handlers.questions import (
    _complete_other_input,
)
from open_shrimp.handlers.state import (
    _media_group_messages,
    _media_group_tasks,
    _MEDIA_GROUP_WAIT,
    _scope_dispatch_locks,
    _pending_other_input,
    _question_states,
)
from open_shrimp.handlers.utils import (
    _is_authorized,
    _is_bot_addressed,
    _strip_mention,
    _thread_kwargs,
    chat_scope_from_message,
)
from open_shrimp.session_runner import RunnerInput, get_or_start_runner

logger = logging.getLogger(__name__)


def _select_sandbox_manager(
    bot_data: dict[str, Any],
    ctx_config: ContextConfig,
) -> "SandboxManager | None":
    """Pick the right SandboxManager for a context's backend."""
    from open_shrimp.sandbox import SandboxManager

    managers: dict[str, SandboxManager] | None = bot_data.get("sandbox_managers")
    if managers and ctx_config.sandbox:
        return managers.get(ctx_config.sandbox.backend)
    return None


# ---------------------------------------------------------------------------
# Attachment download helpers
# ---------------------------------------------------------------------------


async def _download_telegram_photos(
    messages: list[Any], bot: Bot
) -> list[FileAttachment]:
    """Download photos from one or more Telegram messages and return as FileAttachments.

    Each message may contain one photo (represented as a list of PhotoSize
    objects at different resolutions).  We take the largest resolution from
    each message.
    """
    attachments: list[FileAttachment] = []
    for message in messages:
        if not message.photo:
            continue
        # message.photo is a list of PhotoSize objects sorted by size.
        # Take the largest one (last element) for best quality.
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())
        attachments.append(FileAttachment(data=photo_bytes, mime_type="image/jpeg"))
    return attachments


async def _download_telegram_documents(
    messages: list[Any], bot: Bot
) -> list[FileAttachment]:
    """Download documents from one or more Telegram messages and return as FileAttachments.

    Telegram documents include PDFs, text files, and other non-photo file
    uploads.  Each message may have at most one document.
    """
    attachments: list[FileAttachment] = []
    for message in messages:
        if not message.document:
            continue
        doc = message.document
        file = await bot.get_file(doc.file_id)
        doc_bytes = bytes(await file.download_as_bytearray())
        mime_type = doc.mime_type or "application/octet-stream"
        filename = doc.file_name
        attachments.append(FileAttachment(data=doc_bytes, mime_type=mime_type, filename=filename))
    return attachments


async def _download_telegram_voice(
    message: Any, bot: Bot
) -> bytes | None:
    """Download a voice note or video note from a Telegram message.

    Returns the raw audio bytes, or None if the message has no voice/video note.
    """
    voice = message.voice or message.video_note
    if not voice:
        return None
    file = await bot.get_file(voice.file_id)
    return bytes(await file.download_as_bytearray())


async def _download_all_attachments(
    messages: list[Any], bot: Bot
) -> list[FileAttachment]:
    """Download all photos and documents from messages concurrently."""
    photos, docs = await asyncio.gather(
        _download_telegram_photos(messages, bot),
        _download_telegram_documents(messages, bot),
    )
    return photos + docs


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text, photo, and document messages: route to the agent.

    For media groups (albums with multiple photos), messages are batched
    using a short delay so all photos are collected before processing.
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message:
        return

    # Must have text, caption, photo, document, location, or voice
    has_text = bool(message.text)
    has_photo = bool(message.photo)
    has_document = bool(message.document)
    has_caption = bool(message.caption)
    has_location = bool(message.location)
    has_voice = bool(message.voice or message.video_note)

    logger.info(
        "message_handler: chat=%s has_text=%s has_photo=%s has_document=%s has_caption=%s has_location=%s has_voice=%s media_group_id=%s",
        message.chat_id, has_text, has_photo, has_document, has_caption, has_location, has_voice, message.media_group_id,
    )

    if not has_text and not has_photo and not has_document and not has_location and not has_voice:
        logger.info("message_handler: no text, photo, document, location, or voice, ignoring")
        return

    if not _is_authorized(update.effective_user and update.effective_user.id, config):
        logger.info("message_handler: unauthorized user %s", update.effective_user)
        return

    scope = chat_scope_from_message(message)

    # Check if this is a text response to an "Other..." question prompt.
    # If there's a pending "Other" input for this scope, resolve it inline
    # and don't dispatch to the agent.
    if has_text and scope in _pending_other_input:
        question_id = _pending_other_input.pop(scope, None)
        if question_id:
            state = _question_states.get(question_id)
            if state and not state.future.done():
                custom_text = message.text or ""
                state.waiting_for_other = False
                await _complete_other_input(context.bot, state, custom_text)
                logger.info("Resolved pending 'Other' input for scope %s", scope)
                return

    bot_username = (await context.bot.get_me()).username or ""
    if not _is_bot_addressed(update, bot_username):
        logger.info("message_handler: bot not addressed, ignoring")
        return

    # If this message is part of a media group (album), batch it.
    if message.media_group_id and (has_photo or has_document):
        await _handle_media_group_message(update, context, message)
        return

    # Transcribe voice notes to text.
    if has_voice:
        try:
            voice_data = await _download_telegram_voice(message, context.bot)
            if voice_data:
                transcription = await stt_transcribe(voice_data)
                if transcription:
                    logger.info(
                        "Voice transcription for scope %s: %s",
                        scope, transcription[:100],
                    )
                    prompt = f"[Transcribed from voice note] {transcription}"
                    await _dispatch_to_agent(
                        prompt, [], scope, config, db, context,
                        user_id=update.effective_user.id,
                        is_private_chat=update.effective_chat.type == "private" if update.effective_chat else True,
                    )
                    return
                else:
                    logger.warning("Empty transcription for voice note in scope %s", scope)
        except Exception:
            logger.exception("Voice transcription failed for scope %s", scope)
            try:
                await context.bot.send_message(
                    chat_id=scope.chat_id,
                    text="Failed to transcribe voice note\\. Is moonshine\\-stt installed?",
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(scope),
                )
            except Exception:
                pass
        return

    # Extract text from either message.text or message.caption (for photos)
    raw_text = message.text or message.caption or ""
    prompt = _strip_mention(raw_text, bot_username)

    # Build location context string if a location was shared
    if has_location:
        loc = message.location
        location_text = f"User shared location: {loc.latitude}, {loc.longitude}"
        if loc.horizontal_accuracy:
            location_text += f" (accuracy: {loc.horizontal_accuracy}m)"
        if loc.heading is not None:
            location_text += f" (heading: {loc.heading}\u00b0)"
        # Prepend location to any existing prompt text
        prompt = f"{location_text}\n\n{prompt}" if prompt else location_text
        logger.info("Location shared in scope %s: %s, %s", scope, loc.latitude, loc.longitude)

    # For photos without a caption, use a default prompt
    if not prompt and has_photo:
        prompt = "What's in this image?"
    elif not prompt and has_document:
        prompt = "What's in this file?"
    elif not prompt:
        return

    # Download photo and document attachments if present
    attachments: list[FileAttachment] = []
    if has_photo or has_document:
        attachments = await _download_all_attachments([message], context.bot)
        logger.info(
            "Downloaded %d attachment(s) for scope %s (%d bytes)",
            len(attachments), scope, sum(len(att.data) for att in attachments),
        )

    await _dispatch_to_agent(
        prompt, attachments, scope, config, db, context,
        user_id=update.effective_user.id if update.effective_user else 0,
        is_private_chat=update.effective_chat.type == "private" if update.effective_chat else True,
    )


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle data sent from a Telegram Mini App (e.g. commit from review app).

    The review Mini App uses ``WebApp.sendData()`` to send a JSON payload
    back to the bot.  Telegram delivers this as a message with
    ``web_app_data`` set.
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not message.web_app_data:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if not _is_authorized(user_id, config):
        logger.info("web_app_data_handler: unauthorized user %s", update.effective_user)
        return

    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid web_app_data JSON: %s", message.web_app_data.data)
        return

    action = payload.get("action")
    if action == "commit":
        scope = chat_scope_from_message(message)
        prompt = (
            "Please commit the currently staged changes. "
            "Generate an appropriate commit message based on the staged diff."
        )
        await _dispatch_to_agent(
            prompt, [], scope, config, db, context,
            user_id=update.effective_user.id if update.effective_user else 0,
            is_private_chat=message.chat.type == "private" if message.chat else True,
        )
    else:
        logger.warning("Unknown web_app_data action: %s", action)


# ---------------------------------------------------------------------------
# Media group batching
# ---------------------------------------------------------------------------


async def _handle_media_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: Any
) -> None:
    """Collect media group messages and dispatch once the batch is complete.

    Telegram sends each photo in an album as a separate Update with the same
    media_group_id.  We accumulate them and use a short timer to detect when
    the batch is complete (no new messages for _MEDIA_GROUP_WAIT seconds).
    """
    group_id = message.media_group_id
    if group_id not in _media_group_messages:
        _media_group_messages[group_id] = []
    _media_group_messages[group_id].append(message)

    logger.info(
        "Media group %s: collected %d message(s) so far",
        group_id, len(_media_group_messages[group_id]),
    )

    # Cancel the previous timer for this group (we got another message).
    existing_task = _media_group_tasks.pop(group_id, None)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    # Start a new timer.  When it fires, the batch is considered complete.
    async def _process_group() -> None:
        await asyncio.sleep(_MEDIA_GROUP_WAIT)
        messages = _media_group_messages.pop(group_id, [])
        _media_group_tasks.pop(group_id, None)
        if not messages:
            return

        config: Config = context.bot_data["config"]
        db: aiosqlite.Connection = context.bot_data["db"]
        scope = chat_scope_from_message(messages[0])
        bot_username = (await context.bot.get_me()).username or ""

        # Extract caption from the first message that has one.
        raw_text = ""
        for msg in messages:
            if msg.caption:
                raw_text = msg.caption
                break

        prompt = _strip_mention(raw_text, bot_username) if raw_text else ""
        if not prompt:
            has_any_photo = any(msg.photo for msg in messages)
            has_any_doc = any(msg.document for msg in messages)
            if has_any_photo and not has_any_doc:
                count = len(messages)
                prompt = f"What's in {'this image' if count == 1 else 'these images'}?"
            elif has_any_doc and not has_any_photo:
                count = sum(1 for msg in messages if msg.document)
                prompt = f"What's in {'this file' if count == 1 else 'these files'}?"
            else:
                prompt = "What's in these files?"

        attachments = await _download_all_attachments(messages, context.bot)
        logger.info(
            "Media group %s complete: %d attachment(s) for scope %s (%d bytes)",
            group_id, len(attachments), scope, sum(len(att.data) for att in attachments),
        )

        await _dispatch_to_agent(
            prompt, attachments, scope, config, db, context,
            user_id=update.effective_user.id if update.effective_user else 0,
            is_private_chat=update.effective_chat.type == "private" if update.effective_chat else True,
        )

    _media_group_tasks[group_id] = asyncio.create_task(_process_group())


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------


async def _dispatch_to_agent(
    prompt: str,
    attachments: list[FileAttachment],
    scope: ChatScope,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    placeholder: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
) -> None:
    """Submit a Telegram prompt through the per-scope SessionRunner."""
    supersede_prompt_suggestion(scope)
    lock = _scope_dispatch_locks.setdefault(scope, asyncio.Lock())
    async with lock:
        if placeholder:
            try:
                await context.bot.send_message(
                    chat_id=scope.chat_id,
                    text=placeholder,
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(scope),
                )
            except Exception:
                logger.debug("Failed to send placeholder for scope %s", scope)
        runner = await get_or_start_runner(
            scope=scope,
            config=config,
            db=db,
            context=context,
            user_id=user_id,
            is_private_chat=is_private_chat,
        )
        await runner.submit(RunnerInput(prompt, attachments, source="telegram"))
        return

async def _wake_parent_for_agent_notifications(
    scope: ChatScope,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int = 0,
    is_private_chat: bool = True,
) -> None:
    """Wake the per-scope runner so it can submit Agent notifications."""
    runner = await get_or_start_runner(
        scope=scope,
        config=config,
        db=db,
        context=context,
        user_id=user_id,
        is_private_chat=is_private_chat,
    )
    await runner.wake_for_notifications()
