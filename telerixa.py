from telethon import TelegramClient
import asyncio
import json
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import aiohttp
import os
import shutil
import sqlite3
import sys
import tempfile
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from i18n import configure_language, normalize_language, tr

APP_NAME = "Telerixa"
__version__ = "0.2.3"

# Logging
LOG_DIR = "logs"
BOT_LOG_FILE = os.path.join(LOG_DIR, "bot.log")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        BOT_LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger(__name__)

# ===== FILE-BASED CONFIGURATION =====

CONFIG_FILE = "config.json"

CONFIG_RELOAD_KEYS = (
    "LANGUAGE",
    "DISCORD_WEBHOOK_URL",
    "DISCORD_ALERT_USER_ID",
    "TELEGRAM_CHANNELS",
    "CHECK_INTERVAL",
    "MAX_MESSAGE_LENGTH",
    "TIMEZONE",
    "DISCORD_FILE_LIMIT_MB",
    "LARGE_FILE_ACTION",
    "STARTUP_CATCH_UP_LIMIT",
    "MAX_QUEUE_ATTEMPTS",
)

VALID_LARGE_FILE_ACTIONS = {
    "send_text_link",
    "skip_post",
    "try_send_then_text",
}

ALBUM_LOOKUP_RADIUS = 20
ALBUM_MESSAGES_CACHE_ATTR = "_telerixa_album_messages_cache"


class SendResult:
    def __init__(self, ok, error="", terminal=False):
        self.ok = bool(ok)
        self.error = str(error or "")
        self.terminal = bool(terminal)

    def __bool__(self):
        return self.ok

    @classmethod
    def success(cls):
        return cls(True)

    @classmethod
    def retry(cls, error):
        return cls(False, error, terminal=False)

    @classmethod
    def terminal_failure(cls, error):
        return cls(False, error, terminal=True)


def as_send_result(value, fallback_error=None):
    if fallback_error is None:
        fallback_error = tr("send.false")
    if isinstance(value, SendResult):
        return value
    if value:
        return SendResult.success()
    return SendResult.retry(fallback_error)


def get_alert_mention():
    return f"<@{DISCORD_ALERT_USER_ID}> " if DISCORD_ALERT_USER_ID else ""


def read_config_file(exit_on_error=False):
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(tr("config.file_missing", file=CONFIG_FILE))
        if exit_on_error:
            sys.exit(1)
    except json.JSONDecodeError:
        logger.error(tr("config.invalid_json", file=CONFIG_FILE))
        if exit_on_error:
            sys.exit(1)
    return None


config = read_config_file(exit_on_error=True)

# Read values from config.
LANGUAGE = normalize_language(config.get("LANGUAGE", "ru"))
configure_language(LANGUAGE)
DISCORD_WEBHOOK_URL = config.get("DISCORD_WEBHOOK_URL", "")
DISCORD_ALERT_USER_ID = str(config.get("DISCORD_ALERT_USER_ID", "")).strip()
TELEGRAM_API_ID = config.get("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = config.get("TELEGRAM_API_HASH", "")
TELEGRAM_CHANNELS = config.get("TELEGRAM_CHANNELS", [])
CHECK_INTERVAL = config.get("CHECK_INTERVAL", 60)
MAX_MESSAGE_LENGTH = config.get("MAX_MESSAGE_LENGTH", 2000)
TIMEZONE = config.get("TIMEZONE", "Europe/Berlin")
DISCORD_FILE_LIMIT_MB = config.get("DISCORD_FILE_LIMIT_MB", 25)
LARGE_FILE_ACTION = config.get("LARGE_FILE_ACTION", "send_text_link")
STATE_DB_FILE = config.get("STATE_DB_FILE", "bot_state.db")
DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = DB_TIMEOUT_SECONDS * 1000
CONFIG_MTIME = os.path.getmtime(CONFIG_FILE)
QUEUE_RETRY_LIMIT = config.get("QUEUE_RETRY_LIMIT", 20)
STARTUP_CATCH_UP_LIMIT = config.get(
    "STARTUP_CATCH_UP_LIMIT",
    0 if config.get("SKIP_BACKLOG_ON_START", False) else 10,
)
MAX_QUEUE_ATTEMPTS = max(1, int(config.get("MAX_QUEUE_ATTEMPTS", 24)))

if LARGE_FILE_ACTION not in VALID_LARGE_FILE_ACTIONS:
    logger.warning(
        tr("config.invalid_large_file_action", action=repr(LARGE_FILE_ACTION))
    )
    LARGE_FILE_ACTION = "send_text_link"

if not TELEGRAM_CHANNELS or not DISCORD_WEBHOOK_URL:
    logger.error(tr("config.required_missing"))
    sys.exit(1)


def load_timezone(timezone_name):
    """Load the configured timezone, with a fallback for Windows without tzdata."""
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        fallback_timezone = datetime.now().astimezone().tzinfo
        logger.warning(
            tr("config.timezone_not_found", timezone=timezone_name)
        )
        return fallback_timezone


APP_TIMEZONE = load_timezone(TIMEZONE)


def get_now_ts():
    return datetime.now(APP_TIMEZONE).timestamp()


def format_ts(timestamp):
    return datetime.fromtimestamp(float(timestamp), APP_TIMEZONE).isoformat()


def parse_ts(value, fallback_ts=None):
    if fallback_ts is None:
        fallback_ts = get_now_ts()

    if value is None:
        return fallback_ts

    if isinstance(value, (int, float)):
        return float(value)

    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return fallback_ts


def normalize_runtime_config(new_config):
    normalized = {
        "LANGUAGE": normalize_language(new_config.get("LANGUAGE", LANGUAGE)),
        "DISCORD_WEBHOOK_URL": new_config.get("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
        "DISCORD_ALERT_USER_ID": str(new_config.get("DISCORD_ALERT_USER_ID", DISCORD_ALERT_USER_ID)).strip(),
        "TELEGRAM_CHANNELS": new_config.get("TELEGRAM_CHANNELS", TELEGRAM_CHANNELS),
        "CHECK_INTERVAL": new_config.get("CHECK_INTERVAL", CHECK_INTERVAL),
        "MAX_MESSAGE_LENGTH": new_config.get("MAX_MESSAGE_LENGTH", MAX_MESSAGE_LENGTH),
        "TIMEZONE": new_config.get("TIMEZONE", TIMEZONE),
        "DISCORD_FILE_LIMIT_MB": new_config.get("DISCORD_FILE_LIMIT_MB", DISCORD_FILE_LIMIT_MB),
        "LARGE_FILE_ACTION": new_config.get("LARGE_FILE_ACTION", LARGE_FILE_ACTION),
        "STARTUP_CATCH_UP_LIMIT": new_config.get("STARTUP_CATCH_UP_LIMIT", STARTUP_CATCH_UP_LIMIT),
        "MAX_QUEUE_ATTEMPTS": new_config.get("MAX_QUEUE_ATTEMPTS", MAX_QUEUE_ATTEMPTS),
    }

    if not isinstance(normalized["TELEGRAM_CHANNELS"], list):
        normalized["TELEGRAM_CHANNELS"] = TELEGRAM_CHANNELS
    else:
        normalized["TELEGRAM_CHANNELS"] = [
            str(channel).strip().lstrip("@")
            for channel in normalized["TELEGRAM_CHANNELS"]
            if str(channel).strip()
        ]

    try:
        normalized["CHECK_INTERVAL"] = max(5, int(normalized["CHECK_INTERVAL"]))
    except (TypeError, ValueError):
        normalized["CHECK_INTERVAL"] = CHECK_INTERVAL

    try:
        normalized["MAX_MESSAGE_LENGTH"] = max(1, int(normalized["MAX_MESSAGE_LENGTH"]))
    except (TypeError, ValueError):
        normalized["MAX_MESSAGE_LENGTH"] = MAX_MESSAGE_LENGTH

    try:
        normalized["DISCORD_FILE_LIMIT_MB"] = max(1, int(normalized["DISCORD_FILE_LIMIT_MB"]))
    except (TypeError, ValueError):
        normalized["DISCORD_FILE_LIMIT_MB"] = DISCORD_FILE_LIMIT_MB

    try:
        normalized["STARTUP_CATCH_UP_LIMIT"] = max(0, int(normalized["STARTUP_CATCH_UP_LIMIT"]))
    except (TypeError, ValueError):
        normalized["STARTUP_CATCH_UP_LIMIT"] = STARTUP_CATCH_UP_LIMIT

    try:
        normalized["MAX_QUEUE_ATTEMPTS"] = max(1, int(normalized["MAX_QUEUE_ATTEMPTS"]))
    except (TypeError, ValueError):
        normalized["MAX_QUEUE_ATTEMPTS"] = MAX_QUEUE_ATTEMPTS

    if normalized["LARGE_FILE_ACTION"] not in VALID_LARGE_FILE_ACTIONS:
        logger.warning(
            tr("config.invalid_large_file_action", action=repr(normalized["LARGE_FILE_ACTION"]))
        )
        normalized["LARGE_FILE_ACTION"] = "send_text_link"

    return normalized


def apply_runtime_config(new_config):
    global LANGUAGE
    global DISCORD_WEBHOOK_URL
    global DISCORD_ALERT_USER_ID
    global TELEGRAM_CHANNELS
    global CHECK_INTERVAL
    global MAX_MESSAGE_LENGTH
    global TIMEZONE
    global DISCORD_FILE_LIMIT_MB
    global LARGE_FILE_ACTION
    global STARTUP_CATCH_UP_LIMIT
    global MAX_QUEUE_ATTEMPTS
    global APP_TIMEZONE

    normalized = normalize_runtime_config(new_config)
    old_values = {
        "LANGUAGE": LANGUAGE,
        "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
        "DISCORD_ALERT_USER_ID": DISCORD_ALERT_USER_ID,
        "TELEGRAM_CHANNELS": TELEGRAM_CHANNELS,
        "CHECK_INTERVAL": CHECK_INTERVAL,
        "MAX_MESSAGE_LENGTH": MAX_MESSAGE_LENGTH,
        "TIMEZONE": TIMEZONE,
        "DISCORD_FILE_LIMIT_MB": DISCORD_FILE_LIMIT_MB,
        "LARGE_FILE_ACTION": LARGE_FILE_ACTION,
        "STARTUP_CATCH_UP_LIMIT": STARTUP_CATCH_UP_LIMIT,
        "MAX_QUEUE_ATTEMPTS": MAX_QUEUE_ATTEMPTS,
    }

    LANGUAGE = normalized["LANGUAGE"]
    configure_language(LANGUAGE)
    DISCORD_WEBHOOK_URL = normalized["DISCORD_WEBHOOK_URL"]
    DISCORD_ALERT_USER_ID = normalized["DISCORD_ALERT_USER_ID"]
    TELEGRAM_CHANNELS = normalized["TELEGRAM_CHANNELS"]
    CHECK_INTERVAL = normalized["CHECK_INTERVAL"]
    MAX_MESSAGE_LENGTH = normalized["MAX_MESSAGE_LENGTH"]
    TIMEZONE = normalized["TIMEZONE"]
    DISCORD_FILE_LIMIT_MB = normalized["DISCORD_FILE_LIMIT_MB"]
    LARGE_FILE_ACTION = normalized["LARGE_FILE_ACTION"]
    STARTUP_CATCH_UP_LIMIT = normalized["STARTUP_CATCH_UP_LIMIT"]
    MAX_QUEUE_ATTEMPTS = normalized["MAX_QUEUE_ATTEMPTS"]

    if old_values["TIMEZONE"] != TIMEZONE:
        APP_TIMEZONE = load_timezone(TIMEZONE)

    changed_keys = [
        key for key in CONFIG_RELOAD_KEYS
        if old_values[key] != normalized[key]
    ]

    if changed_keys:
        logger.info(tr("config.updated", keys=", ".join(changed_keys)))


def reload_config_if_changed():
    global CONFIG_MTIME

    try:
        current_mtime = os.path.getmtime(CONFIG_FILE)
    except OSError as e:
        logger.warning(tr("config.check_failed", file=CONFIG_FILE, error=e))
        return

    if current_mtime == CONFIG_MTIME:
        return

    new_config = read_config_file(exit_on_error=False)
    if new_config is None:
        logger.warning(tr("config.keep_previous"))
        return

    apply_runtime_config(new_config)
    CONFIG_MTIME = current_mtime


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
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
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
    conn = sqlite3.connect(STATE_DB_FILE, timeout=DB_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_state_db():
    """Create state tables if they do not exist yet."""
    with connect_state_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_state (
                channel TEXT PRIMARY KEY,
                last_seen_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_messages (
                channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id INTEGER,
                created_at TEXT NOT NULL,
                created_ts REAL NOT NULL,
                updated_at TEXT NOT NULL,
                updated_ts REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL,
                next_retry_ts REAL NOT NULL,
                last_error TEXT,
                PRIMARY KEY (channel, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id INTEGER,
                status TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                processed_ts REAL NOT NULL,
                PRIMARY KEY (channel, message_id)
            )
            """
        )
        ensure_pending_message_columns(conn)
        conn.commit()


def ensure_pending_message_columns(conn):
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(pending_messages)").fetchall()
    }

    for column_name in ("created_ts", "updated_ts", "next_retry_ts"):
        if column_name not in columns:
            conn.execute(f"ALTER TABLE pending_messages ADD COLUMN {column_name} REAL")

    rows = conn.execute(
        """
        SELECT channel, message_id, created_at, updated_at, next_retry_at
        FROM pending_messages
        WHERE created_ts IS NULL
           OR updated_ts IS NULL
           OR next_retry_ts IS NULL
        """
    ).fetchall()

    fallback_ts = get_now_ts()
    for channel, message_id, created_at, updated_at, next_retry_at in rows:
        created_ts = parse_ts(created_at, fallback_ts)
        updated_ts = parse_ts(updated_at, created_ts)
        next_retry_ts = parse_ts(next_retry_at, fallback_ts)
        conn.execute(
            """
            UPDATE pending_messages
            SET created_ts = ?,
                updated_ts = ?,
                next_retry_ts = ?
            WHERE channel = ? AND message_id = ?
            """,
            (created_ts, updated_ts, next_retry_ts, channel, int(message_id)),
        )


def get_last_seen_id(channel):
    """Return the last processed message_id for a channel."""
    with connect_state_db() as conn:
        row = conn.execute(
            "SELECT last_seen_id FROM channel_state WHERE channel = ?",
            (channel,),
        ).fetchone()

    if row is None:
        return None

    return row[0]


def set_last_seen_id(channel, message_id):
    """Save the processed-message boundary for a channel."""
    now = datetime.now(APP_TIMEZONE).isoformat()
    with connect_state_db() as conn:
        conn.execute(
            """
            INSERT INTO channel_state (channel, last_seen_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                last_seen_id = excluded.last_seen_id,
                updated_at = excluded.updated_at
            """,
            (channel, int(message_id), now),
        )
        conn.commit()


def advance_last_seen_id(channel, message_id):
    """Move the channel boundary forward only."""
    current_last_seen_id = get_last_seen_id(channel)
    if current_last_seen_id is None or int(message_id) > current_last_seen_id:
        set_last_seen_id(channel, message_id)


def get_processed_message_state(channel, message_id):
    with connect_state_db() as conn:
        row = conn.execute(
            """
            SELECT status, grouped_id
            FROM processed_messages
            WHERE channel = ? AND message_id = ?
            """,
            (channel, int(message_id)),
        ).fetchone()

    if not row:
        return None, None

    return row[0], row[1]


def has_pending_message(channel, message_id=None, grouped_id=None):
    message_value = int(message_id) if message_id is not None else None
    grouped_value = int(grouped_id) if grouped_id is not None else None

    if message_value is None and grouped_value is None:
        return False

    with connect_state_db() as conn:
        if message_value is not None and grouped_value is not None:
            row = conn.execute(
                """
                SELECT 1
                FROM pending_messages
                WHERE channel = ? AND (message_id = ? OR grouped_id = ?)
                """,
                (channel, message_value, grouped_value),
            ).fetchone()
        elif message_value is not None:
            row = conn.execute(
                """
                SELECT 1
                FROM pending_messages
                WHERE channel = ? AND message_id = ?
                """,
                (channel, message_value),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT 1
                FROM pending_messages
                WHERE channel = ? AND grouped_id = ?
                """,
                (channel, grouped_value),
            ).fetchone()

    return row is not None


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
    grouped_value = int(grouped_id) if grouped_id else None

    with connect_state_db() as conn:
        conn.execute(
            """
            INSERT INTO processed_messages (
                channel,
                message_id,
                grouped_id,
                status,
                processed_at,
                processed_ts
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, message_id) DO UPDATE SET
                grouped_id = excluded.grouped_id,
                status = excluded.status,
                processed_at = excluded.processed_at,
                processed_ts = excluded.processed_ts
            """,
            (
                channel,
                int(message_id),
                grouped_value,
                status,
                now_text,
                now_ts,
            ),
        )
        conn.commit()


def get_retry_delay_seconds(attempts):
    delays = [30, 60, 120, 300, 600, 1800]
    index = min(max(attempts, 0), len(delays) - 1)
    return delays[index]


def add_pending_message(channel, message_id, grouped_id=None, error=""):
    now_ts = get_now_ts()
    now_text = format_ts(now_ts)
    grouped_value = int(grouped_id) if grouped_id else None

    with connect_state_db() as conn:
        conn.execute(
            """
            INSERT INTO pending_messages (
                channel,
                message_id,
                grouped_id,
                created_at,
                created_ts,
                updated_at,
                updated_ts,
                attempts,
                next_retry_at,
                next_retry_ts,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(channel, message_id) DO UPDATE SET
                grouped_id = excluded.grouped_id,
                updated_at = excluded.updated_at,
                updated_ts = excluded.updated_ts,
                last_error = excluded.last_error
            """,
            (
                channel,
                int(message_id),
                grouped_value,
                now_text,
                now_ts,
                now_text,
                now_ts,
                now_text,
                now_ts,
                error,
            ),
        )
        conn.commit()


def mark_pending_failed(channel, message_id, error):
    now_ts = get_now_ts()
    now_text = format_ts(now_ts)

    with connect_state_db() as conn:
        row = conn.execute(
            """
            SELECT attempts
            FROM pending_messages
            WHERE channel = ? AND message_id = ?
            """,
            (channel, int(message_id)),
        ).fetchone()

        attempts = (row[0] if row else 0) + 1
        next_retry_ts = now_ts + get_retry_delay_seconds(attempts)
        next_retry_text = format_ts(next_retry_ts)

        conn.execute(
            """
            UPDATE pending_messages
            SET attempts = ?,
                updated_at = ?,
                updated_ts = ?,
                next_retry_at = ?,
                next_retry_ts = ?,
                last_error = ?
            WHERE channel = ? AND message_id = ?
            """,
            (
                attempts,
                now_text,
                now_ts,
                next_retry_text,
                next_retry_ts,
                str(error)[:500],
                channel,
                int(message_id),
            ),
        )
        conn.commit()
    return attempts


def delete_pending_message(channel, message_id):
    with connect_state_db() as conn:
        conn.execute(
            "DELETE FROM pending_messages WHERE channel = ? AND message_id = ?",
            (channel, int(message_id)),
        )
        conn.commit()


def log_discarded_message(channel, message_id, reason, attempts, source):
    logger.error("=" * 72)
    logger.error(tr("discard.source", source=source))
    logger.error(tr("discard.post", channel=channel, message_id=message_id))
    logger.error(tr("discard.link", channel=channel, message_id=message_id))
    logger.error(tr("discard.attempts", attempts=attempts))
    logger.error(tr("discard.reason", reason=reason))
    logger.error("=" * 72)


def drop_pending_message(channel, message_id, grouped_id, reason, attempts, album_ids=None):
    album_ids = album_ids or [int(message_id)]
    grouped_value = grouped_id if grouped_id else None
    delete_pending_message(channel, message_id)
    for album_message_id in album_ids:
        mark_processed_message(channel, album_message_id, grouped_value, "failed")

    log_discarded_message(channel, message_id, reason, attempts, "retry queue")


async def notify_dropped_message(channel, message_id, reason, attempts):
    if not DISCORD_WEBHOOK_URL or not DISCORD_ALERT_USER_ID:
        return

    payload = {
        "content": tr(
            "alert.dropped",
            mention=get_alert_mention(),
            channel=channel,
            message_id=message_id,
            attempts=attempts,
            reason=str(reason)[:300],
        ),
        "allowed_mentions": {"users": [DISCORD_ALERT_USER_ID]},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as session:
            async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=timeout) as response:
                if response.status not in [200, 204]:
                    logger.warning(tr("alert.dropped_failed_status", status=response.status))
    except Exception as e:
        logger.warning(tr("alert.dropped_failed_error", error=describe_network_error(e)))


def get_due_pending_messages(limit=None):
    if limit is None:
        limit = QUEUE_RETRY_LIMIT

    now_ts = get_now_ts()
    with connect_state_db() as conn:
        rows = conn.execute(
            """
            SELECT channel, message_id, grouped_id, attempts, last_error
            FROM pending_messages
            WHERE next_retry_ts <= ?
            ORDER BY next_retry_ts ASC, created_ts ASC
            LIMIT ?
            """,
            (now_ts, int(limit)),
        ).fetchall()

    return rows


def get_pending_count():
    with connect_state_db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_messages").fetchone()
    return row[0] if row else 0


def get_pending_retry_status():
    now_ts = get_now_ts()
    with connect_state_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), MIN(next_retry_ts)
            FROM pending_messages
            """
        ).fetchone()
        due_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM pending_messages
            WHERE next_retry_ts <= ?
            """,
            (now_ts,),
        ).fetchone()

    pending_count = row[0] if row else 0
    next_retry_ts = row[1] if row else None
    due_count = due_row[0] if due_row else 0
    return pending_count, due_count, next_retry_ts


def has_channel_state(channel):
    return get_last_seen_id(channel) is not None


def migrate_legacy_seen_messages():
    """Migrate the old seen_messages.json into SQLite when needed."""
    legacy_seen = load_legacy_seen_messages()
    if not legacy_seen:
        return

    max_ids_by_channel = {}
    processed_keys = set()
    for message_key in legacy_seen:
        if not isinstance(message_key, str) or "_" not in message_key:
            continue

        channel, message_id = message_key.rsplit("_", 1)
        if not message_id.isdigit():
            continue

        processed_keys.add((channel, int(message_id)))
        max_ids_by_channel[channel] = max(
            max_ids_by_channel.get(channel, 0),
            int(message_id),
        )

    migrated_channels = 0
    for channel, message_id in max_ids_by_channel.items():
        if not has_channel_state(channel):
            set_last_seen_id(channel, message_id)
            migrated_channels += 1

    if migrated_channels:
        logger.info(tr("legacy.migrated_channels", count=migrated_channels))

    if processed_keys:
        now_ts = get_now_ts()
        now_text = format_ts(now_ts)
        with connect_state_db() as conn:
            inserted = 0
            for channel, message_id in processed_keys:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_messages (
                        channel,
                        message_id,
                        grouped_id,
                        status,
                        processed_at,
                        processed_ts
                    )
                    VALUES (?, ?, NULL, 'sent', ?, ?)
                    """,
                    (channel, int(message_id), now_text, now_ts),
                )
                inserted += cursor.rowcount
            conn.commit()

        if inserted:
            logger.info(tr("legacy.processed_inserted", count=inserted))


def all_channels_initialized():
    return all(has_channel_state(channel) for channel in TELEGRAM_CHANNELS)


def get_post_url(message, channel_name):
    """Build a public Telegram post URL."""
    return f"https://t.me/{channel_name}/{message.id}"


def get_message_time(message):
    """Return the post time in the configured local timezone."""
    return get_message_datetime(message).strftime("%d.%m.%Y %H:%M")


def get_message_datetime(message):
    """Return the post datetime in the configured local timezone."""
    try:
        return message.date.astimezone(APP_TIMEZONE)
    except Exception:
        return message.date


def get_forward_info(message):
    """Return a human-readable source description for forwarded posts."""
    forward = getattr(message, "forward", None)
    if not forward:
        return None

    from_name = getattr(forward, "from_name", None)
    chat = getattr(forward, "chat", None)

    if chat:
        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        username = getattr(chat, "username", None)
        if username:
            return tr("telegram.forward_from_channel_link", title=title, username=username)
        if title:
            return tr("telegram.forward_from_channel", title=title)

    if from_name:
        return tr("telegram.forward_from_user", name=from_name)

    return tr("telegram.forward_from_unknown")


def trim_context_text(text, limit=700):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + "..."


def repair_mojibake(text):
    if not text:
        return text

    text = str(text)
    if not any(marker in text for marker in ("Ð", "Ñ", "Â", "â")):
        return text

    try:
        fixed_text = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text

    if any(marker in fixed_text for marker in ("Ð", "Ñ")):
        return text

    return fixed_text


def format_blockquote(text):
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


async def get_reply_info(message):
    """Return Telegram reply/quote context when a post replies to another post."""
    reply_to = getattr(message, "reply_to", None)
    if not reply_to:
        return None

    reply_message = None
    try:
        reply_message = await message.get_reply_message()
    except Exception as e:
        logger.warning(tr("telegram.reply_fetch_failed", message_id=message.id, error=e))

    if not reply_message:
        reply_peer = getattr(reply_to, "reply_to_peer_id", None)
        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        if reply_peer and reply_id:
            try:
                reply_message = await telegram_client.get_messages(reply_peer, ids=reply_id)
            except Exception as e:
                logger.warning(
                    tr("telegram.cross_reply_fetch_failed", message_id=message.id, error=e)
                )

    chat = getattr(reply_message, "chat", None) if reply_message else None
    sender = getattr(reply_message, "sender", None) if reply_message else None
    title = (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or getattr(sender, "first_name", None)
        or tr("telegram.reply_unknown_title")
    )
    title = repair_mojibake(title)
    username = getattr(chat, "username", None)

    if username and reply_message:
        header = tr(
            "telegram.reply_to_link",
            title=title,
            username=username,
            message_id=reply_message.id,
        )
    else:
        header = tr("telegram.reply_to", title=title)

    reply_text = (
        getattr(reply_to, "quote_text", None)
        or (reply_message.text if reply_message else "")
    )
    reply_text = trim_context_text(repair_mojibake(reply_text))
    if reply_text:
        return f"{header}\n\n{format_blockquote(reply_text)}"

    return header


def split_text(text, limit):
    """Split long text without cutting through paragraphs when possible."""
    if not text:
        return []

    chunks = []
    current = ""

    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(paragraph) > limit:
            words = paragraph.split()
            for word in words:
                if len(word) > limit:
                    if current:
                        chunks.append(current)
                        current = ""
                    chunks.extend(word[i:i + limit] for i in range(0, len(word), limit))
                    continue

                candidate = f"{current} {word}".strip()
                if len(candidate) <= limit:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = word
            continue

        separator = "\n\n" if current else ""
        candidate = f"{current}{separator}{paragraph}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def select_album_text_message(album_messages, fallback_message):
    text_messages = [message for message in album_messages if message and message.text]
    if not text_messages:
        return fallback_message

    return max(text_messages, key=lambda message: len(message.text or ""))


async def build_message_text(telegram_message, channel_name, text_message=None):
    """Build post text with metadata and Telegram link."""
    source_message = text_message or telegram_message
    lines = [tr("telegram.post_link", url=get_post_url(source_message, channel_name))]
    forward_info = get_forward_info(source_message) or get_forward_info(telegram_message)

    if forward_info:
        lines.append(forward_info)

    reply_info = await get_reply_info(source_message)
    if reply_info:
        lines.append(reply_info)

    if source_message.text:
        lines.append(source_message.text)

    return "\n\n".join(lines)


async def post_json(session, payload):
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=timeout) as response:
        if response.status not in [200, 204]:
            error = f"Discord webhook error {response.status}"
            logger.error(tr("discord.webhook_error", status=response.status))
            if response.status in (400, 413):
                return SendResult.terminal_failure(error)
            return SendResult.retry(error)
        return SendResult.success()


def describe_network_error(error):
    error_text = str(error)
    lower_error = error_text.lower()
    if "name or service not known" in lower_error or "getaddrinfo failed" in lower_error:
        return tr("network.dns_discord", error=error_text)
    return error_text


async def send_text_chunks(session, chunks, channel_name, message_time, start_part=1, total_parts=None):
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

        result = await post_json(session, payload)
        if not result:
            return result

    return SendResult.success()


async def get_latest_message_id(entity):
    latest_messages = await telegram_client.get_messages(entity, limit=1)
    return latest_messages[0].id if latest_messages else 0


def is_forwardable_message(message):
    return bool(message and (message.text or message.media))


async def collect_startup_tail(entity, last_seen_id, limit):
    """Collect the latest limit posts after last_seen_id without assuming dense IDs."""
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


def format_album_ids(album_messages):
    return ", ".join(str(album_message.id) for album_message in album_messages)


async def get_album_messages(channel_name, message):
    if not message.grouped_id:
        return [message]

    cached_album_messages = getattr(message, ALBUM_MESSAGES_CACHE_ATTR, None)
    if cached_album_messages is not None:
        return cached_album_messages

    start_id = max(1, message.id - ALBUM_LOOKUP_RADIUS)
    end_id = message.id + ALBUM_LOOKUP_RADIUS
    nearby_ids = list(range(start_id, end_id + 1))

    try:
        messages_batch = await telegram_client.get_messages(f"@{channel_name}", ids=nearby_ids)
        album_messages = [
            album_message
            for album_message in messages_batch
            if album_message and album_message.grouped_id == message.grouped_id
        ]
        album_messages.sort(key=lambda album_message: album_message.id)
    except Exception as e:
        logger.warning(
            tr(
                "media.album_lookup_failed",
                channel=channel_name,
                message_id=message.id,
                grouped_id=message.grouped_id,
                error=e,
            )
        )
        fallback_album_messages = [message]
        try:
            setattr(message, ALBUM_MESSAGES_CACHE_ATTR, fallback_album_messages)
        except Exception:
            pass
        return fallback_album_messages

    if not album_messages:
        album_messages = [message]

    try:
        setattr(message, ALBUM_MESSAGES_CACHE_ATTR, album_messages)
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
                radius=ALBUM_LOOKUP_RADIUS,
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


async def get_album_message_ids(channel_name, message):
    album_messages = await get_album_messages(channel_name, message)
    return [album_message.id for album_message in album_messages]


async def get_processed_until_id(channel_name, message):
    """Return the highest Telegram message_id covered by a message or album."""
    return max(await get_album_message_ids(channel_name, message))


async def catch_up_channels_on_start():
    """Catch up only a small fresh tail on startup and skip deep backlog."""
    if not telegram_client.is_connected():
        await telegram_client.connect()

    logger.info(
        tr("startup.catch_up_begin", limit=STARTUP_CATCH_UP_LIMIT)
    )

    for channel in TELEGRAM_CHANNELS:
        try:
            entity = await telegram_client.get_entity(f"@{channel}")
            latest_message_id = await get_latest_message_id(entity)
            last_seen_id = get_last_seen_id(channel)

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

            if STARTUP_CATCH_UP_LIMIT <= 0:
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

            to_process, has_older_forwardable = await collect_startup_tail(
                entity,
                last_seen_id,
                STARTUP_CATCH_UP_LIMIT,
            )

            if not to_process:
                advance_last_seen_id(channel, latest_message_id)
                logger.info(tr("startup.no_forwardable_tail", channel=channel))
                continue

            startup_boundary_id = last_seen_id
            if has_older_forwardable:
                startup_boundary_id = max(last_seen_id, min(message.id for message in to_process) - 1)
                set_last_seen_id(channel, startup_boundary_id)
                logger.info(
                    tr(
                        "startup.backlog_skipped",
                        channel=channel,
                        last_seen=last_seen_id,
                        latest=latest_message_id,
                        boundary=startup_boundary_id,
                        count=len(to_process),
                    )
                )

            logger.info(
                tr(
                    "startup.tail_to_process",
                    channel=channel,
                    count=len(to_process),
                    boundary=startup_boundary_id,
                    latest=latest_message_id,
                )
            )
            sent_count, queued_count, skipped_processed, last_processed_id = (
                await process_messages_for_channel(channel, to_process, startup_boundary_id)
            )
            advance_last_seen_id(channel, max(last_processed_id, latest_message_id))
            logger.info(
                tr(
                    "startup.tail_done",
                    channel=channel,
                    sent=sent_count,
                    queued=queued_count,
                    skipped=skipped_processed,
                    boundary=max(last_processed_id, latest_message_id),
                )
            )
        except Exception as e:
            logger.error(tr("startup.channel_error", channel=channel, error=e))


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

    for channel, message_id, grouped_id, attempts, last_error in pending_messages:
        try:
            entity = await telegram_client.get_entity(f"@{channel}")
            message = await telegram_client.get_messages(entity, ids=int(message_id))

            if not message or not (message.text or message.media):
                logger.warning(
                    tr("queue.message_unavailable", channel=channel, message_id=message_id)
                )
                delete_pending_message(channel, message_id)
                continue

            send_result = as_send_result(
                await send_to_discord(message, channel),
                tr("send.retry_false"),
            )
            if send_result:
                album_ids = await get_album_message_ids(channel, message)
                processed_until_id = max(album_ids)
                grouped_value = grouped_id if grouped_id else message.grouped_id
                advance_last_seen_id(channel, processed_until_id)
                for album_message_id in album_ids:
                    mark_processed_message(channel, album_message_id, grouped_value, "sent")
                delete_pending_message(channel, message_id)
                delivered_count += 1
                logger.info(
                    tr(
                        "queue.delivered",
                        channel=channel,
                        message_id=message_id,
                        boundary=processed_until_id,
                    )
                )
            else:
                error_text = send_result.error or tr("send.retry_false")
                attempts_after = mark_pending_failed(channel, message_id, error_text)
                if send_result.terminal or attempts_after >= MAX_QUEUE_ATTEMPTS:
                    album_ids = await get_album_message_ids(channel, message)
                    reason = (
                        tr("queue.terminal_reason", error=error_text)
                        if send_result.terminal
                        else tr(
                            "queue.max_attempts_reason",
                            max_attempts=MAX_QUEUE_ATTEMPTS,
                            error=error_text,
                        )
                    )
                    drop_pending_message(channel, message_id, grouped_id or message.grouped_id, reason, attempts_after, album_ids)
                    await notify_dropped_message(channel, message_id, reason, attempts_after)
                else:
                    logger.warning(
                        tr(
                            "queue.kept",
                            channel=channel,
                            message_id=message_id,
                            attempts=attempts_after,
                            max_attempts=MAX_QUEUE_ATTEMPTS,
                            reason=error_text,
                        )
                    )
        except Exception as e:
            error_text = describe_network_error(e)
            attempts_after = mark_pending_failed(channel, message_id, error_text)
            if attempts_after >= MAX_QUEUE_ATTEMPTS:
                reason = tr(
                    "queue.max_attempts_reason",
                    max_attempts=MAX_QUEUE_ATTEMPTS,
                    error=error_text,
                )
                drop_pending_message(channel, message_id, grouped_id, reason, attempts_after)
                await notify_dropped_message(channel, message_id, reason, attempts_after)
            else:
                logger.warning(
                    tr(
                        "queue.send_failed",
                        channel=channel,
                        message_id=message_id,
                        reason=error_text,
                        attempts=attempts_after,
                        max_attempts=MAX_QUEUE_ATTEMPTS,
                    )
                )

    remaining_count = get_pending_count()
    if delivered_count or remaining_count:
        logger.info(
            tr("queue.summary", delivered=delivered_count, remaining=remaining_count)
        )

    return delivered_count


async def process_messages_for_channel(channel, messages, last_seen_id):
    total_sent = 0
    queued_count = 0
    skipped_processed = 0
    already_sent_grouped_ids = set()
    last_processed_id = last_seen_id

    for message in messages:
        if is_processed_message(channel, message.id):
            skipped_processed += 1
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            logger.info(tr("channel.already_processed", channel=channel, message_id=message.id))
            continue

        if message.grouped_id:
            if message.grouped_id in already_sent_grouped_ids:
                continue

            send_result = as_send_result(
                await send_to_discord(message, channel),
                tr("send.initial_album_failed"),
            )
            already_sent_grouped_ids.add(message.grouped_id)
            album_ids = await get_album_message_ids(channel, message)

            if send_result:
                if album_ids:
                    last_processed_id = max(last_processed_id, max(album_ids))
                    advance_last_seen_id(channel, last_processed_id)
                    for album_message_id in album_ids:
                        mark_processed_message(channel, album_message_id, message.grouped_id, "sent")
                total_sent += 1
                logger.info(
                    tr(
                        "channel.album_processed",
                        channel=channel,
                        message_id=message.id,
                        boundary=last_processed_id,
                    )
                )
            elif send_result.terminal:
                reason = send_result.error or tr("send.initial_album_terminal")
                if album_ids:
                    last_processed_id = max(last_processed_id, max(album_ids))
                    advance_last_seen_id(channel, last_processed_id)
                    for album_message_id in album_ids:
                        mark_processed_message(channel, album_message_id, message.grouped_id, "failed")
                log_discarded_message(channel, message.id, reason, 1, "initial terminal error")
                await notify_dropped_message(channel, message.id, reason, 1)
                logger.error(
                    tr(
                        "channel.album_terminal_not_queued",
                        channel=channel,
                        message_id=message.id,
                        reason=reason,
                    )
                )
            else:
                add_pending_message(
                    channel,
                    message.id,
                    message.grouped_id,
                    send_result.error or tr("send.initial_album_failed"),
                )
                if album_ids:
                    last_processed_id = max(last_processed_id, max(album_ids))
                    advance_last_seen_id(channel, last_processed_id)
                    for album_message_id in album_ids:
                        mark_processed_message(channel, album_message_id, message.grouped_id, "queued")
                queued_count += 1
                logger.warning(tr("channel.album_queued", channel=channel, message_id=message.id))
            continue

        send_result = as_send_result(
            await send_to_discord(message, channel),
            tr("send.initial_message_failed"),
        )
        if send_result:
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            mark_processed_message(channel, message.id, None, "sent")
            total_sent += 1
            logger.info(
                tr(
                    "channel.message_processed",
                    channel=channel,
                    message_id=message.id,
                    boundary=last_processed_id,
                )
            )
        elif send_result.terminal:
            reason = send_result.error or tr("send.initial_message_terminal")
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            mark_processed_message(channel, message.id, None, "failed")
            log_discarded_message(channel, message.id, reason, 1, "initial terminal error")
            await notify_dropped_message(channel, message.id, reason, 1)
            logger.error(
                tr(
                    "channel.message_terminal_not_queued",
                    channel=channel,
                    message_id=message.id,
                    reason=reason,
                )
            )
        else:
            add_pending_message(channel, message.id, None, send_result.error or tr("send.initial_message_failed"))
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            mark_processed_message(channel, message.id, None, "queued")
            queued_count += 1
            logger.warning(tr("channel.message_queued", channel=channel, message_id=message.id))

    return total_sent, queued_count, skipped_processed, last_processed_id


# ===== TELEGRAM NEWS CHECK =====

async def check_telegram_news():
    """Check channels for new messages."""
    try:
        if not telegram_client.is_connected():
            await telegram_client.connect()

        total_sent_this_turn = 0

        for channel in TELEGRAM_CHANNELS:
            try:
                entity = await telegram_client.get_entity(f"@{channel}")
            except ValueError:
                logger.error(tr("channel.not_found", channel=channel))
                continue
            except Exception as e:
                logger.error(tr("channel.fetch_error", channel=channel, error=e))
                continue

            last_seen_id = get_last_seen_id(channel)
            latest_message_id = await get_latest_message_id(entity)

            if last_seen_id is None:
                set_last_seen_id(channel, latest_message_id)
                logger.info(tr("channel.initial_boundary", channel=channel, message_id=latest_message_id))
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

            to_process = []
            async for message in telegram_client.iter_messages(entity, min_id=last_seen_id, reverse=True):
                if is_forwardable_message(message):
                    to_process.append(message)

            if not to_process:
                logger.info(tr("channel.no_forwardable_posts", channel=channel))
                continue

            logger.info(tr("channel.candidates_found", channel=channel, count=len(to_process)))

            sent_count, queued_this_channel, skipped_processed, last_processed_id = (
                await process_messages_for_channel(channel, to_process, last_seen_id)
            )
            total_sent_this_turn += sent_count

            if last_processed_id > last_seen_id:
                advance_last_seen_id(channel, last_processed_id)

            if queued_this_channel:
                logger.info(tr("channel.queued_count", channel=channel, count=queued_this_channel))
            if skipped_processed:
                logger.info(tr("channel.skipped_processed", channel=channel, count=skipped_processed))

        current_time = datetime.now().strftime("%H:%M:%S")
        if total_sent_this_turn > 0:
            logger.info(
                tr("channel.all_checked_with_sent", time=current_time, count=total_sent_this_turn)
            )
        else:
            logger.info(tr("channel.all_checked_empty", time=current_time))

    except Exception as e:
        logger.error(tr("channel.check_error", error=e))


# ===== DISCORD DELIVERY =====

async def send_to_discord(telegram_message, channel_name):
    """Send Telegram message text and media to Discord via webhook."""
    temp_files = []
    temp_dirs = []

    try:
        text_message = telegram_message
        message_time = get_message_datetime(telegram_message)

        media_files = []
        media_download_failed = False

        if telegram_message.grouped_id:
            try:
                temp_dir = tempfile.mkdtemp()
                temp_dirs.append(temp_dir)
                album_messages = await get_album_messages(channel_name, telegram_message)
                text_message = select_album_text_message(album_messages, telegram_message)
                message_time = get_message_datetime(text_message)

                for msg in album_messages:
                    if msg.media:
                        try:
                            file_path = await telegram_client.download_media(msg.media, temp_dir)
                            if file_path:
                                temp_files.append(file_path)
                                media_files.append(file_path)
                            else:
                                media_download_failed = True
                                logger.warning(tr("media.album_path_missing"))
                        except Exception as e:
                            media_download_failed = True
                            logger.warning(tr("media.album_download_failed", error=e))
                if not media_files:
                    media_download_failed = True
            except Exception as e:
                media_download_failed = True
                logger.error(tr("media.album_processing_failed", error=e))

        elif telegram_message.media:
            try:
                temp_dir = tempfile.mkdtemp()
                temp_dirs.append(temp_dir)
                file_path = await telegram_client.download_media(telegram_message.media, temp_dir)
                if file_path:
                    temp_files.append(file_path)
                    media_files.append(file_path)
                else:
                    media_download_failed = True
                    logger.warning(tr("media.path_missing"))
            except Exception as e:
                media_download_failed = True
                logger.error(tr("media.download_failed", error=e))

        if media_download_failed:
            logger.warning(tr("media.partial_download"))
            return SendResult.retry(tr("send.media_download_failed"))

        text = await build_message_text(telegram_message, channel_name, text_message)
        text_chunks = split_text(text, MAX_MESSAGE_LENGTH)

        async with aiohttp.ClientSession() as session:
            if text and not media_files:
                return await send_text_chunks(session, text_chunks, channel_name, message_time)

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
                                "name": tr("telegram.author_media_from_channel", channel=channel_name),
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
                for idx, file_path in enumerate(media_files):
                    if not os.path.exists(file_path):
                        continue

                    file_size = os.path.getsize(file_path)
                    if file_size > DISCORD_FILE_LIMIT_MB * 1024 * 1024:
                        skipped_large_files.append(file_path)
                        if LARGE_FILE_ACTION == "try_send_then_text":
                            logger.warning(
                                tr(
                                    "media.file_too_large_try",
                                    file_path=file_path,
                                    limit=DISCORD_FILE_LIMIT_MB,
                                )
                            )
                        else:
                            logger.warning(
                                tr(
                                    "media.file_too_large_skip_attach",
                                    file_path=file_path,
                                    limit=DISCORD_FILE_LIMIT_MB,
                                )
                            )
                            continue

                    with open(file_path, "rb") as f:
                        file_bytes = BytesIO(f.read())

                    form_data.add_field(
                        f"file{idx}",
                        file_bytes,
                        filename=os.path.basename(file_path),
                    )
                    has_valid_files = True

                if not has_valid_files:
                    if skipped_large_files and LARGE_FILE_ACTION == "send_text_link":
                        logger.warning(
                            tr("media.all_large_send_text_link")
                        )
                        if text_chunks:
                            return await send_text_chunks(session, text_chunks, channel_name, message_time)
                        return SendResult.success()

                    if skipped_large_files and LARGE_FILE_ACTION == "skip_post":
                        logger.warning(tr("media.all_large_skip_post"))
                        return SendResult.success()

                    logger.warning(tr("media.no_files_to_send"))
                    return SendResult.retry(tr("send.no_valid_media"))

                try:
                    timeout = aiohttp.ClientTimeout(total=60)
                    async with session.post(DISCORD_WEBHOOK_URL, data=form_data, timeout=timeout) as response:
                        if response.status not in [200, 204]:
                            error = f"Discord webhook media upload error {response.status}"
                            logger.error(tr("discord.media_upload_error", status=response.status))
                            if response.status == 413:
                                if LARGE_FILE_ACTION == "skip_post":
                                    logger.warning(
                                        tr("media.discord_413_skip")
                                    )
                                    return SendResult.success()
                                logger.warning(
                                    tr("media.discord_413_text_link", limit=DISCORD_FILE_LIMIT_MB)
                                )
                                if text_chunks:
                                    return await send_text_chunks(session, text_chunks, channel_name, message_time)
                                return SendResult.terminal_failure(error)
                            if response.status == 400:
                                return SendResult.terminal_failure(error)
                            return SendResult.retry(error)
                    if len(text_chunks) > 1:
                        return await send_text_chunks(
                            session,
                            text_chunks[1:],
                            channel_name,
                            message_time,
                            start_part=2,
                            total_parts=len(text_chunks),
                        )
                    return SendResult.success()
                except Exception as e:
                    error = describe_network_error(e)
                    logger.error(tr("media.discord_send_failed", error=error))
                    return SendResult.retry(error)

            return SendResult.retry(tr("send.nothing_to_send"))

    except Exception as e:
        error = describe_network_error(e)
        logger.error(tr("media.message_send_failed", error=error))
        return SendResult.retry(error)

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
                if os.path.exists(temp_dir) and os.path.commonpath([temp_root, temp_dir]) == temp_root:
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except (OSError, ValueError):
                pass


# ===== MAIN LOOP =====

async def main():
    logger.info(tr("app.starting", app_name=APP_NAME, version=__version__))
    logger.info(tr("app.telegram_channels", channels=", ".join(TELEGRAM_CHANNELS)))
    logger.info(tr("app.check_interval", seconds=CHECK_INTERVAL))

    logger.info(tr("app.connecting_telegram"))

    try:
        if os.path.exists("tg_session.session"):
            logger.info(tr("app.saved_session_found"))
            try:
                await telegram_client.connect()
                if await telegram_client.is_user_authorized():
                    logger.info(tr("app.saved_session_authorized"))
                else:
                    logger.info(tr("app.saved_session_invalid"))
                    await telegram_client.start()
            except Exception as e:
                logger.error(tr("app.saved_session_error", error=e))
                try:
                    os.remove("tg_session.session")
                except OSError:
                    pass
                await telegram_client.start()
        else:
            logger.info(tr("app.no_saved_session"))
            await telegram_client.start()

        init_state_db()
        migrate_legacy_seen_messages()

        await catch_up_channels_on_start()
        logger.info(tr("app.startup_sync_done"))

        logger.info(tr("app.forwarder_running"))
        print("-" * 50)

        retry_count = 0
        while True:
            try:
                reload_config_if_changed()
                await process_pending_messages()
                await check_telegram_news()
                retry_count = 0
                reload_config_if_changed()
                await sleep_with_config_reload(CHECK_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                if "database is locked" in str(e).lower():
                    retry_count += 1
                    await sleep_with_config_reload(min(5 * retry_count, 30))
                else:
                    logger.error(tr("app.main_loop_error", error=e))
                    await sleep_with_config_reload(CHECK_INTERVAL)
    except Exception as e:
        logger.error(tr("app.telegram_start_error", error=e))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info(tr("app.stopped_by_user"))
