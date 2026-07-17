from dataclasses import dataclass
from typing import Callable

from i18n import tr
from .discord_delivery import notify_dropped_message


@dataclass(frozen=True)
class DeliveryStateActions:
    add_pending_message: Callable
    archive_pending_failure: Callable
    advance_last_seen_id: Callable
    delete_pending_message: Callable
    get_processed_group_message_ids: Callable
    mark_pending_failed: Callable
    mark_processed_message: Callable
    update_pending_delivery_progress: Callable


@dataclass(frozen=True)
class DeliverySettings:
    max_queue_attempts: int
    webhook_url: str
    alert_user_id: str


def log_discarded_message(logger, channel, message_id, reason, attempts, source):
    logger.error("=" * 72)
    logger.error(tr("discard.source", source=source))
    logger.error(tr("discard.post", channel=channel, message_id=message_id))
    logger.error(tr("discard.link", channel=channel, message_id=message_id))
    logger.error(tr("discard.attempts", attempts=attempts))
    logger.error(tr("discard.reason", reason=reason))
    logger.error("=" * 72)


def drop_pending_message(
    actions,
    logger,
    channel,
    message_id,
    grouped_id,
    reason,
    attempts,
    album_ids=None,
    source="retry queue",
    failure_kind="terminal",
):
    if album_ids is None and grouped_id:
        album_ids = actions.get_processed_group_message_ids(channel, grouped_id)
    album_ids = album_ids or [int(message_id)]
    grouped_value = grouped_id if grouped_id else None

    archive_id = actions.archive_pending_failure(
        channel,
        message_id,
        grouped_value,
        album_ids,
        reason,
        failure_kind,
        source,
        attempts,
    )

    log_discarded_message(
        logger,
        channel,
        message_id,
        reason,
        attempts,
        source,
    )
    if archive_id is not None:
        logger.error(tr("discard.archived", archive_id=archive_id))


def prepare_pending_delivery(
    actions,
    channel,
    message_id,
    grouped_id,
    album_ids,
    error,
    telegram_date_ts=None,
):
    delivery_ids = album_ids or [int(message_id)]
    actions.add_pending_message(
        channel,
        message_id,
        grouped_id,
        error,
        telegram_date_ts,
    )
    boundary = max(delivery_ids)
    actions.advance_last_seen_id(channel, boundary)
    for delivery_message_id in delivery_ids:
        actions.mark_processed_message(
            channel,
            delivery_message_id,
            grouped_id,
            "queued",
        )
    return boundary


def make_delivery_progress_callback(actions, logger, channel, message_id):
    def save_delivery_progress(**progress):
        actions.update_pending_delivery_progress(
            channel,
            message_id,
            **progress,
        )
        if "next_chunk_index" in progress or progress.get("media_sent"):
            logger.info(
                tr(
                    "queue.progress_saved",
                    channel=channel,
                    message_id=message_id,
                    next_chunk=progress.get("next_chunk_index", 0),
                    media_sent=int(bool(progress.get("media_sent"))),
                )
            )

    return save_delivery_progress


async def handle_pending_send_failure(
    actions,
    logger,
    settings,
    channel,
    message_id,
    grouped_id,
    album_ids,
    send_result,
    fallback_error,
    source="retry queue",
):
    error_text = send_result.error or fallback_error
    attempts_after = actions.mark_pending_failed(
        channel,
        message_id,
        error_text,
        count_attempt=send_result.count_attempt,
    )
    should_drop = send_result.terminal or (
        send_result.count_attempt
        and attempts_after >= settings.max_queue_attempts
    )
    if not should_drop:
        return True, error_text, attempts_after

    reason = (
        tr("queue.terminal_reason", error=error_text)
        if send_result.terminal
        else tr(
            "queue.max_attempts_reason",
            max_attempts=settings.max_queue_attempts,
            error=error_text,
        )
    )
    failure_kind = "terminal" if send_result.terminal else "max_attempts"
    drop_pending_message(
        actions,
        logger,
        channel,
        message_id,
        grouped_id,
        reason,
        attempts_after,
        album_ids=album_ids,
        source=source,
        failure_kind=failure_kind,
    )
    await notify_dropped_message(
        settings.webhook_url,
        settings.alert_user_id,
        channel,
        message_id,
        reason,
        attempts_after,
    )
    return False, reason, attempts_after
