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

# Настройка логирования
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

# ===== КОНФИГУРАЦИЯ ИЗ ФАЙЛА =====

CONFIG_FILE = "config.json"

CONFIG_RELOAD_KEYS = (
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


def as_send_result(value, fallback_error="Отправка вернула False"):
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
        logger.error(f"Файл {CONFIG_FILE} не найден! Создай его рядом со скриптом.")
        if exit_on_error:
            sys.exit(1)
    except json.JSONDecodeError:
        logger.error(f"Ошибка чтения {CONFIG_FILE}. Проверь запятые и кавычки.")
        if exit_on_error:
            sys.exit(1)
    return None


config = read_config_file(exit_on_error=True)

# Читаем значения из конфига
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
        f"Неизвестный LARGE_FILE_ACTION={LARGE_FILE_ACTION!r}. "
        "Используем send_text_link."
    )
    LARGE_FILE_ACTION = "send_text_link"

if not TELEGRAM_CHANNELS or not DISCORD_WEBHOOK_URL:
    logger.error("В конфиге отсутствуют обязательные поля: TELEGRAM_CHANNELS или DISCORD_WEBHOOK_URL.")
    sys.exit(1)


def load_timezone(timezone_name):
    """Загрузить таймзону из конфига, с fallback для Windows без tzdata."""
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        fallback_timezone = datetime.now().astimezone().tzinfo
        logger.warning(
            f"Таймзона {timezone_name} не найдена. "
            "Используем локальную таймзону системы. "
            "Для точной Europe/Berlin можно установить пакет tzdata."
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
            f"Неизвестный LARGE_FILE_ACTION={normalized['LARGE_FILE_ACTION']!r}. "
            "Используем send_text_link."
        )
        normalized["LARGE_FILE_ACTION"] = "send_text_link"

    return normalized


def apply_runtime_config(new_config):
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
        logger.info(f"Настройки обновлены без перезапуска: {', '.join(changed_keys)}")


def reload_config_if_changed():
    global CONFIG_MTIME

    try:
        current_mtime = os.path.getmtime(CONFIG_FILE)
    except OSError as e:
        logger.warning(f"Не удалось проверить {CONFIG_FILE}: {e}")
        return

    if current_mtime == CONFIG_MTIME:
        return

    new_config = read_config_file(exit_on_error=False)
    if new_config is None:
        logger.warning("Оставляем предыдущие рабочие настройки.")
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

# ===== ИНИЦИАЛИЗАЦИЯ =====

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

# Старый файл seen_messages.json используется только для мягкой миграции в SQLite
SEEN_MESSAGES_FILE = "seen_messages.json"


def load_legacy_seen_messages():
    """Загрузить старый список отправленных сообщений для миграции."""
    try:
        with open(SEEN_MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            logger.warning(f"Файл {SEEN_MESSAGES_FILE} содержит не список, начинаем с пустой базы.")
            return []
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logger.warning(f"Файл {SEEN_MESSAGES_FILE} поврежден, начинаем с пустой базы.")
        return []


def connect_state_db():
    conn = sqlite3.connect(STATE_DB_FILE, timeout=DB_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_state_db():
    """Создать таблицу состояния каналов, если ее еще нет."""
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
    """Получить последний обработанный message_id канала."""
    with connect_state_db() as conn:
        row = conn.execute(
            "SELECT last_seen_id FROM channel_state WHERE channel = ?",
            (channel,),
        ).fetchone()

    if row is None:
        return None

    return row[0]


def set_last_seen_id(channel, message_id):
    """Сохранить границу обработанных сообщений для канала."""
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
    """Сдвинуть границу канала только вперед."""
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
            f"Канал @{channel}: ID {message_id} помечен как queued, "
            "но записи в очереди нет. Пробуем обработать заново."
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
    logger.error(f"Сообщение выброшено: {source}")
    logger.error(f"Пост: @{channel}/{message_id}")
    logger.error(f"Ссылка: https://t.me/{channel}/{message_id}")
    logger.error(f"Попыток: {attempts}")
    logger.error(f"Причина: {reason}")
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
        "content": (
            f"{get_alert_mention()}WARNING: Telegram post was dropped from retry queue.\n"
            f"Post: https://t.me/{channel}/{message_id}\n"
            f"Attempts: `{attempts}`\n"
            f"Reason: `{str(reason)[:300]}`"
        ),
        "allowed_mentions": {"users": [DISCORD_ALERT_USER_ID]},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as session:
            async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=timeout) as response:
                if response.status not in [200, 204]:
                    logger.warning(f"Не удалось отправить alert о dropped-сообщении: {response.status}")
    except Exception as e:
        logger.warning(f"Не удалось отправить alert о dropped-сообщении: {describe_network_error(e)}")


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
    """Перенести старый seen_messages.json в SQLite, если SQLite еще пустая."""
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
        logger.info(f"Старый seen_messages.json перенесен в SQLite для каналов: {migrated_channels}.")

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
            logger.info(f"Старые seen ID добавлены в дедупликацию: {inserted}.")


def all_channels_initialized():
    return all(has_channel_state(channel) for channel in TELEGRAM_CHANNELS)


def get_post_url(message, channel_name):
    """Собрать ссылку на публичный пост Telegram."""
    return f"https://t.me/{channel_name}/{message.id}"


def get_message_time(message):
    """Вернуть время поста в локальной зоне из конфига."""
    return get_message_datetime(message).strftime("%d.%m.%Y %H:%M")


def get_message_datetime(message):
    """Вернуть datetime поста в локальной зоне из конфига."""
    try:
        return message.date.astimezone(APP_TIMEZONE)
    except Exception:
        return message.date


def get_forward_info(message):
    """Вернуть понятное описание источника, если пост является репостом."""
    forward = getattr(message, "forward", None)
    if not forward:
        return None

    from_name = getattr(forward, "from_name", None)
    chat = getattr(forward, "chat", None)

    if chat:
        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        username = getattr(chat, "username", None)
        if username:
            return f"Репост из [{title}](https://t.me/{username})"
        if title:
            return f"Репост из {title}"

    if from_name:
        return f"Репост от {from_name}"

    return "Репост из другого Telegram-источника"


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
    """Вернуть описание Telegram reply/quote, если пост отвечает на другой пост."""
    reply_to = getattr(message, "reply_to", None)
    if not reply_to:
        return None

    reply_message = None
    try:
        reply_message = await message.get_reply_message()
    except Exception as e:
        logger.warning(f"Не удалось получить reply/quote для поста {message.id}: {e}")

    if not reply_message:
        reply_peer = getattr(reply_to, "reply_to_peer_id", None)
        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        if reply_peer and reply_id:
            try:
                reply_message = await telegram_client.get_messages(reply_peer, ids=reply_id)
            except Exception as e:
                logger.warning(
                    f"Не удалось получить cross-channel reply/quote для поста {message.id}: {e}"
                )

    chat = getattr(reply_message, "chat", None) if reply_message else None
    sender = getattr(reply_message, "sender", None) if reply_message else None
    title = (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or getattr(sender, "first_name", None)
        or "другой Telegram-пост"
    )
    title = repair_mojibake(title)
    username = getattr(chat, "username", None)

    if username and reply_message:
        header = f"Ответ на [{title}](https://t.me/{username}/{reply_message.id})"
    else:
        header = f"Ответ на {title}"

    reply_text = (
        getattr(reply_to, "quote_text", None)
        or (reply_message.text if reply_message else "")
    )
    reply_text = trim_context_text(repair_mojibake(reply_text))
    if reply_text:
        return f"{header}\n\n{format_blockquote(reply_text)}"

    return header


def split_text(text, limit):
    """Разбить длинный текст без грубого обрыва посреди абзаца."""
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
    """Собрать текст поста с метаданными и ссылкой на Telegram."""
    source_message = text_message or telegram_message
    lines = [f"Пост в Telegram: {get_post_url(source_message, channel_name)}"]
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
            logger.error(f"Ошибка webhook: {response.status}")
            if response.status in (400, 413):
                return SendResult.terminal_failure(error)
            return SendResult.retry(error)
        return SendResult.success()


def describe_network_error(error):
    error_text = str(error)
    lower_error = error_text.lower()
    if "name or service not known" in lower_error or "getaddrinfo failed" in lower_error:
        return (
            "DNS не смог найти discord.com. Проверь интернет/DNS на SteamOS. "
            f"Детали: {error_text}"
        )
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
                        "text": f"Из Telegram канала @{channel_name}{part_suffix}",
                    },
                    "author": {
                        "name": f"Новость из @{channel_name}",
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
    """Собрать последние limit постов после last_seen_id, не опираясь на плотность ID."""
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


async def get_album_message_ids(channel_name, message):
    if not message.grouped_id:
        return [message.id]

    start_id = max(1, message.id - 10)
    nearby_ids = list(range(start_id, message.id + 11))

    try:
        messages_batch = await telegram_client.get_messages(f"@{channel_name}", ids=nearby_ids)
        album_ids = sorted(
            album_message.id
            for album_message in messages_batch
            if album_message and album_message.grouped_id == message.grouped_id
        )
        if album_ids:
            return album_ids
    except Exception as e:
        logger.warning(f"Не удалось получить ID альбома @{channel_name}/{message.id}: {e}")

    return [message.id]


async def get_processed_until_id(channel_name, message):
    """Вернуть верхний Telegram message_id, который покрывает сообщение или альбом."""
    return max(await get_album_message_ids(channel_name, message))


async def catch_up_channels_on_start():
    """На старте догнать только небольшой свежий хвост, глубокий backlog пропустить."""
    if not telegram_client.is_connected():
        await telegram_client.connect()

    logger.info(
        f"Стартовая синхронизация: догоняем до {STARTUP_CATCH_UP_LIMIT} свежих постов на канал."
    )

    for channel in TELEGRAM_CHANNELS:
        try:
            entity = await telegram_client.get_entity(f"@{channel}")
            latest_message_id = await get_latest_message_id(entity)
            last_seen_id = get_last_seen_id(channel)

            if last_seen_id is None:
                set_last_seen_id(channel, latest_message_id)
                logger.info(
                    f"Канал @{channel}: создана граница ID {latest_message_id}."
                )
                continue

            if latest_message_id <= last_seen_id:
                logger.info(
                    f"Канал @{channel}: last_seen={last_seen_id}, latest={latest_message_id}. "
                    "Старого хвоста нет."
                )
                continue

            if STARTUP_CATCH_UP_LIMIT <= 0:
                skipped_count = latest_message_id - last_seen_id
                set_last_seen_id(channel, latest_message_id)
                logger.info(
                    f"Канал @{channel}: last_seen={last_seen_id}, latest={latest_message_id}. "
                    f"На старте пропущено старых ID: {skipped_count}."
                )
                continue

            to_process, has_older_forwardable = await collect_startup_tail(
                entity,
                last_seen_id,
                STARTUP_CATCH_UP_LIMIT,
            )

            if not to_process:
                advance_last_seen_id(channel, latest_message_id)
                logger.info(f"Канал @{channel}: в стартовом хвосте нет текстовых/медиа постов.")
                continue

            startup_boundary_id = last_seen_id
            if has_older_forwardable:
                startup_boundary_id = max(last_seen_id, min(message.id for message in to_process) - 1)
                set_last_seen_id(channel, startup_boundary_id)
                logger.info(
                    f"Канал @{channel}: last_seen={last_seen_id}, latest={latest_message_id}. "
                    f"Старый backlog пропущен до ID {startup_boundary_id}; "
                    f"догоняем последние {len(to_process)} постов."
                )

            logger.info(
                f"Канал @{channel}: стартовый хвост к обработке: {len(to_process)} "
                f"(ID > {startup_boundary_id}, latest={latest_message_id})."
            )
            sent_count, queued_count, skipped_processed, last_processed_id = (
                await process_messages_for_channel(channel, to_process, startup_boundary_id)
            )
            advance_last_seen_id(channel, max(last_processed_id, latest_message_id))
            logger.info(
                f"Канал @{channel}: стартовый хвост завершен, отправлено {sent_count}, "
                f"в очередь {queued_count}, уже обработано {skipped_processed}, "
                f"граница {max(last_processed_id, latest_message_id)}."
            )
        except Exception as e:
            logger.error(f"Канал @{channel}: ошибка стартовой синхронизации: {e}")


async def process_pending_messages():
    """Повторить отправку сообщений, которые ранее не дошли до Discord."""
    pending_messages = get_due_pending_messages()
    if not pending_messages:
        pending_count, due_count, next_retry_ts = get_pending_retry_status()
        if pending_count:
            retry_delay = 0
            retry_text = "неизвестно"
            if next_retry_ts is not None:
                retry_delay = max(0, int(float(next_retry_ts) - get_now_ts()))
                retry_text = format_ts(next_retry_ts)
            logger.info(
                f"Очередь: ожидают retry {pending_count}, "
                f"готовы сейчас {due_count}, ближайшая попытка через {retry_delay} сек ({retry_text})."
            )
        return 0

    if not telegram_client.is_connected():
        await telegram_client.connect()

    delivered_count = 0
    logger.info(f"Пробуем отправить сообщения из очереди: {len(pending_messages)}")

    for channel, message_id, grouped_id, attempts, last_error in pending_messages:
        try:
            entity = await telegram_client.get_entity(f"@{channel}")
            message = await telegram_client.get_messages(entity, ids=int(message_id))

            if not message or not (message.text or message.media):
                logger.warning(
                    f"Сообщение @{channel}/{message_id} больше недоступно. Удаляем из очереди."
                )
                delete_pending_message(channel, message_id)
                continue

            send_result = as_send_result(
                await send_to_discord(message, channel),
                "Повторная отправка вернула False",
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
                    f"Сообщение @{channel}/{message_id} доставлено из очереди. "
                    f"Граница канала сдвинута до {processed_until_id}."
                )
            else:
                error_text = send_result.error or "Повторная отправка вернула False"
                attempts_after = mark_pending_failed(channel, message_id, error_text)
                if send_result.terminal or attempts_after >= MAX_QUEUE_ATTEMPTS:
                    album_ids = await get_album_message_ids(channel, message)
                    reason = (
                        f"terminal error: {error_text}"
                        if send_result.terminal
                        else f"max attempts reached ({MAX_QUEUE_ATTEMPTS}): {error_text}"
                    )
                    drop_pending_message(channel, message_id, grouped_id or message.grouped_id, reason, attempts_after, album_ids)
                    await notify_dropped_message(channel, message_id, reason, attempts_after)
                else:
                    logger.warning(
                        f"Сообщение @{channel}/{message_id} осталось в очереди. "
                        f"Попыток было: {attempts_after}/{MAX_QUEUE_ATTEMPTS}. "
                        f"Причина: {error_text}"
                    )
        except Exception as e:
            error_text = describe_network_error(e)
            attempts_after = mark_pending_failed(channel, message_id, error_text)
            if attempts_after >= MAX_QUEUE_ATTEMPTS:
                reason = f"max attempts reached ({MAX_QUEUE_ATTEMPTS}): {error_text}"
                drop_pending_message(channel, message_id, grouped_id, reason, attempts_after)
                await notify_dropped_message(channel, message_id, reason, attempts_after)
            else:
                logger.warning(
                    f"Не удалось отправить @{channel}/{message_id} из очереди: {error_text}. "
                    f"Попыток было: {attempts_after}/{MAX_QUEUE_ATTEMPTS}."
                )

    remaining_count = get_pending_count()
    if delivered_count or remaining_count:
        logger.info(
            f"Очередь: доставлено {delivered_count}, осталось {remaining_count}."
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
            logger.info(f"Канал @{channel}: ID {message.id} уже обработан, пропускаем.")
            continue

        if message.grouped_id:
            if message.grouped_id in already_sent_grouped_ids:
                continue

            send_result = as_send_result(
                await send_to_discord(message, channel),
                "Первичная отправка альбома не удалась",
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
                    f"Канал @{channel}: альбом ID {message.id} обработан, "
                    f"граница {last_processed_id}."
                )
            elif send_result.terminal:
                reason = send_result.error or "Первичная отправка альбома завершилась terminal error"
                if album_ids:
                    last_processed_id = max(last_processed_id, max(album_ids))
                    advance_last_seen_id(channel, last_processed_id)
                    for album_message_id in album_ids:
                        mark_processed_message(channel, album_message_id, message.grouped_id, "failed")
                log_discarded_message(channel, message.id, reason, 1, "initial terminal error")
                await notify_dropped_message(channel, message.id, reason, 1)
                logger.error(
                    f"Канал @{channel}: альбом ID {message.id} не добавлен в очередь из-за terminal error: {reason}"
                )
            else:
                add_pending_message(
                    channel,
                    message.id,
                    message.grouped_id,
                    send_result.error or "Первичная отправка альбома не удалась",
                )
                if album_ids:
                    last_processed_id = max(last_processed_id, max(album_ids))
                    advance_last_seen_id(channel, last_processed_id)
                    for album_message_id in album_ids:
                        mark_processed_message(channel, album_message_id, message.grouped_id, "queued")
                queued_count += 1
                logger.warning(f"Канал @{channel}: альбом ID {message.id} добавлен в очередь.")
            continue

        send_result = as_send_result(
            await send_to_discord(message, channel),
            "Первичная отправка сообщения не удалась",
        )
        if send_result:
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            mark_processed_message(channel, message.id, None, "sent")
            total_sent += 1
            logger.info(
                f"Канал @{channel}: сообщение ID {message.id} обработано, "
                f"граница {last_processed_id}."
            )
        elif send_result.terminal:
            reason = send_result.error or "Первичная отправка сообщения завершилась terminal error"
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            mark_processed_message(channel, message.id, None, "failed")
            log_discarded_message(channel, message.id, reason, 1, "initial terminal error")
            await notify_dropped_message(channel, message.id, reason, 1)
            logger.error(
                f"Канал @{channel}: сообщение ID {message.id} не добавлено в очередь "
                f"из-за terminal error: {reason}"
            )
        else:
            add_pending_message(channel, message.id, None, send_result.error or "Первичная отправка сообщения не удалась")
            last_processed_id = max(last_processed_id, message.id)
            advance_last_seen_id(channel, last_processed_id)
            mark_processed_message(channel, message.id, None, "queued")
            queued_count += 1
            logger.warning(f"Канал @{channel}: сообщение ID {message.id} добавлено в очередь.")

    return total_sent, queued_count, skipped_processed, last_processed_id


# ===== ОСНОВНАЯ ФУНКЦИЯ: ПРОВЕРКА НОВОСТЕЙ =====

async def check_telegram_news():
    """Проверяет новые сообщения в каналах."""
    try:
        if not telegram_client.is_connected():
            await telegram_client.connect()

        total_sent_this_turn = 0

        for channel in TELEGRAM_CHANNELS:
            try:
                entity = await telegram_client.get_entity(f"@{channel}")
            except ValueError:
                logger.error(f"Канал @{channel} не найден!")
                continue
            except Exception as e:
                logger.error(f"Ошибка при получении канала @{channel}: {e}")
                continue

            last_seen_id = get_last_seen_id(channel)
            latest_message_id = await get_latest_message_id(entity)

            if last_seen_id is None:
                set_last_seen_id(channel, latest_message_id)
                logger.info(f"Канал @{channel}: зафиксирована стартовая граница ID {latest_message_id}.")
                continue

            logger.info(
                f"Канал @{channel}: проверка, last_seen={last_seen_id}, latest={latest_message_id}."
            )

            if latest_message_id <= last_seen_id:
                logger.info(f"Канал @{channel}: новых постов нет.")
                continue

            to_process = []
            async for message in telegram_client.iter_messages(entity, min_id=last_seen_id, reverse=True):
                if is_forwardable_message(message):
                    to_process.append(message)

            if not to_process:
                logger.info(f"Канал @{channel}: новых текстовых/медиа постов нет.")
                continue

            logger.info(f"Канал @{channel}: найдено кандидатов к обработке: {len(to_process)}.")

            sent_count, queued_this_channel, skipped_processed, last_processed_id = (
                await process_messages_for_channel(channel, to_process, last_seen_id)
            )
            total_sent_this_turn += sent_count

            if last_processed_id > last_seen_id:
                advance_last_seen_id(channel, last_processed_id)

            if queued_this_channel:
                logger.info(f"Канал @{channel}: добавлено в очередь {queued_this_channel}.")
            if skipped_processed:
                logger.info(f"Канал @{channel}: уже обработанных пропущено {skipped_processed}.")

        current_time = datetime.now().strftime("%H:%M:%S")
        if total_sent_this_turn > 0:
            logger.info(
                f"[{current_time}] Проверил все каналы. "
                f"Найдено и отправлено новых публикаций: {total_sent_this_turn}"
            )
        else:
            logger.info(f"[{current_time}] Проверил все каналы. Новых сообщений нет.")

    except Exception as e:
        logger.error(f"Ошибка при проверке Telegram: {e}")


# ===== ОТПРАВКА В DISCORD =====

async def send_to_discord(telegram_message, channel_name):
    """Отправляет сообщение и медиа из Telegram в Discord через webhook."""
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
                start_id = max(1, telegram_message.id - 10)
                nearby_ids = list(range(start_id, telegram_message.id + 11))
                messages_batch = await telegram_client.get_messages(f"@{channel_name}", ids=nearby_ids)
                album_messages = [
                    msg for msg in messages_batch
                    if msg and msg.grouped_id == telegram_message.grouped_id
                ]
                album_messages.sort(key=lambda m: m.id)
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
                                logger.warning("Telegram не вернул путь к медиа из альбома.")
                        except Exception as e:
                            media_download_failed = True
                            logger.warning(f"Не удалось скачать медиа из альбома: {e}")
                if not media_files:
                    media_download_failed = True
            except Exception as e:
                media_download_failed = True
                logger.error(f"Ошибка при обработке альбома: {e}")

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
                    logger.warning("Telegram не вернул путь к медиа.")
            except Exception as e:
                media_download_failed = True
                logger.error(f"Ошибка при скачивании медиа: {e}")

        if media_download_failed:
            logger.warning("Медиа скачалось не полностью. Сообщение будет повторено через очередь.")
            return SendResult.retry("Telegram media download failed")

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
                                "text": f"Из Telegram канала @{channel_name}",
                            },
                            "author": {
                                "name": f"Медиа из @{channel_name}",
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
                                f"Файл {file_path} больше {DISCORD_FILE_LIMIT_MB} MB, "
                                "пробуем отправить как есть."
                            )
                        else:
                            logger.warning(
                                f"Файл {file_path} больше {DISCORD_FILE_LIMIT_MB} MB, "
                                "не прикрепляем."
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
                            "Все медиа больше лимита. Отправляем текст поста со ссылкой на Telegram."
                        )
                        if text_chunks:
                            return await send_text_chunks(session, text_chunks, channel_name, message_time)
                        return SendResult.success()

                    if skipped_large_files and LARGE_FILE_ACTION == "skip_post":
                        logger.warning("Все медиа больше лимита. Пропускаем пост и двигаемся дальше.")
                        return SendResult.success()

                    logger.warning("Нет файлов, которые можно отправить в Discord.")
                    return SendResult.retry("No valid media files to send")

                try:
                    timeout = aiohttp.ClientTimeout(total=60)
                    async with session.post(DISCORD_WEBHOOK_URL, data=form_data, timeout=timeout) as response:
                        if response.status not in [200, 204]:
                            error = f"Discord webhook media upload error {response.status}"
                            logger.error(f"Ошибка webhook при отправке медиа: {response.status}")
                            if response.status == 413:
                                if LARGE_FILE_ACTION == "skip_post":
                                    logger.warning(
                                        "Discord вернул 413 Payload Too Large. "
                                        "По настройке skip_post пропускаем пост и двигаемся дальше."
                                    )
                                    return SendResult.success()
                                logger.warning(
                                    "Discord вернул 413 Payload Too Large. "
                                    f"Текущий лимит в конфиге: {DISCORD_FILE_LIMIT_MB} MB. "
                                    "Отправляем текст поста со ссылкой на Telegram."
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
                    logger.error(f"Ошибка при отправке данных в Discord: {error}")
                    return SendResult.retry(error)

            return SendResult.retry("Nothing to send")

    except Exception as e:
        error = describe_network_error(e)
        logger.error(f"Ошибка при отправке сообщения в Discord: {error}")
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


# ===== ОСНОВНОЙ ЦИКЛ =====

async def main():
    logger.info("Стартуем форвард новостей из Telegram в Discord")
    logger.info(f"Каналы Telegram: {', '.join(TELEGRAM_CHANNELS)}")
    logger.info(f"Проверка каждые {CHECK_INTERVAL} секунд")

    logger.info("Подключение к серверам Telegram...")

    try:
        if os.path.exists("tg_session.session"):
            logger.info("Найдена сохраненная сессия, используем ее...")
            try:
                await telegram_client.connect()
                if await telegram_client.is_user_authorized():
                    logger.info("Авторизация успешна через сохраненную сессию!")
                else:
                    logger.info("Сессия невалидна, требуется повторная авторизация.")
                    await telegram_client.start()
            except Exception as e:
                logger.error(f"Ошибка при использовании сессии: {e}")
                try:
                    os.remove("tg_session.session")
                except OSError:
                    pass
                await telegram_client.start()
        else:
            logger.info("Нет сохраненной сессии, требуется первая авторизация.")
            await telegram_client.start()

        init_state_db()
        migrate_legacy_seen_messages()

        await catch_up_channels_on_start()
        logger.info("Стартовая синхронизация завершена. Отслеживаем новые посты.")

        logger.info("Форвард запущен! Нажми Ctrl+C для остановки.")
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
                    logger.error(f"Ошибка в основном цикле: {e}")
                    await sleep_with_config_reload(CHECK_INTERVAL)
    except Exception as e:
        logger.error(f"Ошибка при запуске Telegram клиента: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Форвард остановлен пользователем.")
