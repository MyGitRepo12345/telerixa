import json
import os
from pathlib import Path
import tempfile
import unittest

from telerixa_core.config import (
    ConfigError,
    ConfigManager,
    build_runtime_config,
)


def valid_config(**overrides):
    config = {
        "DISCORD_WEBHOOK_URL": "https://example.invalid/webhook",
        "TELEGRAM_API_ID": 123,
        "TELEGRAM_API_HASH": "hash",
        "TELEGRAM_CHANNELS": ["channel_one"],
        "LANGUAGE": "en",
        "TIMEZONE": "UTC",
    }
    config.update(overrides)
    return config


class RuntimeConfigTests(unittest.TestCase):
    def test_initial_config_is_normalized(self):
        config, warnings = build_runtime_config(
            valid_config(
                TELEGRAM_API_ID="456",
                TELEGRAM_CHANNELS=["@alpha", " alpha ", "", "beta"],
                CHECK_INTERVAL=1,
                MAX_MESSAGE_LENGTH=0,
                DISCORD_FILE_LIMIT_MB="50",
                LARGE_FILE_ACTION="invalid-action",
                VIDEO_TRANSCODE_PRESET="impossible",
                VIDEO_TRANSCODE_TIMEOUT_SECONDS=1,
                STARTUP_CATCH_UP_LIMIT=-5,
                MAX_QUEUE_ATTEMPTS=0,
            )
        )

        self.assertEqual(config.telegram_api_id, 456)
        self.assertEqual(config.telegram_channels, ("alpha", "beta"))
        self.assertEqual(config.check_interval, 5)
        self.assertEqual(config.max_message_length, 1)
        self.assertEqual(config.discord_file_limit_mb, 50)
        self.assertEqual(config.large_file_action, "send_text_link")
        self.assertEqual(config.video_transcode_preset, "balanced")
        self.assertEqual(config.video_transcode_timeout_seconds, 30)
        self.assertEqual(config.startup_catch_up_limit, 0)
        self.assertEqual(config.max_queue_attempts, 1)
        self.assertIn(
            ("invalid_large_file_action", "invalid-action"),
            [(warning.kind, warning.value) for warning in warnings],
        )
        self.assertIn(
            ("invalid_video_transcode_preset", "impossible"),
            [(warning.kind, warning.value) for warning in warnings],
        )

    def test_required_delivery_settings_are_validated(self):
        with self.assertRaisesRegex(ConfigError, "required_missing"):
            build_runtime_config(valid_config(TELEGRAM_CHANNELS=[]))


class ConfigManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config_path = Path(self.temp_dir.name) / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, config):
        previous_mtime_ns = (
            self.config_path.stat().st_mtime_ns
            if self.config_path.exists()
            else 0
        )
        self.config_path.write_text(
            json.dumps(config),
            encoding="utf-8",
        )
        current_mtime_ns = self.config_path.stat().st_mtime_ns
        forced_mtime_ns = max(current_mtime_ns, previous_mtime_ns + 1_000_000)
        os.utime(
            self.config_path,
            ns=(forced_mtime_ns, forced_mtime_ns),
        )

    def test_reload_atomically_replaces_only_reloadable_settings(self):
        self.write_config(
            valid_config(
                CHECK_INTERVAL=30,
                VIDEO_TRANSCODE_PRESET="fast",
                VIDEO_TRANSCODE_TIMEOUT_SECONDS=300,
                STATE_DB_FILE="first.db",
                QUEUE_RETRY_LIMIT=12,
            )
        )
        manager = ConfigManager(self.config_path)
        initial = manager.load_initial().config

        self.write_config(
            valid_config(
                CHECK_INTERVAL=45,
                TELEGRAM_CHANNELS=["new_channel"],
                VIDEO_TRANSCODE_PRESET="quality",
                VIDEO_TRANSCODE_TIMEOUT_SECONDS=900,
                TELEGRAM_API_ID=999,
                TELEGRAM_API_HASH="new-hash",
                STATE_DB_FILE="second.db",
                QUEUE_RETRY_LIMIT=99,
            )
        )
        update = manager.reload_if_changed()
        assert update is not None

        self.assertIsNot(update.config, initial)
        self.assertIs(manager.current, update.config)
        self.assertEqual(update.config.check_interval, 45)
        self.assertEqual(update.config.telegram_channels, ("new_channel",))
        self.assertEqual(
            update.changed_keys,
            (
                "TELEGRAM_CHANNELS",
                "CHECK_INTERVAL",
                "VIDEO_TRANSCODE_PRESET",
                "VIDEO_TRANSCODE_TIMEOUT_SECONDS",
            ),
        )
        self.assertEqual(update.config.video_transcode_preset, "quality")
        self.assertEqual(update.config.video_transcode_timeout_seconds, 900)
        self.assertEqual(update.config.telegram_api_id, initial.telegram_api_id)
        self.assertEqual(update.config.telegram_api_hash, initial.telegram_api_hash)
        self.assertEqual(update.config.state_db_file, "first.db")
        self.assertEqual(update.config.queue_retry_limit, 12)

    def test_invalid_reload_keeps_previous_snapshot(self):
        self.write_config(valid_config(CHECK_INTERVAL=30))
        manager = ConfigManager(self.config_path)
        initial = manager.load_initial().config

        previous_mtime_ns = self.config_path.stat().st_mtime_ns
        self.config_path.write_text("{invalid", encoding="utf-8")
        forced_mtime_ns = previous_mtime_ns + 1_000_000
        os.utime(
            self.config_path,
            ns=(forced_mtime_ns, forced_mtime_ns),
        )

        with self.assertRaisesRegex(ConfigError, "Expecting"):
            manager.reload_if_changed()

        self.assertIs(manager.current, initial)

    def test_unchanged_file_does_not_create_new_snapshot(self):
        self.write_config(valid_config())
        manager = ConfigManager(self.config_path)
        initial = manager.load_initial().config

        self.assertIsNone(manager.reload_if_changed())
        self.assertIs(manager.current, initial)


if __name__ == "__main__":
    unittest.main()
