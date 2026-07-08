import os


APP_NAME = "Telerixa"
VERSION = "0.2.5"

LOG_DIR = "logs"
BOT_LOG_FILE = os.path.join(LOG_DIR, "bot.log")

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
