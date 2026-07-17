import os


APP_NAME = "Telerixa"
VERSION = "0.4.0"

LOG_DIR = "logs"
BOT_LOG_FILE = os.path.join(LOG_DIR, "bot.log")
BOT_PID_FILE = os.path.join(LOG_DIR, "telerixa.pid")
UI_PID_FILE = os.path.join(LOG_DIR, "web_ui.pid")

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
CHANNEL_FETCH_CONCURRENCY = 8

RUNTIME_SERVICE_NAME = "forwarder"
RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 10
RUNTIME_STALE_AFTER_SECONDS = 35
