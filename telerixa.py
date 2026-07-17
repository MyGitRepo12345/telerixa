from telethon import TelegramClient
import asyncio
import inspect
import json
from datetime import datetime
from functools import partial
import logging
import os
import sys
from typing import Any, cast

from i18n import configure_language, tr
from telerixa_core.config import ConfigError, ConfigManager
from telerixa_core.constants import (
    APP_NAME,
    BOT_PID_FILE,
    CONFIG_FILE,
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    RUNTIME_SERVICE_NAME,
    VERSION as __version__,
)
from telerixa_core.lifecycle import (
    AlreadyRunningError,
    DetachedProcessError,
    ProcessLifetimeMonitor,
    ProcessLock,
    ShutdownSignalHandlers,
    require_attached_console,
)
from telerixa_core.discord_delivery import (
    notify_dropped_message,
    retry_result_for_exception,
)
from telerixa_core.delivery import (
    DeliverySettings,
    DeliveryStateActions,
    drop_pending_message as delivery_drop_pending_message,
    handle_pending_send_failure as delivery_handle_pending_send_failure,
    make_delivery_progress_callback as delivery_make_progress_callback,
    prepare_pending_delivery as delivery_prepare_pending_delivery,
)
from telerixa_core.logging_setup import log_success, setup_logging
from telerixa_core.ffmpeg_tools import (
    FFmpegSetupError,
    ensure_ffmpeg_tools,
    find_ffmpeg_tools,
)
from telerixa_core.media_delivery import (
    MediaDeliverySettings,
    send_to_discord as send_media_to_discord,
)
from telerixa_core.models import SendResult
from telerixa_core import state as state_store
from telerixa_core import telegram_reader


setup_logging()
logger = logging.getLogger(__name__)

# ===== FILE-BASED CONFIGURATION =====


def as_send_result(value, fallback_error=None):
    if fallback_error is None:
        fallback_error = tr("send.false")
    if isinstance(value, SendResult):
        return value
    if value:
        return SendResult.success()
    return SendResult.retry(fallback_error)


async def await_telethon_call(value: Any) -> Any:
    """Await Telethon's runtime-dependent sync/async return values."""
    if inspect.isawaitable(value):
        return await value
    return value


def log_config_warning(config_warning):
    if config_warning.kind == "invalid_large_file_action":
        logger.warning(
            tr("config.invalid_large_file_action", action=repr(config_warning.value))
        )
    elif config_warning.kind == "timezone_not_found":
        logger.warning(
            tr("config.timezone_not_found", timezone=config_warning.value)
        )
    elif config_warning.kind == "invalid_video_transcode_preset":
        logger.warning(
            tr(
                "config.invalid_video_transcode_preset",
                preset=repr(config_warning.value),
            )
        )


def log_config_error(error):
    if error.kind == "file_missing":
        logger.error(tr("config.file_missing", file=error.path))
    elif error.kind in {"invalid_json", "invalid_root"}:
        logger.error(tr("config.invalid_json", file=error.path))
    elif error.kind == "required_missing":
        logger.error(tr("config.required_missing"))
    else:
        logger.warning(
            tr("config.check_failed", file=error.path, error=error.detail or error)
        )


config_manager = ConfigManager(CONFIG_FILE)
try:
    initial_config_update = config_manager.load_initial()
except ConfigError as error:
    log_config_error(error)
    sys.exit(1)

runtime_config = initial_config_update.config
configure_language(runtime_config.language)
for warning in initial_config_update.warnings:
    log_config_warning(warning)

# Restart-only storage settings remain stable for the lifetime of the process.
STATE_DB_FILE = runtime_config.state_db_file
DB_TIMEOUT_SECONDS = runtime_config.db_timeout_seconds
DB_BUSY_TIMEOUT_MS = runtime_config.db_busy_timeout_ms
QUEUE_RETRY_LIMIT = runtime_config.queue_retry_limit


def get_now_ts():
    return datetime.now(runtime_config.app_timezone).timestamp()


def format_ts(timestamp):
    return datetime.fromtimestamp(
        float(timestamp),
        runtime_config.app_timezone,
    ).isoformat()


def reload_config_if_changed():
    global runtime_config
    try:
        config_update = config_manager.reload_if_changed()
    except ConfigError as error:
        log_config_error(error)
        if error.kind not in {"check_failed", "file_missing"}:
            logger.warning(tr("config.keep_previous"))
        return

    if config_update is None:
        return

    runtime_config = config_update.config
    configure_language(runtime_config.language)
    for warning in config_update.warnings:
        log_config_warning(warning)
    if config_update.changed_keys:
        log_success(logger, tr("config.updated", keys=", ".join(config_update.changed_keys)))


async def sleep_with_config_reload(duration):
    remaining = max(0, duration)
    while remaining > 0:
        step = min(2, remaining)
        await asyncio.sleep(step)
        reload_config_if_changed()
        remaining -= step

# ===== INITIALIZATION =====

telegram_client = TelegramClient(
    "tg_session",
    runtime_config.telegram_api_id,
    runtime_config.telegram_api_hash,
    device_model="Pixel 5",
    system_version="11",
    app_version="8.4.1",
    lang_code="en",
    system_lang_code="en-US",
)

# The old seen_messages.json file is used only for a soft SQLite migration.
SEEN_MESSAGES_FILE = "seen_messages.json"


def load_legacy_seen_messages():
    """Load the old sent-message list for migration."""
    try:
        with open(SEEN_MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            logger.warning(tr("legacy.seen_not_list", file=SEEN_MESSAGES_FILE))
            return []
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logger.warning(tr("legacy.seen_broken", file=SEEN_MESSAGES_FILE))
        return []


def connect_state_db():
    return state_store.connect_state_db(STATE_DB_FILE, DB_TIMEOUT_SECONDS, DB_BUSY_TIMEOUT_MS)


def init_state_db():
    state_store.init_state_db(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        get_now_ts(),
    )


def record_runtime_update(operation_name, callback, *args, log_failure=True):
    try:
        callback(
            STATE_DB_FILE,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            *args,
        )
        return True
    except Exception as e:
        if log_failure:
            logger.warning(
                tr(
                    "runtime.state_write_failed",
                    operation=operation_name,
                    error=e,
                )
            )
        return False


def record_runtime_started():
    now_ts = get_now_ts()
    return record_runtime_update(
        "started",
        state_store.mark_runtime_started,
        RUNTIME_SERVICE_NAME,
        os.getpid(),
        now_ts,
        format_ts(now_ts),
    )


def record_runtime_heartbeat(log_failure=True):
    now_ts = get_now_ts()
    return record_runtime_update(
        "heartbeat",
        state_store.touch_runtime_heartbeat,
        RUNTIME_SERVICE_NAME,
        os.getpid(),
        now_ts,
        format_ts(now_ts),
        log_failure=log_failure,
    )


def record_runtime_cycle_started():
    now_ts = get_now_ts()
    return record_runtime_update(
        "cycle_started",
        state_store.mark_runtime_cycle_started,
        RUNTIME_SERVICE_NAME,
        now_ts,
        format_ts(now_ts),
    )


def record_runtime_cycle_finished(result, error=""):
    now_ts = get_now_ts()
    return record_runtime_update(
        "cycle_finished",
        state_store.mark_runtime_cycle_finished,
        RUNTIME_SERVICE_NAME,
        result,
        error,
        now_ts,
        format_ts(now_ts),
    )


def record_runtime_stopped(status="stopped", error=""):
    now_ts = get_now_ts()
    return record_runtime_update(
        "stopped",
        state_store.mark_runtime_stopped,
        RUNTIME_SERVICE_NAME,
        status,
        error,
        now_ts,
        format_ts(now_ts),
    )


def _format_runtime_megabytes(size_bytes):
    return f"{int(size_bytes or 0) / (1024 * 1024):.1f} MB"


def record_runtime_transcode_event(event, details):
    channel = str(details.get("channel") or "")
    message_id = details.get("message_id") or "-"
    source_size = _format_runtime_megabytes(details.get("source_size"))
    reference = f"@{channel}/{message_id}"

    if event == "started":
        activity = "transcoding"
        result = ""
        detail = tr(
            "runtime.transcode_started",
            reference=reference,
            source_size=source_size,
            limit=details.get("limit_mb"),
            preset=details.get("preset"),
        )
    elif event == "succeeded":
        activity = ""
        result = "success"
        detail = tr(
            "runtime.transcode_succeeded",
            reference=reference,
            source_size=source_size,
            output_size=_format_runtime_megabytes(details.get("output_size")),
            duration=round(float(details.get("duration_seconds") or 0), 1),
            attempts=details.get("attempts") or 0,
        )
    else:
        activity = ""
        result = "failed"
        detail = tr(
            "runtime.transcode_failed",
            reference=reference,
            source_size=source_size,
            error=details.get("error") or "unknown error",
        )

    now_ts = get_now_ts()
    record_runtime_update(
        "transcode_status",
        state_store.update_runtime_activity,
        RUNTIME_SERVICE_NAME,
        activity,
        detail,
        result,
        now_ts,
        format_ts(now_ts),
    )


async def runtime_heartbeat_loop():
    failure_reported = False
    while True:
        heartbeat_saved = record_runtime_heartbeat(
            log_failure=not failure_reported,
        )
        if heartbeat_saved and failure_reported:
            log_success(logger, tr("runtime.heartbeat_recovered"))
        failure_reported = not heartbeat_saved
        await asyncio.sleep(RUNTIME_HEARTBEAT_INTERVAL_SECONDS)


def ensure_pending_message_columns(conn):
    state_store.ensure_pending_message_columns(conn, get_now_ts())


def get_last_seen_id(channel):
    return state_store.get_last_seen_id(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
    )


def set_last_seen_id(channel, message_id):
    state_store.set_last_seen_id(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        format_ts(get_now_ts()),
    )


def advance_last_seen_id(channel, message_id):
    state_store.advance_last_seen_id(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        format_ts(get_now_ts()),
    )


def get_processed_message_state(channel, message_id):
    return state_store.get_processed_message_state(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
    )


def get_processed_group_message_ids(channel, grouped_id):
    return state_store.get_processed_group_message_ids(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        grouped_id,
    )


def has_pending_message(channel, message_id=None, grouped_id=None):
    return state_store.has_pending_message(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        grouped_id,
    )


def is_processed_message(channel, message_id):
    status, grouped_id = get_processed_message_state(channel, message_id)
    if status is None:
        return False

    if status == "queued" and not has_pending_message(channel, message_id, grouped_id):
        logger.warning(
            tr("processed.queued_without_pending", channel=channel, message_id=message_id)
        )
        return False

    return True


def mark_processed_message(channel, message_id, grouped_id=None, status="sent"):
    now_ts = get_now_ts()
    now_text = format_ts(now_ts)
    state_store.mark_processed_message(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        grouped_id,
        status,
        now_ts,
        now_text,
    )


def get_retry_delay_seconds(attempts):
    return state_store.get_retry_delay_seconds(attempts)


def add_pending_message(
    channel,
    message_id,
    grouped_id=None,
    error="",
    telegram_date_ts=None,
):
    now_ts = get_now_ts()
    now_text = format_ts(now_ts)
    state_store.add_pending_message(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        grouped_id,
        error,
        now_ts,
        now_text,
        telegram_date_ts,
    )


def mark_pending_failed(channel, message_id, error, count_attempt=True):
    now_ts = get_now_ts()
    now_text = format_ts(now_ts)
    current_attempts = state_store.get_pending_attempts(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
    )
    attempts, next_retry_ts = state_store.calculate_retry_schedule(
        current_attempts,
        count_attempt,
        now_ts,
    )
    next_retry_text = format_ts(next_retry_ts)
    state_store.update_pending_failure(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        attempts,
        error,
        now_ts,
        now_text,
        next_retry_ts,
        next_retry_text,
    )
    return attempts


def archive_pending_failure(
    channel,
    message_id,
    grouped_id,
    album_message_ids,
    reason,
    failure_kind,
    source,
    attempts,
):
    now_ts = get_now_ts()
    return state_store.archive_pending_failure(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        grouped_id,
        album_message_ids,
        reason,
        failure_kind,
        source,
        attempts,
        now_ts,
        format_ts(now_ts),
    )


def delete_pending_message(channel, message_id):
    state_store.delete_pending_message(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
    )


def get_due_pending_messages(limit=None):
    if limit is None:
        limit = QUEUE_RETRY_LIMIT

    return state_store.get_due_pending_messages(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        get_now_ts(),
        limit,
    )


def get_pending_count():
    return state_store.get_pending_count(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
    )


def get_pending_retry_status():
    return state_store.get_pending_retry_status(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        get_now_ts(),
    )


def update_pending_delivery_progress(
    channel,
    message_id,
    next_chunk_index=None,
    media_sent=None,
    rendered_text=None,
):
    state_store.update_pending_delivery_progress(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
        message_id,
        next_chunk_index,
        media_sent,
        rendered_text,
    )


DELIVERY_STATE_ACTIONS = DeliveryStateActions(
    add_pending_message=add_pending_message,
    archive_pending_failure=archive_pending_failure,
    advance_last_seen_id=advance_last_seen_id,
    delete_pending_message=delete_pending_message,
    get_processed_group_message_ids=get_processed_group_message_ids,
    mark_pending_failed=mark_pending_failed,
    mark_processed_message=mark_processed_message,
    update_pending_delivery_progress=update_pending_delivery_progress,
)


def get_delivery_settings():
    return DeliverySettings(
        max_queue_attempts=runtime_config.max_queue_attempts,
        webhook_url=runtime_config.discord_webhook_url,
        alert_user_id=runtime_config.discord_alert_user_id,
    )


def has_channel_state(channel):
    return state_store.has_channel_state(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        channel,
    )


def migrate_legacy_seen_messages():
    """Migrate the old seen_messages.json into SQLite when needed."""
    legacy_seen = load_legacy_seen_messages()
    if not legacy_seen:
        return

    now_ts = get_now_ts()
    now_text = format_ts(now_ts)
    migrated_channels, inserted = state_store.migrate_legacy_seen_messages(
        STATE_DB_FILE,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        legacy_seen,
        now_ts,
        now_text,
    )

    if migrated_channels:
        logger.info(tr("legacy.migrated_channels", count=migrated_channels))

    if inserted:
        logger.info(tr("legacy.processed_inserted", count=inserted))


def all_channels_initialized():
    return all(
        has_channel_state(channel)
        for channel in runtime_config.telegram_channels
    )


async def catch_up_channels_on_start():
    """Catch up only a small fresh tail on startup and skip deep backlog."""
    if not telegram_client.is_connected():
        await telegram_client.connect()

    config_snapshot = runtime_config
    logger.info(
        tr(
            "startup.catch_up_begin",
            limit=config_snapshot.startup_catch_up_limit,
        )
    )
    last_seen_ids = {
        channel: get_last_seen_id(channel)
        for channel in config_snapshot.telegram_channels
    }
    collection_results = await telegram_reader.collect_channels(
        telegram_client,
        config_snapshot.telegram_channels,
        last_seen_ids,
        startup_limit=config_snapshot.startup_catch_up_limit,
    )
    ready_collections = []
    startup_boundaries = {}

    for channel, result in collection_results:
        if isinstance(result, Exception):
            logger.error(tr("startup.channel_error", channel=channel, error=result))
            continue

        last_seen_id = result.last_seen_id
        latest_message_id = result.latest_message_id

        if last_seen_id is None:
            set_last_seen_id(channel, latest_message_id)
            logger.info(
                tr("startup.boundary_created", channel=channel, message_id=latest_message_id)
            )
            continue

        if latest_message_id <= last_seen_id:
            logger.info(
                tr(
                    "startup.no_backlog",
                    channel=channel,
                    last_seen=last_seen_id,
                    latest=latest_message_id,
                )
            )
            continue

        if config_snapshot.startup_catch_up_limit <= 0:
            skipped_count = latest_message_id - last_seen_id
            set_last_seen_id(channel, latest_message_id)
            logger.info(
                tr(
                    "startup.skipped_old_ids",
                    channel=channel,
                    last_seen=last_seen_id,
                    latest=latest_message_id,
                    count=skipped_count,
                )
            )
            continue

        if not result.messages:
            advance_last_seen_id(channel, latest_message_id)
            logger.info(tr("startup.no_forwardable_tail", channel=channel))
            continue

        startup_boundary_id = last_seen_id
        if result.has_older_forwardable:
            startup_boundary_id = max(
                last_seen_id,
                min(message.id for message in result.messages) - 1,
            )
            set_last_seen_id(channel, startup_boundary_id)
            logger.info(
                tr(
                    "startup.backlog_skipped",
                    channel=channel,
                    last_seen=last_seen_id,
                    latest=latest_message_id,
                    boundary=startup_boundary_id,
                    count=len(result.messages),
                )
            )

        startup_boundaries[channel] = startup_boundary_id
        ready_collections.append(result)
        logger.info(
            tr(
                "startup.tail_to_process",
                channel=channel,
                count=len(result.messages),
                boundary=startup_boundary_id,
                latest=latest_message_id,
            )
        )

    chronological_posts = telegram_reader.merge_chronological_posts(
        ready_collections
    )
    if not chronological_posts:
        return

    logger.info(
        tr(
            "channel.chronological_batch",
            count=len(chronological_posts),
            channels=len(ready_collections),
        )
    )
    stats_by_channel, last_processed_ids = await process_collected_posts(
        chronological_posts,
        startup_boundaries,
    )

    for collection in ready_collections:
        channel = collection.channel
        stats = stats_by_channel[channel]
        final_boundary = max(
            last_processed_ids.get(channel, startup_boundaries[channel]),
            collection.latest_message_id,
        )
        advance_last_seen_id(channel, final_boundary)
        logger.info(
            tr(
                "startup.tail_done",
                channel=channel,
                sent=stats["sent"],
                queued=stats["queued"],
                skipped=stats["skipped"],
                boundary=final_boundary,
            )
        )


async def process_pending_messages():
    """Retry messages that previously did not reach Discord."""
    pending_messages = get_due_pending_messages()
    if not pending_messages:
        pending_count, due_count, next_retry_ts = get_pending_retry_status()
        if pending_count:
            retry_delay = 0
            retry_text = tr("queue.retry_unknown")
            if next_retry_ts is not None:
                retry_delay = max(0, int(float(next_retry_ts) - get_now_ts()))
                retry_text = format_ts(next_retry_ts)
            logger.info(
                tr(
                    "queue.waiting",
                    pending=pending_count,
                    due=due_count,
                    delay=retry_delay,
                    retry_at=retry_text,
                )
            )
        return 0

    if not telegram_client.is_connected():
        await telegram_client.connect()

    delivered_count = 0
    logger.info(tr("queue.trying", count=len(pending_messages)))

    for (
        channel,
        message_id,
        grouped_id,
        attempts,
        last_error,
        next_chunk_index,
        media_sent,
        rendered_text,
    ) in pending_messages:
        try:
            entity = await telegram_client.get_entity(f"@{channel}")
            message = cast(
                Any,
                await telegram_client.get_messages(entity, ids=int(message_id)),
            )

            if not telegram_reader.is_forwardable_message(message):
                reason = tr("queue.message_unavailable", channel=channel, message_id=message_id)
                logger.warning(reason)
                delivery_drop_pending_message(
                    DELIVERY_STATE_ACTIONS,
                    logger,
                    channel,
                    message_id,
                    grouped_id,
                    reason,
                    attempts,
                    source="retry queue",
                    failure_kind="unavailable",
                )
                await notify_dropped_message(
                    runtime_config.discord_webhook_url,
                    runtime_config.discord_alert_user_id,
                    channel,
                    message_id,
                    reason,
                    attempts,
                )
                continue

            if next_chunk_index or media_sent:
                logger.info(
                    tr(
                        "queue.resuming_progress",
                        channel=channel,
                        message_id=message_id,
                        next_chunk=next_chunk_index,
                        media_sent=int(bool(media_sent)),
                    )
                )

            send_result = as_send_result(
                await send_to_discord(
                    message,
                    channel,
                    next_chunk_index=next_chunk_index,
                    media_sent=bool(media_sent),
                    rendered_text=rendered_text,
                    progress_callback=delivery_make_progress_callback(
                        DELIVERY_STATE_ACTIONS,
                        logger,
                        channel,
                        message_id,
                    ),
                ),
                tr("send.retry_false"),
            )
            if send_result:
                album_ids = await telegram_reader.get_album_message_ids(
                    telegram_client,
                    channel,
                    message,
                )
                processed_until_id = max(album_ids)
                grouped_value = grouped_id if grouped_id else message.grouped_id
                advance_last_seen_id(channel, processed_until_id)
                for album_message_id in album_ids:
                    mark_processed_message(channel, album_message_id, grouped_value, "sent")
                delete_pending_message(channel, message_id)
                delivered_count += 1
                log_success(
                    logger,
                    tr(
                        "queue.delivered",
                        channel=channel,
                        message_id=message_id,
                        boundary=processed_until_id,
                    )
                )
            else:
                kept, reason, attempts_after = await delivery_handle_pending_send_failure(
                    DELIVERY_STATE_ACTIONS,
                    logger,
                    get_delivery_settings(),
                    channel,
                    message_id,
                    grouped_id or message.grouped_id,
                    None,
                    send_result,
                    tr("send.retry_false"),
                )
                if kept and not send_result.count_attempt:
                    logger.warning(
                        tr(
                            "queue.transient_kept",
                            channel=channel,
                            message_id=message_id,
                            reason=reason,
                        )
                    )
                elif kept:
                    logger.warning(
                        tr(
                            "queue.kept",
                            channel=channel,
                            message_id=message_id,
                            attempts=attempts_after,
                            max_attempts=runtime_config.max_queue_attempts,
                            reason=reason,
                        )
                    )
        except Exception as e:
            retry_result = retry_result_for_exception(e)
            kept, reason, attempts_after = await delivery_handle_pending_send_failure(
                DELIVERY_STATE_ACTIONS,
                logger,
                get_delivery_settings(),
                channel,
                message_id,
                grouped_id,
                None,
                retry_result,
                tr("send.retry_false"),
            )
            if kept and not retry_result.count_attempt:
                logger.warning(
                    tr(
                        "queue.transient_kept",
                        channel=channel,
                        message_id=message_id,
                        reason=reason,
                    )
                )
            elif kept:
                logger.warning(
                    tr(
                        "queue.send_failed",
                        channel=channel,
                        message_id=message_id,
                        reason=reason,
                        attempts=attempts_after,
                        max_attempts=runtime_config.max_queue_attempts,
                    )
                )

    remaining_count = get_pending_count()
    if delivered_count or remaining_count:
        logger.info(
            tr("queue.summary", delivered=delivered_count, remaining=remaining_count)
        )

    return delivered_count


async def process_collected_posts(collected_posts, last_seen_ids):
    stats_by_channel = {
        channel: {"sent": 0, "queued": 0, "skipped": 0}
        for channel in last_seen_ids
    }
    last_processed_ids = dict(last_seen_ids)
    already_sent_grouped_ids = set()

    for collected_post in collected_posts:
        channel = collected_post.channel
        message = collected_post.message
        stats = stats_by_channel.setdefault(
            channel,
            {"sent": 0, "queued": 0, "skipped": 0},
        )
        last_processed_id = last_processed_ids.get(channel) or 0

        if is_processed_message(channel, message.id):
            stats["skipped"] += 1
            last_processed_id = max(last_processed_id, message.id)
            last_processed_ids[channel] = last_processed_id
            advance_last_seen_id(channel, last_processed_id)
            logger.info(tr("channel.already_processed", channel=channel, message_id=message.id))
            continue

        if message.grouped_id:
            grouped_key = (channel, message.grouped_id)
            if grouped_key in already_sent_grouped_ids:
                continue

            already_sent_grouped_ids.add(grouped_key)
            album_ids = await telegram_reader.get_album_message_ids(
                telegram_client,
                channel,
                message,
            )
            last_processed_id = max(
                last_processed_id,
                delivery_prepare_pending_delivery(
                    DELIVERY_STATE_ACTIONS,
                    channel,
                    message.id,
                    message.grouped_id,
                    album_ids,
                    tr("send.initial_album_failed"),
                    telegram_reader.get_message_timestamp(message, get_now_ts()),
                ),
            )
            last_processed_ids[channel] = last_processed_id
            send_result = as_send_result(
                await send_to_discord(
                    message,
                    channel,
                    progress_callback=delivery_make_progress_callback(
                        DELIVERY_STATE_ACTIONS,
                        logger,
                        channel,
                        message.id,
                    ),
                ),
                tr("send.initial_album_failed"),
            )

            if send_result:
                for album_message_id in album_ids:
                    mark_processed_message(
                        channel,
                        album_message_id,
                        message.grouped_id,
                        "sent",
                    )
                delete_pending_message(channel, message.id)
                stats["sent"] += 1
                log_success(
                    logger,
                    tr(
                        "channel.album_processed",
                        channel=channel,
                        message_id=message.id,
                        boundary=last_processed_id,
                    )
                )
            else:
                kept, reason, _attempts_after = await delivery_handle_pending_send_failure(
                    DELIVERY_STATE_ACTIONS,
                    logger,
                    get_delivery_settings(),
                    channel,
                    message.id,
                    message.grouped_id,
                    album_ids,
                    send_result,
                    tr("send.initial_album_failed"),
                    source="initial delivery",
                )
                if kept:
                    stats["queued"] += 1
                    logger.warning(
                        tr(
                            "channel.album_queued",
                            channel=channel,
                            message_id=message.id,
                        )
                    )
                else:
                    logger.error(
                        tr(
                            "channel.album_terminal_not_queued",
                            channel=channel,
                            message_id=message.id,
                            reason=reason,
                        )
                    )
            continue

        last_processed_id = max(
            last_processed_id,
            delivery_prepare_pending_delivery(
                DELIVERY_STATE_ACTIONS,
                channel,
                message.id,
                None,
                [message.id],
                tr("send.initial_message_failed"),
                telegram_reader.get_message_timestamp(message, get_now_ts()),
            ),
        )
        last_processed_ids[channel] = last_processed_id
        send_result = as_send_result(
            await send_to_discord(
                message,
                channel,
                progress_callback=delivery_make_progress_callback(
                    DELIVERY_STATE_ACTIONS,
                    logger,
                    channel,
                    message.id,
                ),
            ),
            tr("send.initial_message_failed"),
        )
        if send_result:
            mark_processed_message(channel, message.id, None, "sent")
            delete_pending_message(channel, message.id)
            stats["sent"] += 1
            log_success(
                logger,
                tr(
                    "channel.message_processed",
                    channel=channel,
                    message_id=message.id,
                    boundary=last_processed_id,
                )
            )
        else:
            kept, reason, _attempts_after = await delivery_handle_pending_send_failure(
                DELIVERY_STATE_ACTIONS,
                logger,
                get_delivery_settings(),
                channel,
                message.id,
                None,
                [message.id],
                send_result,
                tr("send.initial_message_failed"),
                source="initial delivery",
            )
            if kept:
                stats["queued"] += 1
                logger.warning(
                    tr(
                        "channel.message_queued",
                        channel=channel,
                        message_id=message.id,
                    )
                )
            else:
                logger.error(
                    tr(
                        "channel.message_terminal_not_queued",
                        channel=channel,
                        message_id=message.id,
                        reason=reason,
                    )
                )

    return stats_by_channel, last_processed_ids


# ===== TELEGRAM NEWS CHECK =====

async def check_telegram_news():
    """Check channels for new messages."""
    try:
        if not telegram_client.is_connected():
            await telegram_client.connect()

        config_snapshot = runtime_config
        last_seen_ids = {
            channel: get_last_seen_id(channel)
            for channel in config_snapshot.telegram_channels
        }
        logger.info(
            tr(
                "channel.concurrent_collection",
                count=len(config_snapshot.telegram_channels),
            )
        )
        collection_results = await telegram_reader.collect_channels(
            telegram_client,
            config_snapshot.telegram_channels,
            last_seen_ids,
        )
        ready_collections = []
        active_boundaries = {}

        for channel, result in collection_results:
            if isinstance(result, ValueError):
                logger.error(tr("channel.not_found", channel=channel))
                continue
            if isinstance(result, Exception):
                logger.error(tr("channel.fetch_error", channel=channel, error=result))
                continue

            last_seen_id = result.last_seen_id
            latest_message_id = result.latest_message_id

            if last_seen_id is None:
                set_last_seen_id(channel, latest_message_id)
                logger.info(
                    tr(
                        "channel.initial_boundary",
                        channel=channel,
                        message_id=latest_message_id,
                    )
                )
                continue

            logger.info(
                tr(
                    "channel.checking",
                    channel=channel,
                    last_seen=last_seen_id,
                    latest=latest_message_id,
                )
            )

            if latest_message_id <= last_seen_id:
                logger.info(tr("channel.no_new_posts", channel=channel))
                continue

            if not result.messages:
                logger.info(tr("channel.no_forwardable_posts", channel=channel))
                continue

            logger.info(
                tr(
                    "channel.candidates_found",
                    channel=channel,
                    count=len(result.messages),
                )
            )
            active_boundaries[channel] = last_seen_id
            ready_collections.append(result)

        chronological_posts = telegram_reader.merge_chronological_posts(
            ready_collections
        )
        total_sent_this_turn = 0
        if chronological_posts:
            logger.info(
                tr(
                    "channel.chronological_batch",
                    count=len(chronological_posts),
                    channels=len(ready_collections),
                )
            )
            stats_by_channel, last_processed_ids = await process_collected_posts(
                chronological_posts,
                active_boundaries,
            )

            for collection in ready_collections:
                channel = collection.channel
                stats = stats_by_channel[channel]
                last_seen_id = active_boundaries[channel]
                last_processed_id = last_processed_ids.get(channel, last_seen_id)
                total_sent_this_turn += stats["sent"]

                if last_processed_id > last_seen_id:
                    advance_last_seen_id(channel, last_processed_id)

                if stats["queued"]:
                    logger.info(
                        tr(
                            "channel.queued_count",
                            channel=channel,
                            count=stats["queued"],
                        )
                    )
                if stats["skipped"]:
                    logger.info(
                        tr(
                            "channel.skipped_processed",
                            channel=channel,
                            count=stats["skipped"],
                        )
                    )

        current_time = datetime.now().strftime("%H:%M:%S")
        if total_sent_this_turn > 0:
            logger.info(
                tr("channel.all_checked_with_sent", time=current_time, count=total_sent_this_turn)
            )
        else:
            logger.info(tr("channel.all_checked_empty", time=current_time))

        return ""

    except Exception as e:
        logger.error(tr("channel.check_error", error=e))
        return str(e)


# ===== DISCORD DELIVERY =====

async def send_to_discord(
    telegram_message,
    channel_name,
    next_chunk_index=0,
    media_sent=False,
    rendered_text=None,
    progress_callback=None,
):
    config_snapshot = runtime_config
    settings = MediaDeliverySettings(
        webhook_url=config_snapshot.discord_webhook_url,
        max_message_length=config_snapshot.max_message_length,
        file_limit_mb=config_snapshot.discord_file_limit_mb,
        large_file_action=config_snapshot.large_file_action,
        transcode_preset=config_snapshot.video_transcode_preset,
        transcode_timeout_seconds=(
            config_snapshot.video_transcode_timeout_seconds
        ),
    )
    return await send_media_to_discord(
        telegram_client,
        telegram_message,
        channel_name,
        settings,
        partial(telegram_reader.get_album_messages, telegram_client),
        partial(
            telegram_reader.get_message_datetime,
            app_timezone=config_snapshot.app_timezone,
        ),
        next_chunk_index=next_chunk_index,
        media_sent=media_sent,
        rendered_text=rendered_text,
        progress_callback=progress_callback,
        transcode_status_callback=record_runtime_transcode_event,
    )


# ===== MAIN LOOP =====

async def prepare_video_transcoding_tools():
    if runtime_config.large_file_action != "compress_then_text":
        return None

    available = find_ffmpeg_tools()
    if available is None:
        logger.info(tr("app.ffmpeg_downloading"))

    try:
        tools = await asyncio.to_thread(ensure_ffmpeg_tools)
    except FFmpegSetupError as error:
        logger.warning(tr("app.ffmpeg_setup_failed", error=error))
        return None

    log_success(logger, tr("app.ffmpeg_ready", source=tools.source))
    return tools


async def main():
    config_snapshot = runtime_config
    heartbeat_task = None
    ffmpeg_setup_task = None
    runtime_tracking_started = False
    terminal_error = ""
    logger.info(tr("app.starting", app_name=APP_NAME, version=__version__))
    logger.info(
        tr(
            "app.telegram_channels",
            channels=", ".join(config_snapshot.telegram_channels),
        )
    )
    logger.info(tr("app.check_interval", seconds=config_snapshot.check_interval))

    logger.info(tr("app.connecting_telegram"))

    try:
        if os.path.exists("tg_session.session"):
            logger.info(tr("app.saved_session_found"))
            try:
                await telegram_client.connect()
                if await telegram_client.is_user_authorized():
                    log_success(logger, tr("app.saved_session_authorized"))
                else:
                    logger.info(tr("app.saved_session_invalid"))
                    await await_telethon_call(telegram_client.start())
            except Exception as e:
                logger.error(tr("app.saved_session_error", error=e))
                raise
        else:
            logger.info(tr("app.no_saved_session"))
            await await_telethon_call(telegram_client.start())

        init_state_db()
        runtime_tracking_started = True
        record_runtime_started()
        heartbeat_task = asyncio.create_task(
            runtime_heartbeat_loop(),
            name="telerixa-runtime-heartbeat",
        )
        ffmpeg_setup_task = asyncio.create_task(
            prepare_video_transcoding_tools(),
            name="telerixa-ffmpeg-setup",
        )
        migrate_legacy_seen_messages()

        await catch_up_channels_on_start()
        log_success(logger, tr("app.startup_sync_done"))

        log_success(logger, tr("app.forwarder_running"))
        print("-" * 50)

        retry_count = 0
        while True:
            record_runtime_cycle_started()
            try:
                reload_config_if_changed()
                await process_pending_messages()
                check_error = await check_telegram_news()
                if check_error:
                    record_runtime_cycle_finished("error", check_error)
                else:
                    record_runtime_cycle_finished("ok")
                retry_count = 0
                reload_config_if_changed()
                await sleep_with_config_reload(runtime_config.check_interval)
            except asyncio.CancelledError:
                record_runtime_cycle_finished("interrupted")
                raise
            except KeyboardInterrupt:
                record_runtime_cycle_finished("interrupted")
                break
            except Exception as e:
                record_runtime_cycle_finished("error", str(e))
                if "database is locked" in str(e).lower():
                    retry_count += 1
                    await sleep_with_config_reload(min(5 * retry_count, 30))
                else:
                    logger.error(tr("app.main_loop_error", error=e))
                    await sleep_with_config_reload(runtime_config.check_interval)
    except Exception as e:
        terminal_error = str(e)
        logger.exception(tr("app.telegram_start_error", error=e))
        raise
    finally:
        if ffmpeg_setup_task is not None:
            if not ffmpeg_setup_task.done():
                ffmpeg_setup_task.cancel()
            try:
                await ffmpeg_setup_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(tr("app.ffmpeg_setup_failed", error=e))
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if runtime_tracking_started:
            record_runtime_stopped(
                status="failed" if terminal_error else "stopped",
                error=terminal_error,
            )
        if telegram_client.is_connected():
            try:
                await await_telethon_call(telegram_client.disconnect())
            except Exception as e:
                logger.warning(tr("app.telegram_disconnect_error", error=e))


def run():
    lifetime_monitor = None
    try:
        require_attached_console()
        pid_file = os.environ.get("TELERIXA_BOT_PID_FILE", BOT_PID_FILE)
        with ProcessLock(pid_file, APP_NAME):
            lifetime_monitor = ProcessLifetimeMonitor()
            with ShutdownSignalHandlers(), lifetime_monitor:
                asyncio.run(main())
    except AlreadyRunningError as e:
        logger.error(tr("app.already_running", pid=e.pid))
        return 1
    except DetachedProcessError as e:
        logger.error(tr("app.detached_start_refused", error=e))
        return 1
    except KeyboardInterrupt:
        if lifetime_monitor is not None and lifetime_monitor.reason:
            logger.warning(
                tr("app.owner_stopped", reason=lifetime_monitor.reason)
            )
        else:
            logger.info(tr("app.stopped_by_user"))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
