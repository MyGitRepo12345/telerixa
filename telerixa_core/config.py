from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from i18n import normalize_language
from .constants import (
    CONFIG_RELOAD_KEYS,
    VALID_LARGE_FILE_ACTIONS,
    VALID_VIDEO_TRANSCODE_PRESETS,
)


DEFAULT_DB_TIMEOUT_SECONDS = 30


class ConfigError(Exception):
    def __init__(self, kind, path, detail=None):
        super().__init__(detail or kind)
        self.kind = kind
        self.path = str(path)
        self.detail = detail


@dataclass(frozen=True)
class ConfigWarning:
    kind: str
    value: Any


@dataclass(frozen=True)
class RuntimeConfig:
    language: str
    discord_webhook_url: str
    discord_alert_user_id: str
    telegram_api_id: int
    telegram_api_hash: str
    telegram_channels: Tuple[str, ...]
    check_interval: int
    max_message_length: int
    timezone_name: str
    app_timezone: tzinfo
    discord_file_limit_mb: int
    large_file_action: str
    video_transcode_preset: str
    video_transcode_timeout_seconds: int
    state_db_file: str
    queue_retry_limit: int
    startup_catch_up_limit: int
    max_queue_attempts: int
    db_timeout_seconds: int = DEFAULT_DB_TIMEOUT_SECONDS

    @property
    def db_busy_timeout_ms(self):
        return self.db_timeout_seconds * 1000


@dataclass(frozen=True)
class ConfigUpdate:
    config: RuntimeConfig
    changed_keys: Tuple[str, ...]
    warnings: Tuple[ConfigWarning, ...]


FIELD_BY_CONFIG_KEY = {
    "LANGUAGE": "language",
    "DISCORD_WEBHOOK_URL": "discord_webhook_url",
    "DISCORD_ALERT_USER_ID": "discord_alert_user_id",
    "TELEGRAM_CHANNELS": "telegram_channels",
    "CHECK_INTERVAL": "check_interval",
    "MAX_MESSAGE_LENGTH": "max_message_length",
    "TIMEZONE": "timezone_name",
    "DISCORD_FILE_LIMIT_MB": "discord_file_limit_mb",
    "LARGE_FILE_ACTION": "large_file_action",
    "VIDEO_TRANSCODE_PRESET": "video_transcode_preset",
    "VIDEO_TRANSCODE_TIMEOUT_SECONDS": "video_transcode_timeout_seconds",
    "STARTUP_CATCH_UP_LIMIT": "startup_catch_up_limit",
    "MAX_QUEUE_ATTEMPTS": "max_queue_attempts",
}


def _normalize_int(value, fallback, minimum):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _normalize_channels(value, fallback):
    if not isinstance(value, list):
        return fallback

    channels = []
    seen = set()
    for channel in value:
        normalized = str(channel).strip().lstrip("@")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        channels.append(normalized)
    return tuple(channels)


def _load_timezone(timezone_name) -> Tuple[tzinfo, Optional[ConfigWarning]]:
    try:
        return ZoneInfo(timezone_name), None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        fallback_timezone = datetime.now().astimezone().tzinfo or timezone.utc
        return fallback_timezone, ConfigWarning("timezone_not_found", timezone_name)


def build_runtime_config(raw_config, previous=None):
    if not isinstance(raw_config, Mapping):
        raise ConfigError("invalid_root", "config.json")

    warnings = []
    previous_channels = previous.telegram_channels if previous else ()
    default_startup_limit = (
        previous.startup_catch_up_limit
        if previous
        else (0 if raw_config.get("SKIP_BACKLOG_ON_START", False) else 10)
    )

    language = normalize_language(
        raw_config.get("LANGUAGE", previous.language if previous else "ru")
    )
    discord_webhook_url = str(
        raw_config.get(
            "DISCORD_WEBHOOK_URL",
            previous.discord_webhook_url if previous else "",
        )
    ).strip()
    discord_alert_user_id = str(
        raw_config.get(
            "DISCORD_ALERT_USER_ID",
            previous.discord_alert_user_id if previous else "",
        )
    ).strip()
    telegram_channels = _normalize_channels(
        raw_config.get("TELEGRAM_CHANNELS", previous_channels),
        previous_channels,
    )
    check_interval = _normalize_int(
        raw_config.get(
            "CHECK_INTERVAL",
            previous.check_interval if previous else 60,
        ),
        previous.check_interval if previous else 60,
        5,
    )
    max_message_length = _normalize_int(
        raw_config.get(
            "MAX_MESSAGE_LENGTH",
            previous.max_message_length if previous else 2000,
        ),
        previous.max_message_length if previous else 2000,
        1,
    )
    timezone_name = str(
        raw_config.get(
            "TIMEZONE",
            previous.timezone_name if previous else "Europe/Berlin",
        )
    ).strip() or (previous.timezone_name if previous else "Europe/Berlin")
    discord_file_limit_mb = _normalize_int(
        raw_config.get(
            "DISCORD_FILE_LIMIT_MB",
            previous.discord_file_limit_mb if previous else 25,
        ),
        previous.discord_file_limit_mb if previous else 25,
        1,
    )
    large_file_action = raw_config.get(
        "LARGE_FILE_ACTION",
        previous.large_file_action if previous else "send_text_link",
    )
    if large_file_action not in VALID_LARGE_FILE_ACTIONS:
        warnings.append(ConfigWarning("invalid_large_file_action", large_file_action))
        large_file_action = "send_text_link"

    video_transcode_preset = str(
        raw_config.get(
            "VIDEO_TRANSCODE_PRESET",
            previous.video_transcode_preset if previous else "balanced",
        )
    ).strip().lower()
    if video_transcode_preset not in VALID_VIDEO_TRANSCODE_PRESETS:
        warnings.append(
            ConfigWarning("invalid_video_transcode_preset", video_transcode_preset)
        )
        video_transcode_preset = "balanced"
    video_transcode_timeout_seconds = _normalize_int(
        raw_config.get(
            "VIDEO_TRANSCODE_TIMEOUT_SECONDS",
            previous.video_transcode_timeout_seconds if previous else 600,
        ),
        previous.video_transcode_timeout_seconds if previous else 600,
        30,
    )
    video_transcode_timeout_seconds = min(
        7200,
        video_transcode_timeout_seconds,
    )

    startup_catch_up_limit = _normalize_int(
        raw_config.get("STARTUP_CATCH_UP_LIMIT", default_startup_limit),
        default_startup_limit,
        0,
    )
    max_queue_attempts = _normalize_int(
        raw_config.get(
            "MAX_QUEUE_ATTEMPTS",
            previous.max_queue_attempts if previous else 24,
        ),
        previous.max_queue_attempts if previous else 24,
        1,
    )

    if previous:
        telegram_api_id = previous.telegram_api_id
        telegram_api_hash = previous.telegram_api_hash
        state_db_file = previous.state_db_file
        queue_retry_limit = previous.queue_retry_limit
        db_timeout_seconds = previous.db_timeout_seconds
    else:
        telegram_api_id = _normalize_int(raw_config.get("TELEGRAM_API_ID", 0), 0, 0)
        telegram_api_hash = str(raw_config.get("TELEGRAM_API_HASH", "")).strip()
        state_db_file = str(raw_config.get("STATE_DB_FILE", "bot_state.db"))
        queue_retry_limit = _normalize_int(raw_config.get("QUEUE_RETRY_LIMIT", 20), 20, 1)
        db_timeout_seconds = DEFAULT_DB_TIMEOUT_SECONDS

    if previous and timezone_name == previous.timezone_name:
        app_timezone = previous.app_timezone
    else:
        app_timezone, timezone_warning = _load_timezone(timezone_name)
        if timezone_warning:
            warnings.append(timezone_warning)

    if not telegram_channels or not discord_webhook_url:
        raise ConfigError("required_missing", "config.json")

    return RuntimeConfig(
        language=language,
        discord_webhook_url=discord_webhook_url,
        discord_alert_user_id=discord_alert_user_id,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_channels=telegram_channels,
        check_interval=check_interval,
        max_message_length=max_message_length,
        timezone_name=timezone_name,
        app_timezone=app_timezone,
        discord_file_limit_mb=discord_file_limit_mb,
        large_file_action=large_file_action,
        video_transcode_preset=video_transcode_preset,
        video_transcode_timeout_seconds=video_transcode_timeout_seconds,
        state_db_file=state_db_file,
        queue_retry_limit=queue_retry_limit,
        startup_catch_up_limit=startup_catch_up_limit,
        max_queue_attempts=max_queue_attempts,
        db_timeout_seconds=db_timeout_seconds,
    ), tuple(warnings)


def get_changed_keys(previous, current):
    return tuple(
        key
        for key in CONFIG_RELOAD_KEYS
        if getattr(previous, FIELD_BY_CONFIG_KEY[key])
        != getattr(current, FIELD_BY_CONFIG_KEY[key])
    )


class ConfigManager:
    def __init__(self, config_path):
        self.path = Path(config_path)
        self.current: Optional[RuntimeConfig] = None
        self._mtime_ns = None

    def _read_mapping(self):
        try:
            with self.path.open("r", encoding="utf-8") as config_file:
                data = json.load(config_file)
        except FileNotFoundError as error:
            raise ConfigError("file_missing", self.path, str(error)) from error
        except json.JSONDecodeError as error:
            raise ConfigError("invalid_json", self.path, str(error)) from error
        except OSError as error:
            raise ConfigError("read_failed", self.path, str(error)) from error

        if not isinstance(data, dict):
            raise ConfigError("invalid_root", self.path)
        return data

    def _get_mtime_ns(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError as error:
            raise ConfigError("check_failed", self.path, str(error)) from error

    def load_initial(self):
        raw_config = self._read_mapping()
        try:
            config, warnings = build_runtime_config(raw_config)
        except ConfigError as error:
            error.path = str(self.path)
            raise

        self.current = config
        self._mtime_ns = self._get_mtime_ns()
        return ConfigUpdate(config, (), warnings)

    def reload_if_changed(self):
        if self.current is None:
            return self.load_initial()

        current_mtime_ns = self._get_mtime_ns()
        if current_mtime_ns == self._mtime_ns:
            return None

        raw_config = self._read_mapping()
        try:
            new_config, warnings = build_runtime_config(
                raw_config,
                previous=self.current,
            )
        except ConfigError as error:
            error.path = str(self.path)
            raise

        changed_keys = get_changed_keys(self.current, new_config)
        self.current = new_config
        self._mtime_ns = current_mtime_ns
        return ConfigUpdate(new_config, changed_keys, warnings)
