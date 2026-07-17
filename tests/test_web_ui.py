import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from i18n import configure_language
import web_ui
from telerixa_core import state


class WebUiDatabaseTests(unittest.TestCase):
    def test_ui_connection_closes_after_context_manager(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            connection = web_ui.connect_db(Path(temp_dir) / "ui-state.db")

            with connection as active_connection:
                active_connection.execute("CREATE TABLE sample (id INTEGER)")

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


class WebUiDashboardTests(unittest.TestCase):
    def setUp(self):
        configure_language("en")
        web_ui.LAST_DIAGNOSTICS["ran_at"] = ""
        web_ui.LAST_DIAGNOSTICS["results"] = []
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_file = Path(self.temp_dir.name) / "bot-state.db"
        self.config = {
            "STATE_DB_FILE": str(self.db_file),
            "TELEGRAM_CHANNELS": ["alpha_news", "beta_news"],
        }
        self.now_ts = time.time()
        self.now_text = datetime.fromtimestamp(self.now_ts).astimezone().isoformat()
        state.init_state_db(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            self.now_ts,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dashboard_snapshot_reports_queue_failures_and_channel_state(self):
        state.mark_runtime_started(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "forwarder",
            9876,
            self.now_ts,
            self.now_text,
        )
        state.mark_runtime_cycle_finished(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "forwarder",
            "error",
            "Discord unavailable",
            self.now_ts,
            self.now_text,
        )
        state.set_last_seen_id(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            120,
            self.now_text,
        )
        state.mark_processed_message(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            118,
            None,
            "sent",
            self.now_ts - 10,
            self.now_text,
        )
        state.archive_pending_failure(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            119,
            None,
            [119],
            "Payload rejected",
            "terminal",
            "initial delivery",
            1,
            self.now_ts,
            self.now_text,
        )
        state.add_pending_message(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "beta_news",
            42,
            None,
            "network unavailable",
            self.now_ts - 60,
            self.now_text,
            self.now_ts - 120,
        )

        snapshot = web_ui.get_dashboard_snapshot(self.config)

        self.assertEqual(snapshot["database_state"], "ready")
        self.assertEqual(snapshot["configured_channel_count"], 2)
        self.assertEqual(snapshot["pending_count"], 1)
        self.assertEqual(snapshot["due_count"], 1)
        self.assertEqual(snapshot["sent_count"], 1)
        self.assertEqual(snapshot["failed_count"], 1)
        self.assertEqual(snapshot["runtime_state"], "running")
        self.assertEqual(snapshot["runtime"]["pid"], 9876)
        self.assertEqual(snapshot["channels"][0]["last_seen_id"], 120)
        self.assertEqual(snapshot["channels"][0]["sent"], 1)
        self.assertEqual(snapshot["channels"][0]["failed"], 1)
        self.assertEqual(snapshot["failed_items"][0]["message_id"], 119)
        overview_html = web_ui.render_overview(snapshot)
        self.assertIn('action="/retry-now"', overview_html)
        self.assertIn('action="/failed/requeue"', overview_html)
        self.assertIn('action="/failed/dismiss"', overview_html)
        self.assertIn("Discord unavailable", overview_html)
        self.assertIn("Payload rejected", overview_html)
        self.assertIn("Waiting for the bot to retry this post.", overview_html)
        self.assertIn(
            'data-pending-label="Retry requested" disabled>Retry requested</button>',
            overview_html,
        )

    def test_future_retry_keeps_retry_now_action_enabled(self):
        future_ts = time.time() + 3600
        queue_html = web_ui.render_queue_panel(
            1,
            [
                (
                    "alpha_news",
                    321,
                    None,
                    2,
                    datetime.fromtimestamp(future_ts).astimezone().isoformat(),
                    future_ts,
                    "temporary failure",
                )
            ],
        )

        self.assertIn(
            'data-pending-label="Retry requested">Retry now</button>',
            queue_html,
        )
        self.assertNotIn("Waiting for the bot to retry this post.", queue_html)

    def test_runtime_state_uses_heartbeat_freshness(self):
        runtime = {
            "status": "running",
            "heartbeat_ts": 100.0,
        }

        self.assertEqual(
            web_ui.classify_runtime_state(
                runtime,
                now_ts=135.0,
                stale_after=35,
            ),
            "running",
        )
        self.assertEqual(
            web_ui.classify_runtime_state(
                runtime,
                now_ts=135.1,
                stale_after=35,
            ),
            "stale",
        )
        self.assertEqual(
            web_ui.classify_runtime_state(
                {"status": "stopped", "heartbeat_ts": 135.0},
                now_ts=200.0,
                stale_after=35,
            ),
            "stopped",
        )
        self.assertEqual(
            web_ui.classify_runtime_state(None, now_ts=200.0),
            "unknown",
        )

    def test_dashboard_deduplicates_failed_album_rows(self):
        state.archive_pending_failure(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            201,
            9001,
            [201, 202],
            "Album rejected",
            "terminal",
            "initial delivery",
            1,
            self.now_ts,
            self.now_text,
        )

        snapshot = web_ui.get_dashboard_snapshot(self.config)

        self.assertEqual(snapshot["failed_count"], 1)
        self.assertEqual(len(snapshot["failed_items"]), 1)
        self.assertEqual(snapshot["failed_items"][0]["message_id"], 201)
        self.assertEqual(snapshot["failed_items"][0]["grouped_id"], 9001)

    def test_dashboard_snapshot_handles_missing_database(self):
        missing_config = {
            "STATE_DB_FILE": str(Path(self.temp_dir.name) / "missing.db"),
            "TELEGRAM_CHANNELS": ["alpha_news"],
        }

        snapshot = web_ui.get_dashboard_snapshot(missing_config)

        self.assertEqual(snapshot["database_state"], "missing")
        self.assertIsNone(snapshot["pending_count"])
        self.assertEqual(snapshot["channels"][0]["channel"], "alpha_news")

    def test_retry_pending_now_preserves_attempts_and_delivery_progress(self):
        state.add_pending_message(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            130,
            None,
            "temporary failure",
            self.now_ts,
            self.now_text,
            self.now_ts,
        )
        with web_ui.connect_db(self.db_file) as conn:
            conn.execute(
                """
                UPDATE pending_messages
                SET attempts = 4,
                    next_chunk_index = 2,
                    media_sent = 1,
                    next_retry_ts = ?
                WHERE channel = ? AND message_id = ?
                """,
                (self.now_ts + 3600, "alpha_news", 130),
            )
            conn.commit()

        started_at = time.time()
        updated = web_ui.retry_pending_now(self.config, "alpha_news", 130)

        self.assertTrue(updated)
        with web_ui.connect_db(self.db_file) as conn:
            row = conn.execute(
                """
                SELECT attempts, next_chunk_index, media_sent, next_retry_ts
                FROM pending_messages
                WHERE channel = ? AND message_id = ?
                """,
                ("alpha_news", 130),
            ).fetchone()
        self.assertEqual(row[:3], (4, 2, 1))
        self.assertGreaterEqual(row[3], started_at)
        self.assertLessEqual(row[3], time.time())

    def test_archive_actions_requeue_and_dismiss_without_deleting_history(self):
        requeue_id = state.archive_pending_failure(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            140,
            None,
            [140],
            "Retry me",
            "terminal",
            "retry queue",
            2,
            self.now_ts,
            self.now_text,
        )
        dismiss_id = state.archive_pending_failure(
            self.db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "beta_news",
            141,
            None,
            [141],
            "Dismiss me",
            "unavailable",
            "retry queue",
            1,
            self.now_ts,
            self.now_text,
        )

        self.assertEqual(
            web_ui.requeue_archived_failure(self.config, requeue_id),
            "requeued",
        )
        self.assertTrue(
            web_ui.dismiss_archived_failure(self.config, dismiss_id)
        )

        with web_ui.connect_db(self.db_file) as conn:
            statuses = dict(
                conn.execute(
                    "SELECT id, status FROM failed_deliveries ORDER BY id"
                ).fetchall()
            )
            pending = conn.execute(
                """
                SELECT attempts
                FROM pending_messages
                WHERE channel = 'alpha_news' AND message_id = 140
                """
            ).fetchone()
        self.assertEqual(statuses[requeue_id], "requeued")
        self.assertEqual(statuses[dismiss_id], "dismissed")
        self.assertEqual(pending, (0,))


class WebUiRenderingTests(unittest.TestCase):
    def setUp(self):
        web_ui.LAST_DIAGNOSTICS["ran_at"] = ""
        web_ui.LAST_DIAGNOSTICS["results"] = []
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config = web_ui.default_config()
        self.config["LANGUAGE"] = "en"
        self.config["TELEGRAM_CHANNELS"] = ["alpha_news"]
        self.config["DISCORD_WEBHOOK_URL"] = (
            "https://discord.com/api/webhooks/123/secret-sentinel"
        )
        self.config["STATE_DB_FILE"] = str(
            Path(self.temp_dir.name) / "missing-state.db"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_overview_is_the_default_active_view(self):
        html = web_ui.render_page(self.config)

        self.assertIn('href="/" aria-current="page"', html)
        self.assertIn("Channel checkpoints", html)
        self.assertIn('id="overview-content"', html)
        self.assertIn('fetch("/api/overview"', html)
        self.assertIn("window.setInterval(refreshOverview, 10000)", html)
        self.assertIn('action="/run-diagnostics"', html)
        self.assertIn("Diagnostics have not been run yet", html)
        self.assertNotIn('class="settings-form"', html)
        self.assertNotIn("secret-sentinel", html)

    def test_settings_and_logs_views_are_separated(self):
        settings_html = web_ui.render_page(self.config, active_view="settings")
        logs_html = web_ui.render_page(self.config, active_view="logs")

        self.assertIn('href="/settings" aria-current="page"', settings_html)
        self.assertIn('class="settings-form" method="post" action="/settings">', settings_html)
        self.assertIn("secret-sentinel", settings_html)
        self.assertNotIn('fetch("/api/overview"', settings_html)
        self.assertIn('href="/logs" aria-current="page"', logs_html)
        self.assertIn('class="logs">', logs_html)
        self.assertNotIn('fetch("/api/overview"', logs_html)
        self.assertNotIn("secret-sentinel", logs_html)

    def test_log_panel_colors_levels_and_escapes_messages(self):
        html = web_ui.render_log_panel(
            "Events",
            [
                "2026-07-16 10:00:00 INFO:telerixa:Checking channels",
                "2026-07-16 10:00:01 SUCCESS:telerixa:Message delivered",
                "2026-07-16 10:00:02 WARNING:telerixa:Retry scheduled",
                "2026-07-16 10:00:03 ERROR:telerixa:<failed>",
            ],
        )

        self.assertIn('class="log-line log-info"', html)
        self.assertIn('class="log-line log-success"', html)
        self.assertIn('class="log-line log-warning"', html)
        self.assertIn('class="log-line log-error"', html)
        self.assertIn("&lt;failed&gt;", html)
        self.assertNotIn("<failed>", html)

    def test_http_server_serves_each_ui_view(self):
        with (
            patch.object(web_ui, "load_config", return_value=self.config),
            patch.object(web_ui, "log_event") as log_event,
        ):
            server = web_ui.SingleInstanceHTTPServer(
                ("127.0.0.1", 0),
                web_ui.ConfigHandler,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                for path, expected_text in (
                    ("/", "Channel checkpoints"),
                    ("/settings", "Discord webhook"),
                    ("/logs", "Recent bot events"),
                ):
                    with urlopen(
                        f"http://127.0.0.1:{server.server_port}{path}",
                        timeout=3,
                    ) as response:
                        body = response.read().decode("utf-8")
                        self.assertEqual(response.status, 200)
                        self.assertIn(expected_text, body)

                with urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/overview",
                    timeout=3,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers.get_content_type(),
                        "application/json",
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIn("Channel checkpoints", payload["overview_html"])
                    self.assertIsInstance(payload["queue_text"], str)
                    self.assertNotIn("secret-sentinel", json.dumps(payload))

                self.assertEqual(log_event.call_count, 3)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_http_server_handles_failed_delivery_actions(self):
        db_file = Path(self.config["STATE_DB_FILE"])
        now_ts = time.time()
        now_text = datetime.fromtimestamp(now_ts).astimezone().isoformat()
        state.init_state_db(
            db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            now_ts,
        )
        requeue_id = state.archive_pending_failure(
            db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            150,
            None,
            [150],
            "Retry from HTTP",
            "terminal",
            "retry queue",
            2,
            now_ts,
            now_text,
        )
        dismiss_id = state.archive_pending_failure(
            db_file,
            web_ui.DB_TIMEOUT_SECONDS,
            web_ui.DB_BUSY_TIMEOUT_MS,
            "alpha_news",
            151,
            None,
            [151],
            "Dismiss from HTTP",
            "unavailable",
            "retry queue",
            1,
            now_ts,
            now_text,
        )

        with (
            patch.object(web_ui, "load_config", return_value=self.config),
            patch.object(web_ui, "log_event"),
        ):
            server = web_ui.SingleInstanceHTTPServer(
                ("127.0.0.1", 0),
                web_ui.ConfigHandler,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                requests = (
                    (
                        "/failed/requeue",
                        requeue_id,
                        "returned to the retry queue",
                    ),
                    (
                        "/failed/dismiss",
                        dismiss_id,
                        "history remains in the database",
                    ),
                )
                for path, archive_id, expected_text in requests:
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}{path}",
                        data=urlencode({"archive_id": archive_id}).encode("ascii"),
                        method="POST",
                    )
                    with urlopen(request, timeout=3) as response:
                        body = response.read().decode("utf-8")
                        self.assertEqual(response.status, 200)
                        self.assertIn(expected_text, body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

        with web_ui.connect_db(db_file) as conn:
            statuses = dict(
                conn.execute(
                    "SELECT id, status FROM failed_deliveries ORDER BY id"
                ).fetchall()
            )
        self.assertEqual(statuses[requeue_id], "requeued")
        self.assertEqual(statuses[dismiss_id], "dismissed")

    def test_http_server_runs_diagnostics_without_exposing_secrets(self):
        diagnostic_results = [
            {
                "component": "sqlite",
                "status": "success",
                "code": "database_ok",
                "details": {"size": "40.0 KB"},
            },
            {
                "component": "discord",
                "status": "success",
                "code": "discord_ok",
                "details": {},
            },
            {
                "component": "telegram",
                "status": "warning",
                "code": "telegram_session_missing",
                "details": {},
            },
        ]
        with (
            patch.object(web_ui, "load_config", return_value=self.config),
            patch.object(web_ui, "log_event"),
            patch.object(
                web_ui.system_diagnostics,
                "run_diagnostics",
                return_value=diagnostic_results,
            ) as run_diagnostics,
        ):
            server = web_ui.SingleInstanceHTTPServer(
                ("127.0.0.1", 0),
                web_ui.ConfigHandler,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/run-diagnostics",
                    data=b"",
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("Diagnostics completed", body)
                    self.assertIn("Discord webhook", body)
                    self.assertIn("Webhook metadata is reachable", body)
                    self.assertNotIn("secret-sentinel", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

        run_diagnostics.assert_called_once()
        self.assertEqual(
            web_ui.LAST_DIAGNOSTICS["results"],
            diagnostic_results,
        )


if __name__ == "__main__":
    unittest.main()
