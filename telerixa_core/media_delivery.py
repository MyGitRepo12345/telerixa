from dataclasses import dataclass
from io import BytesIO
import json
import logging
import os
import shutil
import tempfile

import aiohttp

from i18n import tr
from .discord_delivery import (
    classify_discord_http_error,
    retry_result_for_exception,
    send_text_chunks,
)
from .formatting import build_message_text, select_album_text_message, split_text
from .models import SendResult
from .rich_messages import (
    get_message_media_attachments,
    get_rich_media_attachments,
    has_rich_message,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaDeliverySettings:
    webhook_url: str
    max_message_length: int
    file_limit_mb: int
    large_file_action: str


def _media_attachment_key(attachment):
    media = attachment.media
    media_id = getattr(media, "id", None)
    return (type(media), media_id if media_id is not None else id(media))


def _collect_media_attachments(messages):
    attachments = []
    seen_keys = set()
    for message in messages:
        for attachment in get_message_media_attachments(message):
            key = _media_attachment_key(attachment)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            attachments.append(attachment)
    return attachments


def _apply_spoiler_filename(file_path):
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    if filename.startswith("SPOILER_"):
        return file_path
    spoiler_path = os.path.join(directory, f"SPOILER_{filename}")
    os.replace(file_path, spoiler_path)
    return spoiler_path


async def send_to_discord(
    telegram_client,
    telegram_message,
    channel_name,
    settings,
    get_album_messages,
    get_message_datetime,
    next_chunk_index=0,
    media_sent=False,
    rendered_text=None,
    progress_callback=None,
):
    """Send a Telegram message and media through a Discord webhook."""
    temp_files = []
    temp_dirs = []

    try:
        text_message = telegram_message
        message_time = get_message_datetime(telegram_message)
        album_messages = [telegram_message]

        if telegram_message.grouped_id and not (media_sent or next_chunk_index > 0):
            album_messages = await get_album_messages(channel_name, telegram_message)
            text_message = select_album_text_message(album_messages, telegram_message)
            message_time = get_message_datetime(text_message)

        text = rendered_text
        if text is None:
            if has_rich_message(text_message):
                rich_message = text_message.rich_message
                logger.info(
                    tr(
                        "media.rich_detected",
                        message_id=text_message.id,
                        blocks=len(getattr(rich_message, "blocks", []) or []),
                        media=len(get_rich_media_attachments(rich_message)),
                    )
                )
            text = await build_message_text(
                telegram_message,
                channel_name,
                text_message,
                telegram_client=telegram_client,
            )
        if progress_callback is not None:
            progress_callback(rendered_text=text)

        text_chunks = split_text(text, settings.max_message_length)
        next_chunk_index = max(
            0,
            min(int(next_chunk_index or 0), len(text_chunks)),
        )

        if media_sent or next_chunk_index > 0:
            if next_chunk_index >= len(text_chunks):
                return SendResult.success()
            async with aiohttp.ClientSession() as session:
                return await send_text_chunks(
                    session,
                    settings.webhook_url,
                    text_chunks[next_chunk_index:],
                    channel_name,
                    message_time,
                    start_part=next_chunk_index + 1,
                    total_parts=len(text_chunks),
                    progress_callback=progress_callback,
                    media_sent=media_sent,
                )

        media_files = []
        media_download_failed = False
        media_download_errors = []

        media_messages = (
            album_messages if telegram_message.grouped_id else [telegram_message]
        )
        media_attachments = _collect_media_attachments(media_messages)
        if media_attachments:
            try:
                temp_dir = tempfile.mkdtemp()
                temp_dirs.append(temp_dir)
                for attachment in media_attachments:
                    try:
                        file_path = await telegram_client.download_media(
                            attachment.media,
                            temp_dir,
                        )
                        if not file_path:
                            media_download_failed = True
                            error = RuntimeError(
                                tr(
                                    "send.media_path_missing",
                                    media_type=type(attachment.media).__name__,
                                )
                            )
                            media_download_errors.append(error)
                            logger.warning(
                                tr(
                                    "media.album_path_missing"
                                    if telegram_message.grouped_id
                                    else "media.path_missing",
                                    media_type=type(attachment.media).__name__,
                                )
                            )
                            continue
                        if attachment.spoiler:
                            file_path = _apply_spoiler_filename(file_path)
                        temp_files.append(file_path)
                        media_files.append(file_path)
                    except Exception as error:
                        media_download_failed = True
                        media_download_errors.append(error)
                        logger.warning(
                            tr(
                                "media.album_download_failed"
                                if telegram_message.grouped_id
                                else "media.download_failed",
                                error=error,
                            )
                        )
            except Exception as error:
                media_download_failed = True
                media_download_errors.append(error)
                logger.error(
                    tr(
                        "media.album_processing_failed"
                        if telegram_message.grouped_id
                        else "media.download_failed",
                        error=error,
                    )
                )

        if media_download_failed:
            logger.warning(tr("media.partial_download"))
            if media_download_errors:
                return retry_result_for_exception(media_download_errors[-1])
            return SendResult.retry(tr("send.media_download_failed"))

        async with aiohttp.ClientSession() as session:
            if text and not media_files:
                return await send_text_chunks(
                    session,
                    settings.webhook_url,
                    text_chunks,
                    channel_name,
                    message_time,
                    progress_callback=progress_callback,
                )

            if media_files:
                first_chunk = text_chunks[0] if text_chunks else ""
                payload = {
                    "embeds": [
                        {
                            "description": first_chunk,
                            "color": 3447003,
                            "timestamp": message_time.isoformat(),
                            "footer": {
                                "text": tr(
                                    "telegram.footer_from_channel",
                                    channel=channel_name,
                                    part_suffix="",
                                ),
                            },
                            "author": {
                                "name": tr(
                                    "telegram.author_media_from_channel",
                                    channel=channel_name,
                                ),
                            },
                        }
                    ]
                }

                form_data = aiohttp.FormData()
                form_data.add_field(
                    "payload_json",
                    json.dumps(payload, ensure_ascii=False),
                    content_type="application/json",
                )

                has_valid_files = False
                skipped_large_files = []
                for index, file_path in enumerate(media_files):
                    if not os.path.exists(file_path):
                        continue

                    file_size = os.path.getsize(file_path)
                    if file_size > settings.file_limit_mb * 1024 * 1024:
                        skipped_large_files.append(file_path)
                        if settings.large_file_action == "try_send_then_text":
                            logger.warning(
                                tr(
                                    "media.file_too_large_try",
                                    file_path=file_path,
                                    limit=settings.file_limit_mb,
                                )
                            )
                        else:
                            logger.warning(
                                tr(
                                    "media.file_too_large_skip_attach",
                                    file_path=file_path,
                                    limit=settings.file_limit_mb,
                                )
                            )
                            continue

                    with open(file_path, "rb") as file:
                        file_bytes = BytesIO(file.read())

                    form_data.add_field(
                        f"file{index}",
                        file_bytes,
                        filename=os.path.basename(file_path),
                    )
                    has_valid_files = True

                if not has_valid_files:
                    if (
                        skipped_large_files
                        and settings.large_file_action == "send_text_link"
                    ):
                        logger.warning(tr("media.all_large_send_text_link"))
                        if text_chunks:
                            return await send_text_chunks(
                                session,
                                settings.webhook_url,
                                text_chunks,
                                channel_name,
                                message_time,
                                progress_callback=progress_callback,
                            )
                        return SendResult.success()

                    if (
                        skipped_large_files
                        and settings.large_file_action == "skip_post"
                    ):
                        logger.warning(tr("media.all_large_skip_post"))
                        return SendResult.success()

                    logger.warning(tr("media.no_files_to_send"))
                    return SendResult.retry(tr("send.no_valid_media"))

                try:
                    timeout = aiohttp.ClientTimeout(total=60)
                    async with session.post(
                        settings.webhook_url,
                        data=form_data,
                        timeout=timeout,
                    ) as response:
                        if response.status not in [200, 204]:
                            error = (
                                "Discord webhook media upload error "
                                f"{response.status}"
                            )
                            logger.error(
                                tr(
                                    "discord.media_upload_error",
                                    status=response.status,
                                )
                            )
                            if response.status == 413:
                                if settings.large_file_action == "skip_post":
                                    logger.warning(tr("media.discord_413_skip"))
                                    return SendResult.success()
                                logger.warning(
                                    tr(
                                        "media.discord_413_text_link",
                                        limit=settings.file_limit_mb,
                                    )
                                )
                                if text_chunks:
                                    return await send_text_chunks(
                                        session,
                                        settings.webhook_url,
                                        text_chunks,
                                        channel_name,
                                        message_time,
                                        progress_callback=progress_callback,
                                    )
                                return SendResult.terminal_failure(error)
                            return classify_discord_http_error(
                                response.status,
                                error,
                            )

                    if progress_callback is not None:
                        progress_callback(
                            next_chunk_index=1 if text_chunks else 0,
                            media_sent=True,
                        )
                    if len(text_chunks) > 1:
                        return await send_text_chunks(
                            session,
                            settings.webhook_url,
                            text_chunks[1:],
                            channel_name,
                            message_time,
                            start_part=2,
                            total_parts=len(text_chunks),
                            progress_callback=progress_callback,
                            media_sent=True,
                        )
                    return SendResult.success()
                except Exception as error:
                    retry_result = retry_result_for_exception(error)
                    logger.error(
                        tr(
                            "media.discord_send_failed",
                            error=retry_result.error,
                        )
                    )
                    return retry_result

            return SendResult.retry(tr("send.nothing_to_send"))

    except Exception as error:
        retry_result = retry_result_for_exception(error)
        logger.error(
            tr(
                "media.message_send_failed",
                error=retry_result.error,
            )
        )
        return retry_result

    finally:
        for file_path in temp_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass

        temp_root = os.path.abspath(tempfile.gettempdir())
        for temp_dir in set(temp_dirs):
            try:
                temp_dir = os.path.abspath(temp_dir)
                if (
                    os.path.exists(temp_dir)
                    and os.path.commonpath([temp_root, temp_dir]) == temp_root
                ):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except (OSError, ValueError):
                pass
