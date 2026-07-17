from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json
import logging
import os
import re
import socket
import sqlite3
import tempfile
import webbrowser
from logging.handlers import RotatingFileHandler

from i18n import configure_language, get_language_options, normalize_language, tr
from telerixa_core.constants import (
    RUNTIME_SERVICE_NAME,
    RUNTIME_STALE_AFTER_SECONDS,
    UI_PID_FILE,
)
from telerixa_core import state as state_store
from telerixa_core import diagnostics as system_diagnostics
from telerixa_core.logging_setup import (
    SUCCESS_LEVEL,
    build_console_formatter,
)
from telerixa_core.lifecycle import (
    AlreadyRunningError,
    DetachedProcessError,
    ProcessLifetimeMonitor,
    ProcessLock,
    ShutdownSignalHandlers,
    require_attached_console,
)


CONFIG_FILE = Path("config.json")
LOG_DIR = Path("logs")
UI_LOG_FILE = LOG_DIR / "ui.log"
BOT_LOG_FILE = LOG_DIR / "bot.log"
HOST = os.environ.get("TG_FORWARDER_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("TG_FORWARDER_UI_PORT", "8765"))
DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = DB_TIMEOUT_SECONDS * 1000
OVERVIEW_REFRESH_INTERVAL_SECONDS = 10
LAST_DIAGNOSTICS = {
    "ran_at": "",
    "results": [],
}

CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)

LARGE_FILE_ACTION_KEYS = (
    "compress_then_text",
    "send_text_link",
    "try_send_then_text",
    "skip_post",
)

VIDEO_TRANSCODE_PRESET_KEYS = (
    "fast",
    "balanced",
    "quality",
)


class UIConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_large_file_action_options():
    return {
        action_key: tr(f"ui.action.{action_key}")
        for action_key in LARGE_FILE_ACTION_KEYS
    }


def get_video_transcode_preset_options():
    return {
        preset_key: tr(f"ui.transcode_preset.{preset_key}")
        for preset_key in VIDEO_TRANSCODE_PRESET_KEYS
    }


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger("web_ui")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(build_console_formatter(console_handler.stream))

    file_handler = RotatingFileHandler(
        UI_LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


UI_LOGGER = setup_logging()


def log_event(message, level=logging.INFO):
    UI_LOGGER.log(level, message)


def open_default_browser():
    if os.environ.get("TG_FORWARDER_NO_BROWSER") == "1":
        log_event("Browser auto-open is disabled by TG_FORWARDER_NO_BROWSER=1.")
        return

    url = f"http://{HOST}:{PORT}/"
    try:
        opened = webbrowser.open(url, new=2)
    except Exception as e:
        log_event(f"Could not open browser automatically: {e}", logging.WARNING)
        return

    if opened:
        log_event(f"Opened default browser: {url}", SUCCESS_LEVEL)
    else:
        log_event(
            f"Could not open browser automatically. Open manually: {url}",
            logging.WARNING,
        )


def default_config():
    return {
        "DISCORD_WEBHOOK_URL": "",
        "TELEGRAM_API_ID": 0,
        "TELEGRAM_API_HASH": "",
        "TELEGRAM_CHANNELS": [],
        "LANGUAGE": "ru",
        "CHECK_INTERVAL": 60,
        "MAX_MESSAGE_LENGTH": 2000,
        "TIMEZONE": "Europe/Berlin",
        "DISCORD_FILE_LIMIT_MB": 25,
        "LARGE_FILE_ACTION": "send_text_link",
        "VIDEO_TRANSCODE_PRESET": "balanced",
        "VIDEO_TRANSCODE_TIMEOUT_SECONDS": 600,
        "STARTUP_CATCH_UP_LIMIT": 10,
        "MAX_QUEUE_ATTEMPTS": 24,
    }


def load_config():
    config = default_config()
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
            if isinstance(loaded, dict):
                config.update(loaded)
    except FileNotFoundError:
        pass
    config["LANGUAGE"] = normalize_language(config.get("LANGUAGE", "ru"))
    return config


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(CONFIG_FILE.parent),
        delete=False,
    ) as tmp:
        json.dump(config, tmp, ensure_ascii=False, indent=4)
        tmp.write("\n")
        tmp_name = tmp.name

    os.replace(tmp_name, CONFIG_FILE)


def normalize_channel(raw_channel):
    channel = raw_channel.strip()
    if not channel:
        return ""

    lower_channel = channel.lower()
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if lower_channel.startswith(prefix):
            channel = channel[len(prefix):]
            break

    channel = channel.strip().lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [part for part in channel.split("/") if part]

    if len(parts) >= 2 and parts[0].lower() == "s":
        return parts[1]
    if parts:
        return parts[0]

    return channel


def parse_channels(raw_channels):
    channels = []
    invalid_items = []
    duplicate_items = []
    seen = set()

    for raw_line in raw_channels.replace(",", "\n").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        channel = normalize_channel(raw_line)
        if not channel or not CHANNEL_RE.fullmatch(channel):
            invalid_items.append(raw_line)
            continue

        channel_key = channel.lower()
        if channel_key not in seen:
            channels.append(channel)
            seen.add(channel_key)
        else:
            duplicate_items.append(raw_line)

    return channels, invalid_items, duplicate_items


def parse_file_limit(raw_value):
    raw_value = str(raw_value).strip()
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.file_limit_int")

    if limit < 1 or limit > 500:
        return None, tr("ui.file_limit_range")

    return limit, ""


def parse_catch_up_limit(raw_value):
    raw_value = str(raw_value).strip()
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.catch_up_int")

    if limit < 0 or limit > 500:
        return None, tr("ui.catch_up_range")

    return limit, ""


def parse_max_queue_attempts(raw_value):
    raw_value = str(raw_value).strip()
    try:
        attempts = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.max_queue_attempts_int")

    if attempts < 1 or attempts > 200:
        return None, tr("ui.max_queue_attempts_range")

    return attempts, ""


def parse_transcode_timeout(raw_value):
    raw_value = str(raw_value).strip()
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.transcode_timeout_int")

    if timeout_seconds < 30 or timeout_seconds > 7200:
        return None, tr("ui.transcode_timeout_range")

    return timeout_seconds, ""


def validate_webhook(webhook):
    if not webhook:
        return tr("ui.webhook_empty")

    if not webhook.startswith(DISCORD_WEBHOOK_PREFIXES):
        return tr("ui.webhook_prefix")

    return ""


def form_values_from_config(config):
    return {
        "discord_webhook_url": config.get("DISCORD_WEBHOOK_URL", ""),
        "telegram_channels": "\n".join(config.get("TELEGRAM_CHANNELS", [])),
        "language": normalize_language(config.get("LANGUAGE", "ru")),
        "discord_file_limit_mb": str(config.get("DISCORD_FILE_LIMIT_MB", 25)),
        "large_file_action": config.get("LARGE_FILE_ACTION", "send_text_link"),
        "video_transcode_preset": config.get(
            "VIDEO_TRANSCODE_PRESET",
            "balanced",
        ),
        "video_transcode_timeout_seconds": str(
            config.get("VIDEO_TRANSCODE_TIMEOUT_SECONDS", 600)
        ),
        "startup_catch_up_limit": str(config.get("STARTUP_CATCH_UP_LIMIT", 10)),
        "max_queue_attempts": str(config.get("MAX_QUEUE_ATTEMPTS", 24)),
    }


def render_select(name, current_value, options):
    items = []
    for value, label in options.items():
        selected = " selected" if value == current_value else ""
        items.append(
            f'<option value="{escape(value)}"{selected}>{escape(label)}</option>'
        )
    return f'<select id="{escape(name)}" name="{escape(name)}">{"".join(items)}</select>'


def connect_db(db_file):
    conn = sqlite3.connect(
        db_file,
        timeout=DB_TIMEOUT_SECONDS,
        factory=UIConnection,
    )
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def classify_runtime_state(runtime, now_ts=None, stale_after=None):
    if not runtime:
        return "unknown"

    reported_status = str(runtime.get("status") or "").lower()
    if reported_status in {"stopped", "failed"}:
        return reported_status

    heartbeat_ts = runtime.get("heartbeat_ts")
    try:
        heartbeat_ts = float(heartbeat_ts)
    except (TypeError, ValueError):
        return "unknown"

    if now_ts is None:
        now_ts = datetime.now().astimezone().timestamp()
    if stale_after is None:
        stale_after = RUNTIME_STALE_AFTER_SECONDS
    if float(now_ts) - heartbeat_ts > float(stale_after):
        return "stale"

    return "running"


def get_dashboard_snapshot(config, pending_limit=10, failed_limit=10):
    configured_channels = list(config.get("TELEGRAM_CHANNELS", []))
    snapshot = {
        "database_state": "missing",
        "database_error": "",
        "configured_channel_count": len(configured_channels),
        "pending_count": None,
        "due_count": None,
        "sent_count": None,
        "failed_count": None,
        "last_processed_at": "",
        "runtime_state": "unknown",
        "runtime": None,
        "channels": [
            {
                "channel": channel,
                "last_seen_id": None,
                "updated_at": "",
                "sent": 0,
                "queued": 0,
                "failed": 0,
            }
            for channel in configured_channels
        ],
        "pending_items": [],
        "failed_items": [],
    }
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return snapshot

    now_ts = datetime.now().astimezone().timestamp()
    try:
        with connect_db(db_file) as conn:
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            required_tables = {
                "channel_state",
                "pending_messages",
                "processed_messages",
            }
            if not required_tables.issubset(table_names):
                snapshot["database_state"] = "uninitialized"
                return snapshot

            snapshot["database_state"] = "ready"
            if "runtime_status" in table_names:
                runtime_columns = (
                    "service",
                    "status",
                    "pid",
                    "started_at",
                    "started_ts",
                    "heartbeat_at",
                    "heartbeat_ts",
                    "last_cycle_started_at",
                    "last_cycle_started_ts",
                    "last_cycle_finished_at",
                    "last_cycle_finished_ts",
                    "last_cycle_result",
                    "last_error",
                    "activity",
                    "activity_detail",
                    "activity_updated_at",
                    "activity_updated_ts",
                    "last_transcode_result",
                    "last_transcode_detail",
                    "last_transcode_at",
                    "last_transcode_ts",
                )
                available_runtime_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(runtime_status)"
                    ).fetchall()
                }
                runtime_select = ", ".join(
                    column
                    if column in available_runtime_columns
                    else f"NULL AS {column}"
                    for column in runtime_columns
                )
                runtime_row = conn.execute(
                    f"""
                    SELECT {runtime_select}
                    FROM runtime_status
                    WHERE service = ?
                    """,
                    (RUNTIME_SERVICE_NAME,),
                ).fetchone()
                if runtime_row:
                    snapshot["runtime"] = dict(zip(runtime_columns, runtime_row))
                    snapshot["runtime_state"] = classify_runtime_state(
                        snapshot["runtime"],
                        now_ts=now_ts,
                    )

            status_counts = {
                str(status): int(count)
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM processed_messages GROUP BY status"
                ).fetchall()
            }
            snapshot["sent_count"] = status_counts.get("sent", 0)
            has_failed_archive = "failed_deliveries" in table_names
            if has_failed_archive:
                failed_count_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM failed_deliveries
                    WHERE status = 'open'
                    """
                ).fetchone()
                snapshot["failed_count"] = (
                    int(failed_count_row[0]) if failed_count_row else 0
                )
            else:
                snapshot["failed_count"] = status_counts.get("failed", 0)

            pending_row = conn.execute(
                "SELECT COUNT(*) FROM pending_messages"
            ).fetchone()
            due_row = conn.execute(
                "SELECT COUNT(*) FROM pending_messages WHERE next_retry_ts <= ?",
                (now_ts,),
            ).fetchone()
            snapshot["pending_count"] = int(pending_row[0]) if pending_row else 0
            snapshot["due_count"] = int(due_row[0]) if due_row else 0

            latest_row = conn.execute(
                """
                SELECT processed_at
                FROM processed_messages
                ORDER BY processed_ts DESC
                LIMIT 1
                """
            ).fetchone()
            if latest_row:
                snapshot["last_processed_at"] = str(latest_row[0] or "")

            channel_states = {
                str(channel): (last_seen_id, updated_at)
                for channel, last_seen_id, updated_at in conn.execute(
                    "SELECT channel, last_seen_id, updated_at FROM channel_state"
                ).fetchall()
            }
            channel_counts = {}
            for channel, status, count in conn.execute(
                """
                SELECT channel, status, COUNT(*)
                FROM processed_messages
                GROUP BY channel, status
                """
            ).fetchall():
                channel_counts.setdefault(str(channel), {})[str(status)] = int(count)

            channel_rows = []
            for channel in configured_channels:
                last_seen_id, updated_at = channel_states.get(channel, (None, ""))
                counts = channel_counts.get(channel, {})
                channel_rows.append(
                    {
                        "channel": channel,
                        "last_seen_id": last_seen_id,
                        "updated_at": str(updated_at or ""),
                        "sent": counts.get("sent", 0),
                        "queued": counts.get("queued", 0),
                        "failed": counts.get("failed", 0),
                    }
                )
            snapshot["channels"] = channel_rows

            snapshot["pending_items"] = conn.execute(
                """
                SELECT channel,
                       message_id,
                       grouped_id,
                       attempts,
                       next_retry_at,
                       next_retry_ts,
                       last_error
                FROM pending_messages
                ORDER BY next_retry_ts ASC, created_ts ASC
                LIMIT ?
                """,
                (int(pending_limit),),
            ).fetchall()
            if has_failed_archive:
                failed_columns = (
                    "id",
                    "channel",
                    "message_id",
                    "grouped_id",
                    "reason",
                    "last_error",
                    "failure_kind",
                    "source",
                    "attempts",
                    "next_chunk_index",
                    "media_sent",
                    "failed_at",
                    "status",
                    "resolved_at",
                )
                failed_rows = conn.execute(
                    """
                    SELECT id,
                           channel,
                           message_id,
                           grouped_id,
                           reason,
                           last_error,
                           failure_kind,
                           source,
                           attempts,
                           next_chunk_index,
                           media_sent,
                           failed_at,
                           status,
                           resolved_at
                    FROM failed_deliveries
                    ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                             failed_ts DESC
                    LIMIT ?
                    """,
                    (int(failed_limit),),
                ).fetchall()
                snapshot["failed_items"] = [
                    dict(zip(failed_columns, row)) for row in failed_rows
                ]
            else:
                legacy_rows = conn.execute(
                    """
                    SELECT channel,
                           MIN(message_id) AS message_id,
                           grouped_id,
                           MAX(processed_at) AS processed_at
                    FROM processed_messages
                    WHERE status = 'failed'
                    GROUP BY channel, COALESCE(grouped_id, -message_id)
                    ORDER BY MAX(processed_ts) DESC
                    LIMIT ?
                    """,
                    (int(failed_limit),),
                ).fetchall()
                snapshot["failed_items"] = [
                    {
                        "id": None,
                        "channel": channel,
                        "message_id": message_id,
                        "grouped_id": grouped_id,
                        "reason": "",
                        "last_error": "",
                        "failure_kind": "legacy",
                        "source": "legacy state",
                        "attempts": 0,
                        "next_chunk_index": 0,
                        "media_sent": 0,
                        "failed_at": processed_at,
                        "status": "open",
                        "resolved_at": None,
                    }
                    for channel, message_id, grouped_id, processed_at in legacy_rows
                ]
    except sqlite3.Error as e:
        snapshot["database_state"] = "error"
        snapshot["database_error"] = str(e)

    return snapshot


def retry_pending_now(config, channel, message_id):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return False

    now = datetime.now().astimezone()
    with connect_db(db_file) as conn:
        cursor = conn.execute(
            """
            UPDATE pending_messages
            SET updated_at = ?,
                updated_ts = ?,
                next_retry_at = ?,
                next_retry_ts = ?
            WHERE channel = ? AND message_id = ?
            """,
            (
                now.isoformat(),
                now.timestamp(),
                now.isoformat(),
                now.timestamp(),
                channel,
                int(message_id),
            ),
        )
        conn.commit()
        return cursor.rowcount > 0


def requeue_archived_failure(config, archive_id):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return "missing"

    now = datetime.now().astimezone()
    return state_store.requeue_failed_delivery(
        db_file,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        archive_id,
        now.timestamp(),
        now.isoformat(),
    )


def dismiss_archived_failure(config, archive_id):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return False

    now = datetime.now().astimezone()
    return state_store.dismiss_failed_delivery(
        db_file,
        DB_TIMEOUT_SECONDS,
        DB_BUSY_TIMEOUT_MS,
        archive_id,
        now.timestamp(),
        now.isoformat(),
    )


def run_system_diagnostics(config):
    now = datetime.now().astimezone()
    results = system_diagnostics.run_diagnostics(config, Path("."))
    LAST_DIAGNOSTICS["ran_at"] = now.isoformat()
    LAST_DIAGNOSTICS["results"] = results
    return results


def clear_pending_queue(config):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return 0

    now = datetime.now().astimezone()
    with connect_db(db_file) as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_messages").fetchone()
        deleted_count = row[0] if row else 0
        conn.execute(
            """
            UPDATE processed_messages
            SET status = 'dropped',
                processed_at = ?,
                processed_ts = ?
            WHERE status = 'queued'
            """,
            (now.isoformat(), now.timestamp()),
        )
        conn.execute("DELETE FROM pending_messages")
        conn.commit()

    return deleted_count


def read_log_tail(path, max_lines=30, levels=None):
    if not path.exists():
        return []

    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []

    if levels:
        level_markers = tuple(f" {level}:" for level in levels)
        lines = [line for line in lines if any(marker in line for marker in level_markers)]

    return lines[-max_lines:]


def render_log_panel(title, lines):
    if lines:
        rendered_lines = []
        for line in lines:
            match = re.match(
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
                r"(DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL):",
                line,
            )
            level = match.group(1).lower() if match else "unknown"
            rendered_lines.append(
                f'<div class="log-line log-{level}">{escape(line)}</div>'
            )
        body = (
            '<div class="log-lines" role="log">'
            f'{"".join(rendered_lines)}'
            "</div>"
        )
    else:
        body = f'<div class="log-empty">{escape(tr("ui.no_log_entries"))}</div>'

    return f"""
        <div class="log-panel">
          <h2>{escape(title)}</h2>
          {body}
        </div>
    """


def format_ui_timestamp(value):
    if not value:
        return "-"

    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        else:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def render_navigation(active_view):
    items = (
        ("overview", "/", tr("ui.nav_overview")),
        ("settings", "/settings", tr("ui.nav_settings")),
        ("logs", "/logs", tr("ui.nav_logs")),
    )
    links = []
    for view_name, href, label in items:
        active_class = " active" if view_name == active_view else ""
        current = ' aria-current="page"' if view_name == active_view else ""
        links.append(
            f'<a class="tab{active_class}" href="{href}"{current}>{escape(label)}</a>'
        )
    return (
        f'<nav class="tabs" aria-label="{escape(tr("ui.nav_label"))}">'
        f'{"".join(links)}</nav>'
    )


def render_metric(label, value, tone=""):
    value_text = "-" if value is None else str(value)
    tone_class = f" {tone}" if tone else ""
    return (
        f'<div class="metric{tone_class}">'
        f'<span>{escape(label)}</span>'
        f'<strong>{escape(value_text)}</strong>'
        "</div>"
    )


def render_queue_panel(pending_count, pending_items):
    count_text = "-" if pending_count is None else str(pending_count)
    disabled = " disabled" if not pending_count else ""
    now_ts = datetime.now().astimezone().timestamp()
    confirm_clear = escape(
        json.dumps(tr("ui.confirm_clear_queue"), ensure_ascii=False),
        quote=True,
    )

    if pending_items:
        item_rows = []
        for channel, message_id, grouped_id, attempts, next_retry_at, next_retry_ts, last_error in pending_items:
            retry_delay = ""
            retry_requested = False
            if next_retry_ts is not None:
                retry_timestamp = float(next_retry_ts)
                retry_requested = retry_timestamp <= now_ts
                seconds = max(0, int(retry_timestamp - now_ts))
                retry_delay = tr("ui.retry_in_seconds", seconds=seconds)
            retry_text = next_retry_at or retry_delay or "-"
            grouped_text = tr("ui.album_suffix", grouped_id=grouped_id) if grouped_id else ""
            error_text = str(last_error or "").strip()
            if len(error_text) > 220:
                error_text = error_text[:217] + "..."
            telegram_url = f"https://t.me/{channel}/{message_id}"
            retry_button_text = (
                tr("ui.retry_requested") if retry_requested else tr("ui.retry_now")
            )
            retry_disabled = " disabled" if retry_requested else ""
            retry_state_html = (
                f'<span class="queue-retry-state">{escape(tr("ui.retry_waiting"))}</span>'
                if retry_requested
                else ""
            )
            item_rows.append(
                "<li>"
                '<div class="queue-copy">'
                f"<strong>@{escape(str(channel))}/{escape(str(message_id))}</strong>"
                f"<span>{escape(tr('ui.queue_attempts', attempts=attempts, grouped_text=grouped_text))}</span>"
                f"<span>{escape(tr('ui.queue_next_attempt', retry_text=retry_text))}</span>"
                f"<span>{escape(tr('ui.queue_error', error=error_text or '-'))}</span>"
                f"{retry_state_html}"
                "</div>"
                '<div class="item-actions">'
                f'<a href="{escape(telegram_url)}" target="_blank" rel="noopener noreferrer">'
                f'{escape(tr("ui.open_telegram"))}</a>'
                '<form method="post" action="/retry-now" '
                'onsubmit="const button=this.querySelector(\'button\');button.disabled=true;button.textContent=button.dataset.pendingLabel;">'
                f'<input type="hidden" name="channel" value="{escape(str(channel), quote=True)}">'
                f'<input type="hidden" name="message_id" value="{escape(str(message_id), quote=True)}">'
                f'<button class="secondary-button compact-button" type="submit" '
                f'data-pending-label="{escape(tr("ui.retry_requested"), quote=True)}"{retry_disabled}>'
                f'{escape(retry_button_text)}</button>'
                "</form>"
                "</div>"
                "</li>"
            )
        pending_html = f'<ul class="queue-items">{"".join(item_rows)}</ul>'
    else:
        pending_html = f'<p class="queue-empty">{escape(tr("ui.queue_empty"))}</p>'

    return f"""
    <section class="queue-panel">
      <div class="queue-head">
        <h2>{escape(tr("ui.queue_title"))}</h2>
        <p>{escape(tr("ui.queue_count", count=count_text))}</p>
      </div>
      <form method="post" action="/clear-queue">
        <button class="danger-button" type="submit"{disabled} onclick="return confirm({confirm_clear})">{escape(tr("ui.clear_queue"))}</button>
      </form>
      {pending_html}
    </section>
    """


def render_channel_state(snapshot):
    channels = snapshot["channels"]
    if not channels:
        table_body = (
            '<tr><td colspan="6" class="empty-cell">'
            f'{escape(tr("ui.no_channels_configured"))}</td></tr>'
        )
    else:
        rows = []
        for item in channels:
            channel = str(item["channel"])
            telegram_url = f"https://t.me/{channel}"
            rows.append(
                "<tr>"
                f'<td><a href="{escape(telegram_url)}" target="_blank" rel="noopener noreferrer">@{escape(channel)}</a></td>'
                f'<td class="numeric">{escape(str(item["last_seen_id"] if item["last_seen_id"] is not None else "-"))}</td>'
                f'<td>{escape(format_ui_timestamp(item["updated_at"]))}</td>'
                f'<td class="numeric success-text">{escape(str(item["sent"]))}</td>'
                f'<td class="numeric">{escape(str(item["queued"]))}</td>'
                f'<td class="numeric danger-text">{escape(str(item["failed"]))}</td>'
                "</tr>"
            )
        table_body = "".join(rows)

    return f"""
    <section class="data-section">
      <div class="section-head">
        <h2>{escape(tr("ui.channel_state_title"))}</h2>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{escape(tr("ui.channel"))}</th>
              <th class="numeric">{escape(tr("ui.last_seen"))}</th>
              <th>{escape(tr("ui.updated_at"))}</th>
              <th class="numeric">{escape(tr("ui.sent"))}</th>
              <th class="numeric">{escape(tr("ui.queued"))}</th>
              <th class="numeric">{escape(tr("ui.failed"))}</th>
            </tr>
          </thead>
          <tbody>{table_body}</tbody>
        </table>
      </div>
    </section>
    """


def render_failed_messages(failed_items):
    if not failed_items:
        body = f'<p class="empty-state">{escape(tr("ui.no_failed_messages"))}</p>'
    else:
        failure_kind_labels = {
            "terminal": tr("ui.failure_kind_terminal"),
            "max_attempts": tr("ui.failure_kind_max_attempts"),
            "unavailable": tr("ui.failure_kind_unavailable"),
            "legacy": tr("ui.failure_kind_legacy"),
        }
        status_labels = {
            "open": tr("ui.failure_status_open"),
            "requeued": tr("ui.failure_status_requeued"),
            "dismissed": tr("ui.failure_status_dismissed"),
        }
        confirm_dismiss = escape(
            json.dumps(tr("ui.confirm_dismiss_failure"), ensure_ascii=False),
            quote=True,
        )
        rows = []
        for item in failed_items:
            archive_id = item.get("id")
            channel = str(item["channel"])
            message_id = int(item["message_id"])
            grouped_id = item.get("grouped_id")
            attempts = max(0, int(item.get("attempts") or 0))
            failure_kind = str(item.get("failure_kind") or "legacy")
            status = str(item.get("status") or "open")
            failed_at = item.get("failed_at")
            telegram_url = f"https://t.me/{channel}/{message_id}"
            grouped_text = tr("ui.album_suffix", grouped_id=grouped_id) if grouped_id else ""
            reason = str(item.get("reason") or item.get("last_error") or "").strip()
            if not reason:
                reason = tr("ui.failure_reason_unavailable")
            progress_parts = []
            if item.get("media_sent"):
                progress_parts.append(tr("ui.failure_progress_media_sent"))
            next_chunk_index = max(0, int(item.get("next_chunk_index") or 0))
            if next_chunk_index:
                progress_parts.append(
                    tr("ui.failure_progress_chunk", chunk=next_chunk_index)
                )
            progress_html = ""
            if progress_parts:
                progress_html = (
                    '<span class="archive-progress">'
                    f'{escape(tr("ui.failure_saved_progress", progress=", ".join(progress_parts)))}'
                    "</span>"
                )

            action_parts = [
                f'<a href="{escape(telegram_url)}" target="_blank" rel="noopener noreferrer">'
                f'{escape(tr("ui.open_telegram"))}</a>'
            ]
            if archive_id is not None and status == "open":
                action_parts.extend(
                    (
                        '<form method="post" action="/failed/requeue">'
                        f'<input type="hidden" name="archive_id" value="{escape(str(archive_id), quote=True)}">'
                        f'<button class="secondary-button compact-button" type="submit">{escape(tr("ui.retry_now"))}</button>'
                        "</form>",
                        '<form method="post" action="/failed/dismiss">'
                        f'<input type="hidden" name="archive_id" value="{escape(str(archive_id), quote=True)}">'
                        f'<button class="secondary-button compact-button" type="submit" onclick="return confirm({confirm_dismiss})">{escape(tr("ui.dismiss"))}</button>'
                        "</form>",
                    )
                )

            status_text = status_labels.get(status, status)
            status_class = status if status in status_labels else "unknown"
            kind_text = failure_kind_labels.get(failure_kind, failure_kind)
            rows.append(
                "<tr>"
                '<td class="archive-post">'
                f'<a href="{escape(telegram_url)}" target="_blank" rel="noopener noreferrer">@{escape(channel)}/{escape(str(message_id))}</a>'
                f'<span>{escape(grouped_text.lstrip(", ") or "-")}</span>'
                "</td>"
                '<td class="archive-kind">'
                f'<strong>{escape(kind_text)}</strong>'
                f'<span class="archive-status status-{status_class}">{escape(status_text)}</span>'
                "</td>"
                f'<td class="numeric">{escape(str(attempts))}</td>'
                f'<td>{escape(format_ui_timestamp(failed_at))}</td>'
                f'<td class="archive-reason"><span>{escape(reason)}</span>{progress_html}</td>'
                f'<td><div class="item-actions archive-actions">{"".join(action_parts)}</div></td>'
                "</tr>"
            )
        body = f"""
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{escape(tr("ui.failed_post"))}</th>
                <th>{escape(tr("ui.failure_type"))}</th>
                <th class="numeric">{escape(tr("ui.attempts"))}</th>
                <th>{escape(tr("ui.failed_at"))}</th>
                <th>{escape(tr("ui.failure_reason"))}</th>
                <th>{escape(tr("ui.actions"))}</th>
              </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>
        """

    return f"""
    <section class="data-section">
      <div class="section-head">
        <h2>{escape(tr("ui.failed_deliveries"))}</h2>
      </div>
      {body}
    </section>
    """


def render_runtime_details(runtime):
    if runtime:
        started_at = format_ui_timestamp(runtime.get("started_at"))
        heartbeat_at = format_ui_timestamp(runtime.get("heartbeat_at"))
        pid = runtime.get("pid") or "-"
        cycle_result = str(runtime.get("last_cycle_result") or "none")
        cycle_labels = {
            "running": tr("ui.runtime_cycle_running"),
            "ok": tr("ui.runtime_cycle_ok"),
            "error": tr("ui.runtime_cycle_error"),
            "interrupted": tr("ui.runtime_cycle_interrupted"),
            "none": tr("ui.runtime_cycle_none"),
        }
        cycle_at = (
            runtime.get("last_cycle_started_at")
            if cycle_result == "running"
            else runtime.get("last_cycle_finished_at")
        )
        cycle_text = cycle_labels.get(cycle_result, cycle_result)
        if cycle_at:
            cycle_text = f"{cycle_text} - {format_ui_timestamp(cycle_at)}"
        last_error = str(runtime.get("last_error") or "").strip()
        activity = str(runtime.get("activity") or "").strip()
        activity_detail = str(runtime.get("activity_detail") or "").strip()
        if activity == "transcoding" and activity_detail:
            activity_text = activity_detail
        else:
            activity_text = tr("ui.runtime_activity_idle")

        last_transcode_result = str(
            runtime.get("last_transcode_result") or ""
        ).strip()
        last_transcode_detail = str(
            runtime.get("last_transcode_detail") or ""
        ).strip()
        if last_transcode_result and last_transcode_detail:
            result_label = tr(
                f"ui.runtime_transcode_{last_transcode_result}"
            )
            last_transcode_text = f"{result_label}: {last_transcode_detail}"
            last_transcode_at = runtime.get("last_transcode_at")
            if last_transcode_at:
                last_transcode_text += (
                    f" - {format_ui_timestamp(last_transcode_at)}"
                )
        else:
            last_transcode_text = tr("ui.runtime_transcode_none")
    else:
        started_at = "-"
        heartbeat_at = "-"
        pid = "-"
        cycle_text = tr("ui.runtime_cycle_none")
        last_error = ""
        activity_text = tr("ui.runtime_activity_idle")
        last_transcode_text = tr("ui.runtime_transcode_none")

    error_html = ""
    if last_error:
        if len(last_error) > 500:
            last_error = last_error[:497] + "..."
        error_html = (
            '<div class="runtime-error">'
            f'{escape(tr("ui.runtime_last_error", error=last_error))}'
            "</div>"
        )

    return f"""
    <section class="runtime-details">
      <div class="runtime-item">
        <span>{escape(tr("ui.runtime_started"))}</span>
        <strong>{escape(started_at)}</strong>
      </div>
      <div class="runtime-item">
        <span>{escape(tr("ui.runtime_heartbeat"))}</span>
        <strong>{escape(heartbeat_at)}</strong>
      </div>
      <div class="runtime-item">
        <span>{escape(tr("ui.runtime_pid"))}</span>
        <strong>{escape(str(pid))}</strong>
      </div>
      <div class="runtime-item">
        <span>{escape(tr("ui.runtime_cycle"))}</span>
        <strong>{escape(cycle_text)}</strong>
      </div>
      <div class="runtime-item">
        <span>{escape(tr("ui.runtime_activity"))}</span>
        <strong>{escape(activity_text)}</strong>
      </div>
      <div class="runtime-item">
        <span>{escape(tr("ui.runtime_last_transcode"))}</span>
        <strong>{escape(last_transcode_text)}</strong>
      </div>
      {error_html}
    </section>
    """


def render_diagnostics(diagnostics_snapshot):
    results = list((diagnostics_snapshot or {}).get("results") or [])
    ran_at = str((diagnostics_snapshot or {}).get("ran_at") or "")
    component_labels = {
        "sqlite": tr("ui.diagnostic_component_sqlite"),
        "discord": tr("ui.diagnostic_component_discord"),
        "telegram": tr("ui.diagnostic_component_telegram"),
        "disk": tr("ui.diagnostic_component_disk"),
        "ffmpeg": tr("ui.diagnostic_component_ffmpeg"),
    }
    status_labels = {
        "success": tr("ui.diagnostic_status_success"),
        "warning": tr("ui.diagnostic_status_warning"),
        "error": tr("ui.diagnostic_status_error"),
    }

    if results:
        rows = []
        for result in results:
            component = str(result.get("component") or "unknown")
            status = str(result.get("status") or "error")
            status_class = status if status in status_labels else "error"
            code = str(result.get("code") or "internal_error")
            details = dict(result.get("details") or {})
            message = tr(f"ui.diagnostic_{code}", **details)
            rows.append(
                "<tr>"
                f'<td><strong>{escape(component_labels.get(component, component))}</strong></td>'
                f'<td><span class="diagnostic-status diagnostic-{status_class}">{escape(status_labels.get(status, status))}</span></td>'
                f'<td class="diagnostic-message">{escape(message)}</td>'
                "</tr>"
            )
        body = f"""
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{escape(tr("ui.diagnostic_component"))}</th>
                <th>{escape(tr("ui.diagnostic_status"))}</th>
                <th>{escape(tr("ui.diagnostic_details"))}</th>
              </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>
        """
        ran_at_text = tr(
            "ui.diagnostic_last_run",
            value=format_ui_timestamp(ran_at),
        )
    else:
        body = f'<p class="empty-state">{escape(tr("ui.diagnostic_not_run"))}</p>'
        ran_at_text = ""

    return f"""
    <section class="data-section diagnostics-section">
      <div class="section-head diagnostics-head">
        <div>
          <h2>{escape(tr("ui.diagnostics_title"))}</h2>
          <span>{escape(ran_at_text)}</span>
        </div>
        <form method="post" action="/run-diagnostics">
          <button class="secondary-button" type="submit">{escape(tr("ui.run_diagnostics"))}</button>
        </form>
      </div>
      {body}
    </section>
    """


def render_overview(snapshot, diagnostics_snapshot=None):
    database_state = snapshot["database_state"]
    database_labels = {
        "ready": tr("ui.database_ready"),
        "missing": tr("ui.database_missing"),
        "uninitialized": tr("ui.database_uninitialized"),
        "error": tr("ui.database_error"),
    }
    database_tone = "success" if database_state == "ready" else "warning"
    if database_state == "error":
        database_tone = "danger"

    runtime_state = str(snapshot["runtime_state"] or "unknown")
    runtime_labels = {
        "running": tr("ui.bot_running"),
        "stale": tr("ui.bot_stale"),
        "stopped": tr("ui.bot_stopped"),
        "failed": tr("ui.bot_failed"),
        "unknown": tr("ui.bot_unknown"),
    }
    runtime_tones = {
        "running": "success",
        "stale": "danger",
        "stopped": "warning",
        "failed": "danger",
        "unknown": "warning",
    }

    database_error_html = ""
    if snapshot["database_error"]:
        database_error_html = (
            '<div class="inline-error">'
            f'{escape(tr("ui.database_error_detail", error=snapshot["database_error"]))}'
            "</div>"
        )

    last_activity = format_ui_timestamp(snapshot["last_processed_at"])
    if not snapshot["last_processed_at"]:
        last_activity = tr("ui.no_activity")

    metrics_html = "".join(
        (
            render_metric(tr("ui.metric_channels"), snapshot["configured_channel_count"]),
            render_metric(tr("ui.metric_pending"), snapshot["pending_count"]),
            render_metric(tr("ui.metric_due"), snapshot["due_count"]),
            render_metric(tr("ui.metric_failed"), snapshot["failed_count"], "danger-metric"),
            render_metric(tr("ui.metric_sent"), snapshot["sent_count"], "success-metric"),
        )
    )

    return f"""
    <section class="overview-status">
      <div class="health-items">
        <div class="health-item">
          <span class="eyebrow">{escape(tr("ui.database"))}</span>
          <strong class="health {database_tone}">{escape(database_labels.get(database_state, database_state))}</strong>
        </div>
        <div class="health-item">
          <span class="eyebrow">{escape(tr("ui.bot_process"))}</span>
          <strong class="health {runtime_tones.get(runtime_state, "warning")}">{escape(runtime_labels.get(runtime_state, runtime_state))}</strong>
        </div>
      </div>
      <p>{escape(tr("ui.last_activity", value=last_activity))}</p>
    </section>
    {database_error_html}
    {render_runtime_details(snapshot["runtime"])}
    <section class="metrics">{metrics_html}</section>
    {render_diagnostics(diagnostics_snapshot)}
    {render_channel_state(snapshot)}
    {render_queue_panel(snapshot["pending_count"], snapshot["pending_items"])}
    {render_failed_messages(snapshot["failed_items"])}
    """


def format_queue_status(pending_count):
    if pending_count is None:
        return tr("ui.queue_status_unknown")
    return tr("ui.queue_status", count=pending_count)


def build_overview_payload(config):
    selected_language = normalize_language(config.get("LANGUAGE", "ru"))
    configure_language(selected_language)
    snapshot = get_dashboard_snapshot(config)
    return {
        "overview_html": render_overview(snapshot, LAST_DIAGNOSTICS),
        "queue_text": format_queue_status(snapshot["pending_count"]),
    }


def render_overview_refresh_script():
    interval_ms = OVERVIEW_REFRESH_INTERVAL_SECONDS * 1000
    return f"""
  <script>
    (() => {{
      const overview = document.getElementById("overview-content");
      const queueStatus = document.getElementById("queue-status");
      let refreshInProgress = false;

      async function refreshOverview() {{
        if (refreshInProgress || document.hidden || !overview) {{
          return;
        }}

        refreshInProgress = true;
        try {{
          const response = await fetch("/api/overview", {{
            cache: "no-store",
            headers: {{"Accept": "application/json"}},
          }});
          if (!response.ok) {{
            throw new Error(`HTTP ${{response.status}}`);
          }}

          const payload = await response.json();
          if (typeof payload.overview_html !== "string" || typeof payload.queue_text !== "string") {{
            throw new Error("Invalid overview response");
          }}

          overview.innerHTML = payload.overview_html;
          if (queueStatus) {{
            queueStatus.textContent = payload.queue_text;
          }}
        }} catch (error) {{
          console.warn("Telerixa overview refresh failed", error);
        }} finally {{
          refreshInProgress = false;
        }}
      }}

      window.setInterval(refreshOverview, {interval_ms});
      document.addEventListener("visibilitychange", () => {{
        if (!document.hidden) {{
          refreshOverview();
        }}
      }});
    }})();
  </script>
    """


def redact_webhook(webhook):
    if not webhook:
        return "(empty)"

    parts = webhook.split("/")
    if len(parts) >= 7:
        return f"{parts[0]}//{parts[2]}/.../{parts[5]}/***"

    return "***"


def describe_config_changes(old_config, new_config):
    changes = []

    if old_config.get("DISCORD_WEBHOOK_URL", "") != new_config.get("DISCORD_WEBHOOK_URL", ""):
        changes.append(
            "Discord webhook changed: "
            f"{redact_webhook(old_config.get('DISCORD_WEBHOOK_URL', ''))} -> "
            f"{redact_webhook(new_config.get('DISCORD_WEBHOOK_URL', ''))}"
        )

    old_channels = old_config.get("TELEGRAM_CHANNELS", [])
    new_channels = new_config.get("TELEGRAM_CHANNELS", [])
    added_channels = [channel for channel in new_channels if channel not in old_channels]
    removed_channels = [channel for channel in old_channels if channel not in new_channels]

    if added_channels:
        changes.append(f"Telegram channels added: {', '.join(added_channels)}")
    if removed_channels:
        changes.append(f"Telegram channels removed: {', '.join(removed_channels)}")

    old_limit = old_config.get("DISCORD_FILE_LIMIT_MB", 25)
    new_limit = new_config.get("DISCORD_FILE_LIMIT_MB", 25)
    if old_limit != new_limit:
        changes.append(f"Discord file limit changed: {old_limit} MB -> {new_limit} MB")

    old_action = old_config.get("LARGE_FILE_ACTION", "send_text_link")
    new_action = new_config.get("LARGE_FILE_ACTION", "send_text_link")
    if old_action != new_action:
        action_options = get_large_file_action_options()
        changes.append(
            "Large file action changed: "
            f"{action_options.get(old_action, old_action)} -> "
            f"{action_options.get(new_action, new_action)}"
        )

    old_preset = old_config.get("VIDEO_TRANSCODE_PRESET", "balanced")
    new_preset = new_config.get("VIDEO_TRANSCODE_PRESET", "balanced")
    if old_preset != new_preset:
        preset_options = get_video_transcode_preset_options()
        changes.append(
            "Video conversion preset changed: "
            f"{preset_options.get(old_preset, old_preset)} -> "
            f"{preset_options.get(new_preset, new_preset)}"
        )

    old_timeout = old_config.get("VIDEO_TRANSCODE_TIMEOUT_SECONDS", 600)
    new_timeout = new_config.get("VIDEO_TRANSCODE_TIMEOUT_SECONDS", 600)
    if old_timeout != new_timeout:
        changes.append(
            "Video conversion timeout changed: "
            f"{old_timeout}s -> {new_timeout}s"
        )

    old_language = normalize_language(old_config.get("LANGUAGE", "ru"))
    new_language = normalize_language(new_config.get("LANGUAGE", "ru"))
    if old_language != new_language:
        changes.append(tr("ui.change_language", old=old_language, new=new_language))

    old_catch_up = old_config.get("STARTUP_CATCH_UP_LIMIT", 10)
    new_catch_up = new_config.get("STARTUP_CATCH_UP_LIMIT", 10)
    if old_catch_up != new_catch_up:
        changes.append(f"Startup catch-up changed: {old_catch_up} -> {new_catch_up} posts")

    old_max_attempts = old_config.get("MAX_QUEUE_ATTEMPTS", 24)
    new_max_attempts = new_config.get("MAX_QUEUE_ATTEMPTS", 24)
    if old_max_attempts != new_max_attempts:
        changes.append(f"Queue retry limit changed: {old_max_attempts} -> {new_max_attempts} attempts")

    return changes


def render_settings_form(values, action, selected_language):
    action_select = render_select(
        "large_file_action",
        action,
        get_large_file_action_options(),
    )
    language_select = render_select(
        "language",
        selected_language,
        get_language_options(),
    )
    transcode_preset_select = render_select(
        "video_transcode_preset",
        values.get("video_transcode_preset", "balanced"),
        get_video_transcode_preset_options(),
    )
    return f"""
    <form class="settings-form" method="post" action="/settings">
      <div class="grid">
        <div class="field full">
          <label for="discord_webhook_url">Discord webhook</label>
          <input id="discord_webhook_url" name="discord_webhook_url" type="url" autocomplete="off" value="{escape(values.get("discord_webhook_url", ""))}">
          <div class="hint">{escape(tr("ui.discord_webhook_hint"))}</div>
        </div>

        <div class="field full">
          <label for="telegram_channels">{escape(tr("ui.telegram_channels"))}</label>
          <textarea id="telegram_channels" name="telegram_channels" spellcheck="false">{escape(values.get("telegram_channels", ""))}</textarea>
          <div class="hint">{escape(tr("ui.telegram_channels_hint"))}</div>
        </div>

        <div class="field">
          <label for="discord_file_limit_mb">{escape(tr("ui.discord_file_limit"))}</label>
          <input id="discord_file_limit_mb" name="discord_file_limit_mb" type="number" min="1" max="500" step="1" value="{escape(values.get("discord_file_limit_mb", ""))}">
          <div class="hint">{escape(tr("ui.discord_file_limit_hint"))}</div>
        </div>

        <div class="field">
          <label for="large_file_action">{escape(tr("ui.large_videos"))}</label>
          {action_select}
          <div class="hint">{escape(tr("ui.large_videos_hint"))}</div>
        </div>

        <div class="field">
          <label for="video_transcode_preset">{escape(tr("ui.transcode_preset"))}</label>
          {transcode_preset_select}
          <div class="hint">{escape(tr("ui.transcode_preset_hint"))}</div>
        </div>

        <div class="field">
          <label for="video_transcode_timeout_seconds">{escape(tr("ui.transcode_timeout"))}</label>
          <input id="video_transcode_timeout_seconds" name="video_transcode_timeout_seconds" type="number" min="30" max="7200" step="1" value="{escape(values.get("video_transcode_timeout_seconds", ""))}">
          <div class="hint">{escape(tr("ui.transcode_timeout_hint"))}</div>
        </div>

        <div class="field">
          <label for="startup_catch_up_limit">{escape(tr("ui.startup_catch_up"))}</label>
          <input id="startup_catch_up_limit" name="startup_catch_up_limit" type="number" min="0" max="500" step="1" value="{escape(values.get("startup_catch_up_limit", ""))}">
          <div class="hint">{escape(tr("ui.startup_catch_up_hint"))}</div>
        </div>

        <div class="field">
          <label for="max_queue_attempts">{escape(tr("ui.max_queue_attempts"))}</label>
          <input id="max_queue_attempts" name="max_queue_attempts" type="number" min="1" max="200" step="1" value="{escape(values.get("max_queue_attempts", ""))}">
          <div class="hint">{escape(tr("ui.max_queue_attempts_hint"))}</div>
        </div>

        <div class="field">
          <label for="language">{escape(tr("ui.language"))}</label>
          {language_select}
          <div class="hint">{escape(tr("ui.language_hint"))}</div>
        </div>
      </div>

      <div class="actions">
        <button type="submit">{escape(tr("ui.save"))}</button>
      </div>
    </form>
    """


def render_logs_view():
    ui_log_html = render_log_panel(
        tr("ui.events_ui"),
        read_log_tail(UI_LOG_FILE, max_lines=25),
    )
    bot_log_html = render_log_panel(
        tr("ui.events_bot"),
        read_log_tail(BOT_LOG_FILE, max_lines=35),
    )
    return f"""
    <section class="logs">
      {ui_log_html}
      {bot_log_html}
    </section>
    """


def render_page(config, notice="", error="", form_values=None, active_view="overview"):
    if active_view not in {"overview", "settings", "logs"}:
        active_view = "overview"

    selected_language = normalize_language(
        (form_values or {}).get("language") or config.get("LANGUAGE", "ru")
    )
    configure_language(selected_language)
    values = form_values or form_values_from_config(config)
    values["language"] = selected_language
    action = values.get("large_file_action", "send_text_link")
    if action not in LARGE_FILE_ACTION_KEYS:
        action = "send_text_link"

    dashboard_snapshot = get_dashboard_snapshot(config)
    pending_count = dashboard_snapshot["pending_count"]
    queue_text = format_queue_status(pending_count)
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    navigation_html = render_navigation(active_view)
    if active_view == "overview":
        content_html = (
            '<div id="overview-content">'
            f"{render_overview(dashboard_snapshot, LAST_DIAGNOSTICS)}"
            "</div>"
        )
        refresh_script = render_overview_refresh_script()
    elif active_view == "settings":
        content_html = render_settings_form(values, action, selected_language)
        refresh_script = ""
    else:
        content_html = render_logs_view()
        refresh_script = ""
    subtitles = {
        "overview": tr("ui.overview_subtitle"),
        "settings": tr("ui.subtitle"),
        "logs": tr("ui.logs_subtitle"),
    }

    return f"""<!doctype html>
<html lang="{escape(selected_language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telerixa</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f5f7f8;
      --panel: #ffffff;
      --text: #1f2a2e;
      --muted: #5b6b72;
      --line: #d9e1e5;
      --accent: #27745f;
      --accent-strong: #1f5f4e;
      --danger: #9f2d2d;
      --warning: #8a5b0f;
      --notice-bg: #e8f5ef;
      --error-bg: #fae9e9;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #15191b;
        --panel: #202628;
        --text: #eef3f4;
        --muted: #a7b4b9;
        --line: #344044;
        --accent: #53b99b;
        --accent-strong: #6dc8ad;
        --danger: #f08282;
        --warning: #efbd67;
        --notice-bg: #193c33;
        --error-bg: #422121;
      }}
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}

    main {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 44px;
    }}

    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 22px;
    }}

    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.15;
      font-weight: 720;
    }}

    .subtitle {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
    }}

    .status {{
      flex: 0 0 auto;
      color: var(--muted);
      font-size: 13px;
      text-align: right;
    }}

    .tabs {{
      display: flex;
      gap: 22px;
      margin-bottom: 22px;
      border-bottom: 1px solid var(--line);
    }}

    .tab {{
      position: relative;
      color: var(--muted);
      font-size: 14px;
      font-weight: 650;
      padding: 11px 1px 12px;
      text-decoration: none;
    }}

    .tab:hover,
    .tab.active {{
      color: var(--text);
    }}

    .tab.active::after {{
      position: absolute;
      right: 0;
      bottom: -1px;
      left: 0;
      height: 2px;
      background: var(--accent);
      content: "";
    }}

    .settings-form {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
    }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}

    .field {{
      display: flex;
      min-width: 0;
      flex-direction: column;
      gap: 7px;
    }}

    .field.full {{
      grid-column: 1 / -1;
    }}

    label {{
      font-size: 13px;
      font-weight: 650;
    }}

    input,
    textarea,
    select {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: transparent;
      color: var(--text);
      font: inherit;
      font-size: 15px;
      padding: 10px 11px;
      outline: none;
    }}

    textarea {{
      min-height: 150px;
      resize: vertical;
      line-height: 1.45;
    }}

    input:focus,
    textarea:focus,
    select:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent);
    }}

    .hint {{
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}

    .actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      margin-top: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
    }}

    button {{
      min-height: 42px;
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      color: #ffffff;
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      padding: 10px 16px;
    }}

    button:hover {{
      background: var(--accent-strong);
    }}

    button:disabled {{
      cursor: default;
      opacity: 0.5;
    }}

    .danger-button {{
      background: var(--danger);
    }}

    .danger-button:hover {{
      background: color-mix(in srgb, var(--danger) 84%, #000000);
    }}

    .secondary-button {{
      border: 1px solid var(--line);
      background: transparent;
      color: var(--text);
    }}

    .secondary-button:hover {{
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 10%, transparent);
    }}

    .compact-button {{
      min-height: 32px;
      padding: 5px 10px;
      font-size: 12px;
    }}

    .notice,
    .error {{
      margin-bottom: 16px;
      border-radius: 7px;
      padding: 11px 13px;
      font-size: 14px;
      line-height: 1.45;
    }}

    .notice {{
      background: var(--notice-bg);
      color: var(--accent-strong);
    }}

    .error {{
      background: var(--error-bg);
      color: var(--danger);
    }}

    .logs {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}

    .overview-status {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      border-bottom: 1px solid var(--line);
      padding: 2px 0 16px;
    }}

    .health-items {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 14px 28px;
    }}

    .health-item {{
      display: grid;
      gap: 4px;
    }}

    .overview-status p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}

    .eyebrow {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }}

    .health {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 13px;
    }}

    .health::before {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      content: "";
    }}

    .health.success,
    .success-text {{
      color: var(--accent-strong);
    }}

    .health.warning {{
      color: var(--warning);
    }}

    .health.danger,
    .danger-text {{
      color: var(--danger);
    }}

    .inline-error {{
      margin-top: 12px;
      border-left: 3px solid var(--danger);
      background: var(--error-bg);
      color: var(--danger);
      padding: 9px 11px;
      font-size: 13px;
    }}

    .runtime-details {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
    }}

    .runtime-item {{
      min-width: 0;
      border-right: 1px solid var(--line);
      padding: 14px 16px;
    }}

    .runtime-item:first-child {{
      padding-left: 0;
    }}

    .runtime-item:nth-child(3n + 1) {{
      padding-left: 0;
    }}

    .runtime-item:nth-child(3n) {{
      border-right: 0;
      padding-right: 0;
    }}

    .runtime-item:nth-child(-n + 3) {{
      border-bottom: 1px solid var(--line);
    }}

    .runtime-item:last-of-type {{
      border-right: 0;
      padding-right: 0;
    }}

    .runtime-item span,
    .runtime-item strong {{
      display: block;
    }}

    .runtime-item span {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
    }}

    .runtime-item strong {{
      margin-top: 5px;
      overflow-wrap: anywhere;
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }}

    .runtime-error {{
      grid-column: 1 / -1;
      border-top: 1px solid var(--line);
      color: var(--danger);
      padding: 10px 0;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}

    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
    }}

    .metric {{
      min-width: 0;
      border-right: 1px solid var(--line);
      padding: 18px 16px;
    }}

    .metric:first-child {{
      padding-left: 0;
    }}

    .metric:last-child {{
      border-right: 0;
    }}

    .metric span,
    .metric strong {{
      display: block;
    }}

    .metric span {{
      color: var(--muted);
      font-size: 12px;
    }}

    .metric strong {{
      margin-top: 5px;
      font-size: 24px;
      line-height: 1;
    }}

    .metric.danger-metric strong {{
      color: var(--danger);
    }}

    .metric.success-metric strong {{
      color: var(--accent-strong);
    }}

    .data-section {{
      margin-top: 24px;
    }}

    .section-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 10px;
    }}

    .section-head h2 {{
      margin: 0;
      font-size: 15px;
      line-height: 1.25;
    }}

    .diagnostics-head > div {{
      display: grid;
      gap: 4px;
    }}

    .diagnostics-head span {{
      color: var(--muted);
      font-size: 11px;
    }}

    .diagnostics-head form {{
      margin: 0;
    }}

    .diagnostic-status {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 12px;
      font-weight: 700;
    }}

    .diagnostic-status::before {{
      width: 8px;
      height: 8px;
      flex: 0 0 auto;
      border-radius: 50%;
      background: currentColor;
      content: "";
    }}

    .diagnostic-success {{
      color: var(--accent-strong);
    }}

    .diagnostic-warning {{
      color: var(--warning);
    }}

    .diagnostic-error {{
      color: var(--danger);
    }}

    .diagnostic-message {{
      min-width: 300px;
      white-space: normal;
      overflow-wrap: anywhere;
    }}

    .table-wrap {{
      width: 100%;
      overflow-x: auto;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}

    th,
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      white-space: nowrap;
    }}

    th:first-child,
    td:first-child {{
      padding-left: 0;
    }}

    th:last-child,
    td:last-child {{
      padding-right: 0;
    }}

    tbody tr:last-child td {{
      border-bottom: 0;
    }}

    th {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }}

    td a,
    .item-actions a {{
      color: var(--accent-strong);
      text-decoration: none;
    }}

    td a:hover,
    .item-actions a:hover {{
      text-decoration: underline;
    }}

    .numeric {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}

    .archive-post span,
    .archive-kind span,
    .archive-reason span {{
      display: block;
    }}

    .archive-post span,
    .archive-progress {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
    }}

    .archive-kind strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 12px;
    }}

    .archive-status {{
      font-size: 11px;
      font-weight: 700;
    }}

    .archive-status.status-open {{
      color: var(--danger);
    }}

    .archive-status.status-requeued {{
      color: var(--accent-strong);
    }}

    .archive-status.status-dismissed {{
      color: var(--muted);
    }}

    .archive-reason {{
      min-width: 260px;
      max-width: 480px;
      white-space: normal;
      overflow-wrap: anywhere;
    }}

    .archive-actions {{
      flex-wrap: wrap;
      min-width: 250px;
    }}

    .empty-cell,
    .empty-state {{
      color: var(--muted);
    }}

    .empty-state {{
      margin: 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      padding: 14px 0;
      font-size: 13px;
    }}

    .queue-panel {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
      gap: 14px 16px;
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 16px;
    }}

    .queue-head h2 {{
      margin: 0 0 4px;
      font-size: 14px;
      line-height: 1.2;
    }}

    .queue-head p,
    .queue-empty {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }}

    .queue-panel form {{
      margin: 0;
      border: 0;
      background: transparent;
      padding: 0;
    }}

    .queue-items {{
      grid-column: 1 / -1;
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}

    .queue-items li {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      border-top: 1px solid var(--line);
      padding-top: 9px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}

    .queue-items strong {{
      color: var(--text);
      font-size: 13px;
    }}

    .queue-items span {{
      min-width: 0;
    }}

    .queue-copy {{
      display: grid;
      min-width: 0;
      gap: 3px;
    }}

    .queue-retry-state {{
      color: var(--warning);
      font-weight: 700;
    }}

    .item-actions {{
      display: flex;
      flex: 0 0 auto;
      align-items: center;
      gap: 10px;
    }}

    .item-actions form {{
      margin: 0;
    }}

    .log-panel {{
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}

    .log-panel h2 {{
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.2;
    }}

    .log-lines,
    .log-empty {{
      width: 100%;
      min-height: 180px;
      max-height: 320px;
      margin: 0;
      overflow: auto;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.45;
    }}

    .log-lines {{
      display: flex;
      flex-direction: column;
      gap: 3px;
    }}

    .log-line {{
      padding: 5px 7px;
      border-left: 3px solid var(--line);
      background: color-mix(in srgb, var(--line) 12%, transparent);
      color: var(--text);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}

    .log-info {{
      border-color: #3486b8;
      background: color-mix(in srgb, #3486b8 10%, transparent);
    }}

    .log-success {{
      border-color: #2f9a68;
      background: color-mix(in srgb, #2f9a68 12%, transparent);
    }}

    .log-warning {{
      border-color: #c58a25;
      background: color-mix(in srgb, #c58a25 13%, transparent);
    }}

    .log-error,
    .log-critical {{
      border-color: #d24d4d;
      background: color-mix(in srgb, #d24d4d 13%, transparent);
    }}

    @media (max-width: 720px) {{
      main {{
        width: min(100vw - 20px, 1120px);
        padding-top: 18px;
      }}

      header {{
        display: block;
      }}

      .status {{
        margin-top: 10px;
        text-align: left;
      }}

      .settings-form {{
        padding: 16px;
      }}

      .grid {{
        grid-template-columns: 1fr;
      }}

      .logs {{
        grid-template-columns: 1fr;
      }}

      .queue-panel {{
        grid-template-columns: 1fr;
      }}

      .metrics {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}

      .runtime-details {{
        grid-template-columns: 1fr;
      }}

      .runtime-item,
      .runtime-item:first-child,
      .runtime-item:last-of-type {{
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 12px 0;
      }}

      .runtime-item:last-of-type {{
        border-bottom: 0;
      }}

      .metric,
      .metric:first-child {{
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 14px 0;
      }}

      .overview-status,
      .queue-items li {{
        display: grid;
      }}

      .item-actions {{
        justify-content: space-between;
      }}

      h1 {{
        font-size: 24px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Telerixa</h1>
        <p class="subtitle">{escape(subtitles[active_view])}</p>
      </div>
      <div class="status">UI: {escape(HOST)}:{PORT}<br><span id="queue-status">{escape(queue_text)}</span></div>
    </header>

    {navigation_html}

    {notice_html}
    {error_html}

    {content_html}
  </main>
{refresh_script}
</body>
</html>"""


class SingleInstanceHTTPServer(HTTPServer):
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ConfigHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request_path = urlsplit(self.path).path
        if request_path == "/api/overview":
            self.respond_json(build_overview_payload(load_config()))
            return

        views = {
            "/": "overview",
            "/index.html": "overview",
            "/settings": "settings",
            "/logs": "logs",
        }
        active_view = views.get(request_path)
        if active_view is None:
            self.send_error(404)
            return

        log_event(
            f"UI {active_view} page opened from {self.client_address[0]}"
        )
        self.respond(render_page(load_config(), active_view=active_view))

    def do_POST(self):
        request_path = urlsplit(self.path).path
        if request_path == "/clear-queue":
            self.handle_clear_queue()
            return
        if request_path == "/retry-now":
            self.handle_retry_now()
            return
        if request_path == "/failed/requeue":
            self.handle_failed_requeue()
            return
        if request_path == "/failed/dismiss":
            self.handle_failed_dismiss()
            return
        if request_path == "/run-diagnostics":
            self.handle_run_diagnostics()
            return

        if request_path not in ("/", "/settings"):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        config = load_config()

        form_values = {
            "discord_webhook_url": form.get("discord_webhook_url", [""])[0].strip(),
            "telegram_channels": form.get("telegram_channels", [""])[0],
            "language": normalize_language(form.get("language", [config.get("LANGUAGE", "ru")])[0]),
            "discord_file_limit_mb": form.get("discord_file_limit_mb", [""])[0].strip(),
            "large_file_action": form.get("large_file_action", ["send_text_link"])[0],
            "video_transcode_preset": form.get(
                "video_transcode_preset",
                ["balanced"],
            )[0],
            "video_transcode_timeout_seconds": form.get(
                "video_transcode_timeout_seconds",
                ["600"],
            )[0].strip(),
            "startup_catch_up_limit": form.get("startup_catch_up_limit", ["10"])[0].strip(),
            "max_queue_attempts": form.get("max_queue_attempts", ["24"])[0].strip(),
        }
        configure_language(form_values["language"])

        errors = []
        webhook = form_values["discord_webhook_url"]
        webhook_error = validate_webhook(webhook)
        if webhook_error:
            errors.append(webhook_error)

        channels, invalid_channels, duplicate_channels = parse_channels(form_values["telegram_channels"])
        if invalid_channels:
            invalid_list = ", ".join(invalid_channels[:5])
            if len(invalid_channels) > 5:
                invalid_list += tr("ui.more_items", count=len(invalid_channels) - 5)
            errors.append(
                tr("ui.invalid_channels", items=invalid_list)
            )
        if duplicate_channels:
            duplicate_list = ", ".join(duplicate_channels[:5])
            if len(duplicate_channels) > 5:
                duplicate_list += tr("ui.more_items", count=len(duplicate_channels) - 5)
            errors.append(
                tr("ui.duplicate_channels", items=duplicate_list)
            )
        if not channels:
            errors.append(tr("ui.no_channels"))

        limit, limit_error = parse_file_limit(form_values["discord_file_limit_mb"])
        if limit_error:
            errors.append(limit_error)

        action = form_values["large_file_action"]
        if action not in LARGE_FILE_ACTION_KEYS:
            errors.append(tr("ui.unknown_large_file_action"))

        transcode_preset = form_values["video_transcode_preset"]
        if transcode_preset not in VIDEO_TRANSCODE_PRESET_KEYS:
            errors.append(tr("ui.unknown_transcode_preset"))

        transcode_timeout, transcode_timeout_error = parse_transcode_timeout(
            form_values["video_transcode_timeout_seconds"]
        )
        if transcode_timeout_error:
            errors.append(transcode_timeout_error)

        catch_up_limit, catch_up_error = parse_catch_up_limit(form_values["startup_catch_up_limit"])
        if catch_up_error:
            errors.append(catch_up_error)

        max_queue_attempts, max_queue_attempts_error = parse_max_queue_attempts(form_values["max_queue_attempts"])
        if max_queue_attempts_error:
            errors.append(max_queue_attempts_error)

        if errors:
            error_text = " ".join(errors)
            log_event(f"Save rejected: {error_text}", logging.WARNING)
            self.respond(
                render_page(
                    config,
                    error=error_text,
                    form_values=form_values,
                    active_view="settings",
                )
            )
            return

        new_config = dict(config)
        new_config["LANGUAGE"] = form_values["language"]
        new_config["DISCORD_WEBHOOK_URL"] = webhook
        new_config["TELEGRAM_CHANNELS"] = channels
        new_config["DISCORD_FILE_LIMIT_MB"] = limit
        new_config["LARGE_FILE_ACTION"] = action
        new_config["VIDEO_TRANSCODE_PRESET"] = transcode_preset
        new_config["VIDEO_TRANSCODE_TIMEOUT_SECONDS"] = transcode_timeout
        new_config["STARTUP_CATCH_UP_LIMIT"] = catch_up_limit
        new_config["MAX_QUEUE_ATTEMPTS"] = max_queue_attempts

        changes = describe_config_changes(config, new_config)
        save_config(new_config)

        if changes:
            log_event("Settings saved:", SUCCESS_LEVEL)
            for change in changes:
                log_event(f"  - {change}", SUCCESS_LEVEL)
        else:
            log_event("Settings saved: no changes.", SUCCESS_LEVEL)

        self.respond(
            render_page(
                new_config,
                notice=tr("ui.saved_notice"),
                active_view="settings",
            )
        )

    def handle_retry_now(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        config = load_config()
        configure_language(config.get("LANGUAGE", "ru"))

        channel = normalize_channel(form.get("channel", [""])[0])
        raw_message_id = form.get("message_id", [""])[0].strip()
        try:
            message_id = int(raw_message_id)
        except (TypeError, ValueError):
            message_id = 0

        if not CHANNEL_RE.fullmatch(channel) or message_id <= 0:
            error_text = tr("ui.retry_invalid")
            log_event(
                f"Retry-now request rejected: {channel}/{raw_message_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        try:
            scheduled = retry_pending_now(config, channel, message_id)
        except sqlite3.Error as e:
            error_text = tr("ui.retry_failed", error=e)
            log_event(error_text, logging.ERROR)
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        if not scheduled:
            error_text = tr("ui.retry_missing")
            log_event(
                f"Retry-now target missing: @{channel}/{message_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        log_event(
            f"Retry scheduled immediately: @{channel}/{message_id}",
            SUCCESS_LEVEL,
        )
        self.respond(
            render_page(
                config,
                notice=tr(
                    "ui.retry_scheduled",
                    channel=channel,
                    message_id=message_id,
                ),
                active_view="overview",
            )
        )

    def read_archive_id(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        try:
            archive_id = int(form.get("archive_id", [""])[0].strip())
        except (TypeError, ValueError):
            archive_id = 0
        return archive_id

    def handle_failed_requeue(self):
        config = load_config()
        configure_language(config.get("LANGUAGE", "ru"))
        archive_id = self.read_archive_id()
        if archive_id <= 0:
            error_text = tr("ui.failure_invalid")
            log_event(
                f"Failed-delivery requeue rejected: {archive_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        try:
            result = requeue_archived_failure(config, archive_id)
        except sqlite3.Error as e:
            error_text = tr("ui.failure_requeue_failed", error=e)
            log_event(error_text, logging.ERROR)
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        if result == "already_pending":
            error_text = tr("ui.failure_already_pending")
            log_event(
                f"Failed delivery already pending: archive_id={archive_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return
        if result != "requeued":
            error_text = tr("ui.failure_missing")
            log_event(
                f"Failed-delivery archive row missing: archive_id={archive_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        log_event(f"Failed delivery requeued: archive_id={archive_id}", SUCCESS_LEVEL)
        self.respond(
            render_page(
                config,
                notice=tr("ui.failure_requeued"),
                active_view="overview",
            )
        )

    def handle_failed_dismiss(self):
        config = load_config()
        configure_language(config.get("LANGUAGE", "ru"))
        archive_id = self.read_archive_id()
        if archive_id <= 0:
            error_text = tr("ui.failure_invalid")
            log_event(
                f"Failed-delivery dismiss rejected: {archive_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        try:
            dismissed = dismiss_archived_failure(config, archive_id)
        except sqlite3.Error as e:
            error_text = tr("ui.failure_dismiss_failed", error=e)
            log_event(error_text, logging.ERROR)
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        if not dismissed:
            error_text = tr("ui.failure_missing")
            log_event(
                f"Failed-delivery archive row missing: archive_id={archive_id}",
                logging.WARNING,
            )
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        log_event(f"Failed delivery dismissed: archive_id={archive_id}", SUCCESS_LEVEL)
        self.respond(
            render_page(
                config,
                notice=tr("ui.failure_dismissed"),
                active_view="overview",
            )
        )

    def handle_run_diagnostics(self):
        config = load_config()
        configure_language(config.get("LANGUAGE", "ru"))
        try:
            results = run_system_diagnostics(config)
        except Exception as e:
            error_text = tr("ui.diagnostics_failed", error=e)
            log_event(error_text, logging.ERROR)
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        summary = ", ".join(
            f"{result.get('component')}={result.get('status')}:{result.get('code')}"
            for result in results
        )
        success_count = sum(result.get("status") == "success" for result in results)
        warning_count = sum(result.get("status") == "warning" for result in results)
        error_count = sum(result.get("status") == "error" for result in results)
        diagnostic_level = (
            logging.ERROR
            if error_count
            else logging.WARNING
            if warning_count
            else SUCCESS_LEVEL
        )
        log_event(f"Diagnostics completed: {summary}", diagnostic_level)
        self.respond(
            render_page(
                config,
                notice=tr(
                    "ui.diagnostics_completed",
                    success=success_count,
                    warnings=warning_count,
                    errors=error_count,
                ),
                active_view="overview",
            )
        )

    def handle_clear_queue(self):
        config = load_config()

        try:
            deleted_count = clear_pending_queue(config)
        except sqlite3.Error as e:
            configure_language(config.get("LANGUAGE", "ru"))
            error_text = tr("ui.clear_queue_failed", error=e)
            log_event(error_text, logging.ERROR)
            self.respond(
                render_page(config, error=error_text, active_view="overview")
            )
            return

        log_event(
            f"Pending queue cleared: {deleted_count} messages.",
            SUCCESS_LEVEL,
        )
        configure_language(config.get("LANGUAGE", "ru"))
        self.respond(
            render_page(
                config,
                notice=tr("ui.queue_cleared", count=deleted_count),
                active_view="overview",
            )
        )

    def log_message(self, format, *args):
        return

    def respond(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond_json(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    try:
        require_attached_console()
    except DetachedProcessError as e:
        log_event(f"Detached settings UI startup was refused: {e}", logging.ERROR)
        return 1

    pid_file = os.environ.get("TELERIXA_UI_PID_FILE", UI_PID_FILE)
    try:
        with ProcessLock(pid_file, "Telerixa settings UI"):
            try:
                server = SingleInstanceHTTPServer((HOST, PORT), ConfigHandler)
            except OSError as e:
                log_event(
                    f"Cannot start settings UI at http://{HOST}:{PORT}: {e}",
                    logging.ERROR,
                )
                log_event(
                    "Port is already busy. Close the existing UI window/process and start again.",
                    logging.WARNING,
                )
                return 1

            lifetime_monitor = ProcessLifetimeMonitor()
            log_event(
                f"Settings UI is running at http://{HOST}:{PORT}",
                SUCCESS_LEVEL,
            )
            open_default_browser()
            log_event("Press Ctrl+C to stop.")
            try:
                with ShutdownSignalHandlers(), lifetime_monitor:
                    server.serve_forever()
            except KeyboardInterrupt:
                if lifetime_monitor.reason:
                    log_event(
                        "Settings UI stopped because its console owner disappeared: "
                        f"{lifetime_monitor.reason}",
                        logging.WARNING,
                    )
                else:
                    log_event("Settings UI stopped.")
            finally:
                server.server_close()
    except AlreadyRunningError as e:
        log_event(
            f"Settings UI is already running (PID {e.pid}).",
            logging.ERROR,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
