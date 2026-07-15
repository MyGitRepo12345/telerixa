import asyncio
import logging
import socket

import aiohttp

from i18n import tr
from .models import SendResult


logger = logging.getLogger(__name__)


NETWORK_ERROR_MARKERS = (
    "cannot connect to host",
    "connection aborted",
    "connection closed",
    "connection reset",
    "getaddrinfo failed",
    "name or service not known",
    "network is unreachable",
    "server closed the connection",
    "temporary failure in name resolution",
    "timed out",
)


def get_alert_mention(alert_user_id):
    return f"<@{alert_user_id}> " if alert_user_id else ""


def describe_network_error(error):
    error_text = str(error)
    lower_error = error_text.lower()
    if "name or service not known" in lower_error or "getaddrinfo failed" in lower_error:
        return tr("network.dns_discord", error=error_text)
    return error_text


def is_network_error(error):
    if isinstance(
        error,
        (
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            ConnectionError,
            socket.gaierror,
        ),
    ):
        return True

    error_text = str(error).lower()
    return any(marker in error_text for marker in NETWORK_ERROR_MARKERS)


def retry_result_for_exception(error):
    description = describe_network_error(error)
    if is_network_error(error):
        return SendResult.transient_retry(description)
    return SendResult.retry(description)


def classify_discord_http_error(status, error):
    if status in (408, 425, 429) or 500 <= status <= 599:
        return SendResult.transient_retry(error)
    if 400 <= status <= 499:
        return SendResult.terminal_failure(error)
    return SendResult.retry(error)


async def post_json(session, webhook_url, payload):
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.post(webhook_url, json=payload, timeout=timeout) as response:
        if response.status not in [200, 204]:
            error = f"Discord webhook error {response.status}"
            logger.error(tr("discord.webhook_error", status=response.status))
            return classify_discord_http_error(response.status, error)
        return SendResult.success()


async def send_text_chunks(
    session,
    webhook_url,
    chunks,
    channel_name,
    message_time,
    start_part=1,
    total_parts=None,
    progress_callback=None,
    media_sent=False,
):
    if total_parts is None:
        total_parts = len(chunks)

    for index, chunk in enumerate(chunks, start=start_part):
        part_suffix = f" ({index}/{total_parts})" if total_parts > 1 else ""
        payload = {
            "embeds": [
                {
                    "description": chunk,
                    "color": 3447003,
                    "timestamp": message_time.isoformat(),
                    "footer": {
                        "text": tr(
                            "telegram.footer_from_channel",
                            channel=channel_name,
                            part_suffix=part_suffix,
                        ),
                    },
                    "author": {
                        "name": tr("telegram.author_news_from_channel", channel=channel_name),
                    },
                }
            ]
        }

        result = await post_json(session, webhook_url, payload)
        if not result:
            return result
        if progress_callback is not None:
            progress_callback(
                next_chunk_index=index,
                media_sent=media_sent,
            )

    return SendResult.success()


async def notify_dropped_message(webhook_url, alert_user_id, channel, message_id, reason, attempts):
    if not webhook_url or not alert_user_id:
        return

    payload = {
        "content": tr(
            "alert.dropped",
            mention=get_alert_mention(alert_user_id),
            channel=channel,
            message_id=message_id,
            attempts=attempts,
            reason=str(reason)[:300],
        ),
        "allowed_mentions": {"users": [alert_user_id]},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, timeout=timeout) as response:
                if response.status not in [200, 204]:
                    logger.warning(tr("alert.dropped_failed_status", status=response.status))
    except Exception as e:
        logger.warning(tr("alert.dropped_failed_error", error=describe_network_error(e)))
