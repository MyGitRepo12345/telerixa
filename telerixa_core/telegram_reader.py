import asyncio
from dataclasses import dataclass
import logging
from typing import Optional

from i18n import tr
from .constants import (
    ALBUM_LOOKUP_RADIUS,
    ALBUM_MESSAGES_CACHE_ATTR,
    CHANNEL_FETCH_CONCURRENCY,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelCollection:
    channel: str
    channel_order: int
    last_seen_id: Optional[int]
    latest_message_id: int
    messages: list
    has_older_forwardable: bool = False


@dataclass(frozen=True)
class CollectedPost:
    channel: str
    channel_order: int
    message: object


def get_message_datetime(message, app_timezone):
    """Return a Telegram message datetime in the configured timezone."""
    try:
        return message.date.astimezone(app_timezone)
    except Exception:
        return message.date


async def get_latest_message_id(telegram_client, entity):
    latest_messages = await telegram_client.get_messages(entity, limit=1)
    return latest_messages[0].id if latest_messages else 0


def is_forwardable_message(message):
    return bool(message and (message.text or message.media))


async def collect_startup_tail(telegram_client, entity, last_seen_id, limit):
    """Collect the latest posts after last_seen_id without assuming dense IDs."""
    if limit <= 0:
        return [], False

    selected_messages = []
    seen_grouped_ids = set()
    has_older_forwardable = False

    async for message in telegram_client.iter_messages(entity, min_id=last_seen_id):
        if not is_forwardable_message(message):
            continue

        if message.grouped_id:
            if message.grouped_id in seen_grouped_ids:
                continue
            seen_grouped_ids.add(message.grouped_id)

        if len(selected_messages) >= limit:
            has_older_forwardable = True
            break

        selected_messages.append(message)

    selected_messages.sort(key=lambda item: item.id)
    return selected_messages, has_older_forwardable


async def collect_new_messages(telegram_client, entity, last_seen_id):
    messages = []
    async for message in telegram_client.iter_messages(
        entity,
        min_id=last_seen_id,
        reverse=True,
    ):
        if is_forwardable_message(message):
            messages.append(message)
    return messages


async def collect_channel(
    telegram_client,
    channel,
    channel_order,
    last_seen_id,
    startup_limit=None,
):
    entity = await telegram_client.get_entity(f"@{channel}")
    latest_message_id = await get_latest_message_id(telegram_client, entity)
    messages = []
    has_older_forwardable = False

    if last_seen_id is not None and latest_message_id > last_seen_id:
        if startup_limit is None:
            messages = await collect_new_messages(
                telegram_client,
                entity,
                last_seen_id,
            )
        elif startup_limit > 0:
            messages, has_older_forwardable = await collect_startup_tail(
                telegram_client,
                entity,
                last_seen_id,
                startup_limit,
            )

    return ChannelCollection(
        channel=channel,
        channel_order=channel_order,
        last_seen_id=last_seen_id,
        latest_message_id=latest_message_id,
        messages=messages,
        has_older_forwardable=has_older_forwardable,
    )


async def collect_channels(
    telegram_client,
    channels,
    last_seen_ids,
    startup_limit=None,
    concurrency_limit=CHANNEL_FETCH_CONCURRENCY,
):
    if not channels:
        return []

    semaphore = asyncio.Semaphore(
        max(1, min(int(concurrency_limit), len(channels)))
    )

    async def collect_with_limit(channel, channel_order):
        async with semaphore:
            return await collect_channel(
                telegram_client,
                channel,
                channel_order,
                last_seen_ids.get(channel),
                startup_limit=startup_limit,
            )

    tasks = [
        collect_with_limit(channel, channel_order)
        for channel_order, channel in enumerate(channels)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return list(zip(channels, results))


def get_message_timestamp(message, fallback=None):
    message_date = getattr(message, "date", None)
    try:
        return float(message_date.timestamp())
    except (AttributeError, TypeError, ValueError, OSError):
        return fallback


def merge_chronological_posts(collections):
    posts = []
    seen_grouped_ids = set()

    for collection in collections:
        for message in collection.messages:
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id:
                grouped_key = (collection.channel, grouped_id)
                if grouped_key in seen_grouped_ids:
                    continue
                seen_grouped_ids.add(grouped_key)

            posts.append(
                CollectedPost(
                    channel=collection.channel,
                    channel_order=collection.channel_order,
                    message=message,
                )
            )

    posts.sort(
        key=lambda post: (
            get_message_timestamp(post.message, float("inf")),
            post.channel_order,
            int(post.message.id),
        )
    )
    return posts


def format_album_ids(album_messages):
    return ", ".join(str(album_message.id) for album_message in album_messages)


async def get_album_messages(
    telegram_client,
    channel_name,
    message,
    lookup_radius=ALBUM_LOOKUP_RADIUS,
    cache_attr=ALBUM_MESSAGES_CACHE_ATTR,
):
    if not message.grouped_id:
        return [message]

    cached_album_messages = getattr(message, cache_attr, None)
    if cached_album_messages is not None:
        return cached_album_messages

    start_id = max(1, message.id - lookup_radius)
    end_id = message.id + lookup_radius
    nearby_ids = list(range(start_id, end_id + 1))

    try:
        messages_batch = await telegram_client.get_messages(
            f"@{channel_name}",
            ids=nearby_ids,
        )
        album_messages = [
            album_message
            for album_message in messages_batch
            if album_message and album_message.grouped_id == message.grouped_id
        ]
        album_messages.sort(key=lambda album_message: album_message.id)
    except Exception as error:
        logger.warning(
            tr(
                "media.album_lookup_failed",
                channel=channel_name,
                message_id=message.id,
                grouped_id=message.grouped_id,
                error=error,
            )
        )
        fallback_album_messages = [message]
        try:
            setattr(message, cache_attr, fallback_album_messages)
        except Exception:
            pass
        return fallback_album_messages

    if not album_messages:
        album_messages = [message]

    try:
        setattr(message, cache_attr, album_messages)
    except Exception:
        pass

    album_ids = format_album_ids(album_messages)
    if len(album_messages) <= 1:
        logger.warning(
            tr(
                "media.album_collected_single",
                channel=channel_name,
                grouped_id=message.grouped_id,
                message_id=message.id,
                radius=lookup_radius,
                count=len(album_messages),
                ids=album_ids,
            )
        )
    else:
        logger.info(
            tr(
                "media.album_collected",
                channel=channel_name,
                grouped_id=message.grouped_id,
                count=len(album_messages),
                ids=album_ids,
            )
        )

    return album_messages


async def get_album_message_ids(telegram_client, channel_name, message):
    album_messages = await get_album_messages(
        telegram_client,
        channel_name,
        message,
    )
    return [album_message.id for album_message in album_messages]
