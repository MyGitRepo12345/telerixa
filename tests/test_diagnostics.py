import io
import json
import sqlite3
import tempfile
import unittest
from collections import namedtuple
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from telerixa_core import diagnostics
from telerixa_core import state


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload if payload is not None else {"id": "123"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload).encode("utf-8")


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.temp_dir.name)
        self.db_file = self.base_dir / "state.db"
        self.config = {
            "STATE_DB_FILE": str(self.db_file),
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret",
            "TELEGRAM_API_ID": 12345,
            "TELEGRAM_API_HASH": "telegram-hash",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sqlite_check_validates_integrity_and_schema(self):
        missing = diagnostics.check_sqlite(self.config)
        self.assertEqual((missing["status"], missing["code"]), ("error", "database_missing"))

        state.init_state_db(self.db_file, 30, 30000, 1000.0)
        result = diagnostics.check_sqlite(self.config)

        self.assertEqual((result["status"], result["code"]), ("success", "database_ok"))
        self.assertIn("size", result["details"])

    def test_discord_check_uses_safe_get_and_hides_webhook(self):
        calls = []

        def open_url(request, timeout):
            calls.append((request, timeout))
            return FakeResponse()

        result = diagnostics.check_discord(self.config, open_url=open_url)

        self.assertEqual((result["status"], result["code"]), ("success", "discord_ok"))
        self.assertEqual(calls[0][0].get_method(), "GET")
        self.assertEqual(calls[0][1], 5)
        self.assertNotIn("secret", json.dumps(result))

    def test_discord_check_classifies_http_and_network_failures(self):
        def rejected(_request, timeout):
            raise HTTPError("url", 404, "missing", Message(), io.BytesIO())

        def offline(_request, timeout):
            raise URLError("DNS unavailable")

        rejected_result = diagnostics.check_discord(self.config, open_url=rejected)
        offline_result = diagnostics.check_discord(self.config, open_url=offline)

        self.assertEqual((rejected_result["status"], rejected_result["code"]), ("error", "discord_rejected"))
        self.assertEqual((offline_result["status"], offline_result["code"]), ("warning", "discord_network_error"))

    def test_telegram_check_reads_session_without_modifying_it(self):
        session_file = self.base_dir / "tg_session.session"
        with closing(sqlite3.connect(session_file)) as connection:
            connection.execute("CREATE TABLE sessions (dc_id INTEGER)")
            connection.execute("INSERT INTO sessions (dc_id) VALUES (4)")
            connection.commit()
        before = session_file.stat().st_mtime_ns

        result = diagnostics.check_telegram(self.config, self.base_dir)

        self.assertEqual((result["status"], result["code"]), ("success", "telegram_session_ok"))
        self.assertEqual(result["details"]["dc_id"], 4)
        self.assertEqual(session_file.stat().st_mtime_ns, before)

    def test_telegram_check_reports_configuration_and_session_problems(self):
        invalid_config = dict(self.config, TELEGRAM_API_ID=0)
        not_configured = diagnostics.check_telegram(invalid_config, self.base_dir)
        missing_session = diagnostics.check_telegram(self.config, self.base_dir)

        self.assertEqual(not_configured["code"], "telegram_not_configured")
        self.assertEqual(missing_session["code"], "telegram_session_missing")

    def test_disk_check_uses_free_space_thresholds(self):
        DiskUsage = namedtuple("DiskUsage", "total used free")
        with patch.object(
            diagnostics.shutil,
            "disk_usage",
            return_value=DiskUsage(100 * 1024**3, 99.8 * 1024**3, 200 * 1024**2),
        ):
            critical = diagnostics.check_disk(self.base_dir)
        with patch.object(
            diagnostics.shutil,
            "disk_usage",
            return_value=DiskUsage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3),
        ):
            healthy = diagnostics.check_disk(self.base_dir)

        self.assertEqual((critical["status"], critical["code"]), ("error", "disk_critical"))
        self.assertEqual((healthy["status"], healthy["code"]), ("success", "disk_ok"))

    def test_ffmpeg_check_reports_optional_missing_and_version(self):
        with patch.object(diagnostics.shutil, "which", return_value=None):
            missing = diagnostics.check_ffmpeg()

        completed = diagnostics.subprocess.CompletedProcess(
            ["ffmpeg", "-version"],
            0,
            stdout="ffmpeg version 7.1 test\n",
            stderr="",
        )
        with (
            patch.object(diagnostics.shutil, "which", return_value="ffmpeg"),
            patch.object(diagnostics.subprocess, "run", return_value=completed),
        ):
            available = diagnostics.check_ffmpeg()

        self.assertEqual((missing["status"], missing["code"]), ("warning", "ffmpeg_missing"))
        self.assertEqual((available["status"], available["code"]), ("success", "ffmpeg_ok"))
        self.assertIn("7.1", available["details"]["version"])

    def test_run_diagnostics_isolates_unexpected_checker_failure(self):
        state.init_state_db(self.db_file, 30, 30000, 1000.0)
        with (
            patch.object(diagnostics, "check_sqlite", side_effect=RuntimeError("boom")),
            patch.object(diagnostics, "check_discord", return_value=diagnostics.diagnostic_result("discord", "success", "discord_ok")),
            patch.object(diagnostics, "check_telegram", return_value=diagnostics.diagnostic_result("telegram", "success", "telegram_session_ok", dc_id=4)),
            patch.object(diagnostics, "check_disk", return_value=diagnostics.diagnostic_result("disk", "success", "disk_ok")),
            patch.object(diagnostics, "check_ffmpeg", return_value=diagnostics.diagnostic_result("ffmpeg", "warning", "ffmpeg_missing")),
        ):
            results = diagnostics.run_diagnostics(self.config, self.base_dir)

        self.assertEqual(len(results), 5)
        self.assertEqual(results[0]["code"], "internal_error")
        self.assertEqual([result["component"] for result in results], ["sqlite", "discord", "telegram", "disk", "ffmpeg"])


if __name__ == "__main__":
    unittest.main()
