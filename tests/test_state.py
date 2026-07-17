import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from telerixa_core import state


DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = DB_TIMEOUT_SECONDS * 1000


class StateStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_file = str(Path(self.temp_dir.name) / "state.db")
        self.now_ts = 1000.0
        self.now_text = "2026-07-08T20:00:00+00:00"
        state.init_state_db(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            self.now_ts,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_channel_state_moves_forward_only(self):
        self.assertIsNone(
            state.get_last_seen_id(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
            )
        )

        state.set_last_seen_id(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            5,
            self.now_text,
        )
        state.advance_last_seen_id(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            4,
            self.now_text,
        )
        self.assertEqual(
            state.get_last_seen_id(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
            ),
            5,
        )

        state.advance_last_seen_id(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            7,
            self.now_text,
        )
        self.assertEqual(
            state.get_last_seen_id(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
            ),
            7,
        )

    def test_state_connection_closes_after_context_manager(self):
        connection = state.connect_state_db(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
        )

        with connection as active_connection:
            active_connection.execute("SELECT 1").fetchone()

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_existing_runtime_table_gets_transcode_status_columns(self):
        legacy_db = str(Path(self.temp_dir.name) / "legacy-runtime.db")
        with closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute(
                """
                CREATE TABLE runtime_status (
                    service TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    started_at TEXT NOT NULL,
                    started_ts REAL NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    heartbeat_ts REAL NOT NULL,
                    last_cycle_started_at TEXT,
                    last_cycle_started_ts REAL,
                    last_cycle_finished_at TEXT,
                    last_cycle_finished_ts REAL,
                    last_cycle_result TEXT,
                    last_error TEXT
                )
                """
            )

        state.init_state_db(
            legacy_db,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            self.now_ts,
        )

        with closing(sqlite3.connect(legacy_db)) as conn:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(runtime_status)"
                ).fetchall()
            }
        self.assertTrue(
            {
                "activity",
                "activity_detail",
                "last_transcode_result",
                "last_transcode_detail",
            }.issubset(columns)
        )

    def test_runtime_status_tracks_process_and_cycle_lifecycle(self):
        state.mark_runtime_started(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            4321,
            self.now_ts,
            self.now_text,
        )
        runtime = state.get_runtime_status(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
        )
        assert runtime is not None
        self.assertEqual(runtime["status"], "running")
        self.assertEqual(runtime["pid"], 4321)
        self.assertIsNone(runtime["last_cycle_result"])

        state.mark_runtime_cycle_started(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            self.now_ts + 10,
            "2026-07-08T20:00:10+00:00",
        )
        state.mark_runtime_cycle_finished(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            "error",
            "Discord unavailable",
            self.now_ts + 20,
            "2026-07-08T20:00:20+00:00",
        )
        runtime = state.get_runtime_status(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
        )
        assert runtime is not None
        self.assertEqual(runtime["last_cycle_result"], "error")
        self.assertEqual(runtime["last_error"], "Discord unavailable")
        self.assertEqual(runtime["heartbeat_ts"], self.now_ts + 20)

        state.update_runtime_activity(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            "transcoding",
            "@demo/10: converting 40.0 MB",
            "",
            self.now_ts + 25,
            "2026-07-08T20:00:25+00:00",
        )
        state.update_runtime_activity(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            "",
            "@demo/10: 40.0 MB -> 20.0 MB",
            "success",
            self.now_ts + 26,
            "2026-07-08T20:00:26+00:00",
        )
        runtime = state.get_runtime_status(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
        )
        assert runtime is not None
        self.assertIsNone(runtime["activity"])
        self.assertEqual(runtime["last_transcode_result"], "success")
        self.assertIn("40.0 MB -> 20.0 MB", runtime["last_transcode_detail"])

        state.touch_runtime_heartbeat(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            4321,
            self.now_ts + 30,
            "2026-07-08T20:00:30+00:00",
        )
        state.mark_runtime_stopped(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            "stopped",
            "",
            self.now_ts + 40,
            "2026-07-08T20:00:40+00:00",
        )
        runtime = state.get_runtime_status(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
        )
        assert runtime is not None
        self.assertEqual(runtime["status"], "stopped")
        self.assertEqual(runtime["heartbeat_ts"], self.now_ts + 40)
        self.assertEqual(runtime["last_error"], "Discord unavailable")

    def test_new_runtime_start_clears_previous_cycle_state(self):
        state.mark_runtime_started(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            100,
            self.now_ts,
            self.now_text,
        )
        state.mark_runtime_cycle_finished(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            "error",
            "old error",
            self.now_ts + 5,
            "2026-07-08T20:00:05+00:00",
        )

        state.mark_runtime_started(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
            200,
            self.now_ts + 60,
            "2026-07-08T20:01:00+00:00",
        )
        runtime = state.get_runtime_status(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "forwarder",
        )
        assert runtime is not None
        self.assertEqual(runtime["pid"], 200)
        self.assertIsNone(runtime["last_cycle_result"])
        self.assertIsNone(runtime["last_error"])

    def test_pending_queue_lifecycle(self):
        state.add_pending_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            777,
            "temporary failure",
            self.now_ts,
            self.now_text,
        )

        self.assertEqual(
            state.get_pending_count(self.db_file, DB_TIMEOUT_SECONDS, DB_BUSY_TIMEOUT_MS),
            1,
        )
        self.assertTrue(
            state.has_pending_message(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                message_id=10,
                grouped_id=777,
            )
        )
        self.assertEqual(
            len(
                state.get_due_pending_messages(
                    self.db_file,
                    DB_TIMEOUT_SECONDS,
                    DB_BUSY_TIMEOUT_MS,
                    self.now_ts,
                    limit=10,
                )
            ),
            1,
        )
        self.assertEqual(
            state.get_pending_delivery_progress(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                10,
            ),
            (0, False, None),
        )

        state.update_pending_delivery_progress(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            next_chunk_index=2,
            media_sent=True,
            rendered_text="stable rendered payload",
        )
        state.update_pending_delivery_progress(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            next_chunk_index=1,
            media_sent=False,
            rendered_text="changed payload must not replace snapshot",
        )
        self.assertEqual(
            state.get_pending_delivery_progress(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                10,
            ),
            (2, True, "stable rendered payload"),
        )

        attempts = state.get_pending_attempts(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
        ) + 1
        next_retry_ts = self.now_ts + state.get_retry_delay_seconds(attempts)
        state.update_pending_failure(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            attempts,
            "retry failed",
            self.now_ts,
            self.now_text,
            next_retry_ts,
            "2026-07-08T20:00:30+00:00",
        )

        pending_count, due_count, stored_next_retry_ts = state.get_pending_retry_status(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            self.now_ts,
        )
        self.assertEqual(pending_count, 1)
        self.assertEqual(due_count, 0)
        self.assertEqual(stored_next_retry_ts, next_retry_ts)

        state.delete_pending_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
        )
        self.assertEqual(
            state.get_pending_count(self.db_file, DB_TIMEOUT_SECONDS, DB_BUSY_TIMEOUT_MS),
            0,
        )

    def test_due_pending_messages_are_ordered_by_telegram_date(self):
        state.add_pending_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "later-channel",
            20,
            None,
            "queued later",
            self.now_ts,
            self.now_text,
            telegram_date_ts=self.now_ts + 120,
        )
        state.add_pending_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "earlier-channel",
            10,
            None,
            "queued earlier",
            self.now_ts,
            self.now_text,
            telegram_date_ts=self.now_ts + 60,
        )

        due_messages = state.get_due_pending_messages(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            self.now_ts,
            limit=10,
        )

        self.assertEqual(
            [(row[0], row[1]) for row in due_messages],
            [("earlier-channel", 10), ("later-channel", 20)],
        )

    def test_failed_album_archive_is_atomic_and_requeue_preserves_progress(self):
        state.add_pending_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            777,
            "Discord unavailable",
            self.now_ts,
            self.now_text,
            telegram_date_ts=900.0,
        )
        state.update_pending_delivery_progress(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            next_chunk_index=2,
            media_sent=True,
            rendered_text="stable payload",
        )
        for message_id in (10, 11):
            state.mark_processed_message(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                message_id,
                777,
                "queued",
                self.now_ts,
                self.now_text,
            )

        archive_id = state.archive_pending_failure(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            777,
            [10, 11],
            "Maximum attempts reached",
            "max_attempts",
            "retry queue",
            3,
            self.now_ts + 10,
            "2026-07-08T20:00:10+00:00",
        )

        self.assertEqual(
            state.get_pending_count(self.db_file, DB_TIMEOUT_SECONDS, DB_BUSY_TIMEOUT_MS),
            0,
        )
        with state.connect_state_db(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
        ) as conn:
            archived = conn.execute(
                """
                SELECT album_message_ids,
                       failure_kind,
                       attempts,
                       telegram_date_ts,
                       next_chunk_index,
                       media_sent,
                       rendered_text,
                       status
                FROM failed_deliveries
                WHERE id = ?
                """,
                (archive_id,),
            ).fetchone()
        self.assertEqual(json.loads(archived[0]), [10, 11])
        self.assertEqual(archived[1:], ("max_attempts", 3, 900.0, 2, 1, "stable payload", "open"))
        for message_id in (10, 11):
            self.assertEqual(
                state.get_processed_message_state(
                    self.db_file,
                    DB_TIMEOUT_SECONDS,
                    DB_BUSY_TIMEOUT_MS,
                    "demo",
                    message_id,
                ),
                ("failed", 777),
            )

        result = state.requeue_failed_delivery(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            archive_id,
            self.now_ts + 20,
            "2026-07-08T20:00:20+00:00",
        )

        self.assertEqual(result, "requeued")
        with state.connect_state_db(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
        ) as conn:
            pending = conn.execute(
                """
                SELECT attempts,
                       telegram_date_ts,
                       next_chunk_index,
                       media_sent,
                       rendered_text
                FROM pending_messages
                WHERE channel = 'demo' AND message_id = 10
                """
            ).fetchone()
            archive_status = conn.execute(
                "SELECT status FROM failed_deliveries WHERE id = ?",
                (archive_id,),
            ).fetchone()[0]
        self.assertEqual(pending, (0, 900.0, 2, 1, "stable payload"))
        self.assertEqual(archive_status, "requeued")
        self.assertEqual(
            state.requeue_failed_delivery(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                archive_id,
                self.now_ts + 30,
                "2026-07-08T20:00:30+00:00",
            ),
            "missing",
        )

    def test_failed_delivery_can_be_dismissed_without_deleting_history(self):
        archive_id = state.archive_pending_failure(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            20,
            None,
            [20],
            "Telegram message unavailable",
            "unavailable",
            "retry queue",
            1,
            self.now_ts,
            self.now_text,
        )

        dismissed = state.dismiss_failed_delivery(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            archive_id,
            self.now_ts + 10,
            "2026-07-08T20:00:10+00:00",
        )

        self.assertTrue(dismissed)
        with state.connect_state_db(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
        ) as conn:
            row = conn.execute(
                "SELECT status, resolved_at FROM failed_deliveries WHERE id = ?",
                (archive_id,),
            ).fetchone()
        self.assertEqual(row, ("dismissed", "2026-07-08T20:00:10+00:00"))

    def test_transient_retry_does_not_consume_attempt_limit(self):
        bounded_attempts, bounded_retry_ts = state.calculate_retry_schedule(
            current_attempts=2,
            count_attempt=True,
            now_ts=self.now_ts,
        )
        transient_attempts, transient_retry_ts = state.calculate_retry_schedule(
            current_attempts=2,
            count_attempt=False,
            now_ts=self.now_ts,
        )

        self.assertEqual(bounded_attempts, 3)
        self.assertEqual(bounded_retry_ts, self.now_ts + 300)
        self.assertEqual(transient_attempts, 2)
        self.assertEqual(transient_retry_ts, self.now_ts + 300)

    def test_processed_messages_are_upserted(self):
        state.mark_processed_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            777,
            "queued",
            self.now_ts,
            self.now_text,
        )
        self.assertEqual(
            state.get_processed_message_state(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                10,
            ),
            ("queued", 777),
        )

        state.mark_processed_message(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            "demo",
            10,
            777,
            "sent",
            self.now_ts + 10,
            "2026-07-08T20:00:10+00:00",
        )
        self.assertEqual(
            state.get_processed_message_state(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                10,
            ),
            ("sent", 777),
        )

    def test_processed_album_ids_can_be_recovered_by_group(self):
        for message_id in (10, 11, 12):
            state.mark_processed_message(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                message_id,
                777,
                "queued",
                self.now_ts,
                self.now_text,
            )

        self.assertEqual(
            state.get_processed_group_message_ids(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                777,
            ),
            [10, 11, 12],
        )

    def test_legacy_seen_messages_migration(self):
        migrated_channels, inserted = state.migrate_legacy_seen_messages(
            self.db_file,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            ["legacychan_3", "legacychan_5", "broken-key", "other_2"],
            self.now_ts,
            self.now_text,
        )

        self.assertEqual(migrated_channels, 2)
        self.assertEqual(inserted, 3)
        self.assertEqual(
            state.get_last_seen_id(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "legacychan",
            ),
            5,
        )
        self.assertEqual(
            state.get_processed_message_state(
                self.db_file,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "legacychan",
                3,
            ),
            ("sent", None),
        )

    def test_existing_failed_processed_album_is_migrated_to_archive(self):
        legacy_db = str(Path(self.temp_dir.name) / "legacy-failures.db")
        with closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute(
                """
                CREATE TABLE processed_messages (
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
            conn.executemany(
                """
                INSERT INTO processed_messages (
                    channel,
                    message_id,
                    grouped_id,
                    status,
                    processed_at,
                    processed_ts
                ) VALUES (?, ?, ?, 'failed', ?, ?)
                """,
                (
                    ("demo", 30, 888, self.now_text, self.now_ts),
                    ("demo", 31, 888, self.now_text, self.now_ts),
                ),
            )
            conn.commit()

        state.init_state_db(
            legacy_db,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            self.now_ts,
        )

        with state.connect_state_db(
            legacy_db,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
        ) as conn:
            row = conn.execute(
                """
                SELECT message_id,
                       grouped_id,
                       album_message_ids,
                       failure_kind,
                       status
                FROM failed_deliveries
                """
            ).fetchone()
        self.assertEqual(row[:2], (30, 888))
        self.assertEqual(json.loads(row[2]), [30, 31])
        self.assertEqual(row[3:], ("legacy", "open"))

    def test_existing_pending_table_gets_delivery_progress_columns(self):
        legacy_db = str(Path(self.temp_dir.name) / "legacy-state.db")
        with closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute(
                """
                CREATE TABLE pending_messages (
                    channel TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    grouped_id INTEGER,
                    created_at TEXT NOT NULL,
                    created_ts REAL,
                    updated_at TEXT NOT NULL,
                    updated_ts REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL,
                    next_retry_ts REAL,
                    last_error TEXT,
                    PRIMARY KEY (channel, message_id)
                )
                """
            )
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
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    "demo",
                    10,
                    self.now_text,
                    self.now_ts,
                    self.now_text,
                    self.now_ts,
                    self.now_text,
                    self.now_ts,
                    "old queue row",
                ),
            )
            conn.commit()

        state.init_state_db(
            legacy_db,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
            self.now_ts,
        )

        self.assertEqual(
            state.get_pending_delivery_progress(
                legacy_db,
                DB_TIMEOUT_SECONDS,
                DB_BUSY_TIMEOUT_MS,
                "demo",
                10,
            ),
            (0, False, None),
        )
        with state.connect_state_db(
            legacy_db,
            DB_TIMEOUT_SECONDS,
            DB_BUSY_TIMEOUT_MS,
        ) as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(pending_messages)")
            }
            telegram_date_ts = conn.execute(
                """
                SELECT telegram_date_ts
                FROM pending_messages
                WHERE channel = ? AND message_id = ?
                """,
                ("demo", 10),
            ).fetchone()[0]

        self.assertIn("telegram_date_ts", columns)
        self.assertEqual(telegram_date_ts, self.now_ts)


if __name__ == "__main__":
    unittest.main()
