from datetime import datetime
import json
import sqlite3


class StateConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def parse_stored_ts(value, fallback_ts):
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


def connect_state_db(db_file, timeout_seconds, busy_timeout_ms):
    conn = sqlite3.connect(
        db_file,
        timeout=timeout_seconds,
        factory=StateConnection,
    )
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_state_db(db_file, timeout_seconds, busy_timeout_ms, fallback_ts):
    """Create state tables if they do not exist yet."""
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        failed_deliveries_existed = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'failed_deliveries'
            """
        ).fetchone() is not None
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
                telegram_date_ts REAL NOT NULL,
                last_error TEXT,
                next_chunk_index INTEGER NOT NULL DEFAULT 0,
                media_sent INTEGER NOT NULL DEFAULT 0,
                rendered_text TEXT,
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_status (
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
                last_error TEXT,
                activity TEXT,
                activity_detail TEXT,
                activity_updated_at TEXT,
                activity_updated_ts REAL,
                last_transcode_result TEXT,
                last_transcode_detail TEXT,
                last_transcode_at TEXT,
                last_transcode_ts REAL
            )
            """
        )
        ensure_runtime_status_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS failed_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id INTEGER,
                album_message_ids TEXT NOT NULL,
                reason TEXT NOT NULL,
                last_error TEXT,
                failure_kind TEXT NOT NULL,
                source TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                telegram_date_ts REAL NOT NULL,
                next_chunk_index INTEGER NOT NULL DEFAULT 0,
                media_sent INTEGER NOT NULL DEFAULT 0,
                rendered_text TEXT,
                failed_at TEXT NOT NULL,
                failed_ts REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_at TEXT,
                resolved_ts REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_failed_deliveries_status_time
            ON failed_deliveries (status, failed_ts DESC)
            """
        )
        ensure_pending_message_columns(conn, fallback_ts)
        if not failed_deliveries_existed:
            migrate_processed_failures_to_archive(conn)
        conn.commit()


def ensure_runtime_status_columns(conn):
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(runtime_status)").fetchall()
    }
    definitions = {
        "activity": "TEXT",
        "activity_detail": "TEXT",
        "activity_updated_at": "TEXT",
        "activity_updated_ts": "REAL",
        "last_transcode_result": "TEXT",
        "last_transcode_detail": "TEXT",
        "last_transcode_at": "TEXT",
        "last_transcode_ts": "REAL",
    }
    for column_name, column_type in definitions.items():
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE runtime_status ADD COLUMN {column_name} {column_type}"
            )


def ensure_pending_message_columns(conn, fallback_ts):
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(pending_messages)").fetchall()
    }

    for column_name in ("created_ts", "updated_ts", "next_retry_ts"):
        if column_name not in columns:
            conn.execute(f"ALTER TABLE pending_messages ADD COLUMN {column_name} REAL")

    if "next_chunk_index" not in columns:
        conn.execute(
            "ALTER TABLE pending_messages "
            "ADD COLUMN next_chunk_index INTEGER NOT NULL DEFAULT 0"
        )
    if "media_sent" not in columns:
        conn.execute(
            "ALTER TABLE pending_messages "
            "ADD COLUMN media_sent INTEGER NOT NULL DEFAULT 0"
        )
    if "rendered_text" not in columns:
        conn.execute("ALTER TABLE pending_messages ADD COLUMN rendered_text TEXT")
    if "telegram_date_ts" not in columns:
        conn.execute("ALTER TABLE pending_messages ADD COLUMN telegram_date_ts REAL")

    rows = conn.execute(
        """
        SELECT channel, message_id, created_at, updated_at, next_retry_at
        FROM pending_messages
        WHERE created_ts IS NULL
           OR updated_ts IS NULL
           OR next_retry_ts IS NULL
        """
    ).fetchall()

    for channel, message_id, created_at, updated_at, next_retry_at in rows:
        created_ts = parse_stored_ts(created_at, fallback_ts)
        updated_ts = parse_stored_ts(updated_at, created_ts)
        next_retry_ts = parse_stored_ts(next_retry_at, fallback_ts)
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

    conn.execute(
        """
        UPDATE pending_messages
        SET telegram_date_ts = COALESCE(created_ts, ?)
        WHERE telegram_date_ts IS NULL
        """,
        (fallback_ts,),
    )


def normalize_album_message_ids(message_id, album_message_ids):
    normalized_ids = {int(message_id)}
    for album_message_id in album_message_ids or ():
        normalized_ids.add(int(album_message_id))
    return sorted(normalized_ids)


def parse_album_message_ids(value, fallback_message_id):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    return normalize_album_message_ids(fallback_message_id, parsed)


def migrate_processed_failures_to_archive(conn):
    rows = conn.execute(
        """
        SELECT channel,
               message_id,
               grouped_id,
               processed_at,
               processed_ts
        FROM processed_messages
        WHERE status = 'failed'
        ORDER BY processed_ts ASC, channel ASC, message_id ASC
        """
    ).fetchall()
    grouped_failures = {}
    for channel, message_id, grouped_id, processed_at, processed_ts in rows:
        group_key = (
            str(channel),
            ("group", int(grouped_id))
            if grouped_id is not None
            else ("message", int(message_id)),
        )
        failure = grouped_failures.setdefault(
            group_key,
            {
                "channel": str(channel),
                "grouped_id": int(grouped_id) if grouped_id is not None else None,
                "message_ids": [],
                "failed_at": str(processed_at),
                "failed_ts": float(processed_ts),
            },
        )
        failure["message_ids"].append(int(message_id))
        if float(processed_ts) >= failure["failed_ts"]:
            failure["failed_at"] = str(processed_at)
            failure["failed_ts"] = float(processed_ts)

    for failure in grouped_failures.values():
        message_ids = sorted(set(failure["message_ids"]))
        reason = "Imported from legacy processed-message state"
        conn.execute(
            """
            INSERT INTO failed_deliveries (
                channel,
                message_id,
                grouped_id,
                album_message_ids,
                reason,
                last_error,
                failure_kind,
                source,
                attempts,
                telegram_date_ts,
                next_chunk_index,
                media_sent,
                rendered_text,
                failed_at,
                failed_ts,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'legacy', 'state migration', 0, ?, 0, 0, NULL, ?, ?, 'open')
            """,
            (
                failure["channel"],
                message_ids[0],
                failure["grouped_id"],
                json.dumps(message_ids),
                reason,
                reason,
                failure["failed_ts"],
                failure["failed_at"],
                failure["failed_ts"],
            ),
        )


def mark_runtime_started(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
    pid,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            """
            INSERT INTO runtime_status (
                service,
                status,
                pid,
                started_at,
                started_ts,
                heartbeat_at,
                heartbeat_ts,
                last_cycle_started_at,
                last_cycle_started_ts,
                last_cycle_finished_at,
                last_cycle_finished_ts,
                last_cycle_result,
                last_error
            )
            VALUES (?, 'running', ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)
            ON CONFLICT(service) DO UPDATE SET
                status = 'running',
                pid = excluded.pid,
                started_at = excluded.started_at,
                started_ts = excluded.started_ts,
                heartbeat_at = excluded.heartbeat_at,
                heartbeat_ts = excluded.heartbeat_ts,
                last_cycle_started_at = NULL,
                last_cycle_started_ts = NULL,
                last_cycle_finished_at = NULL,
                last_cycle_finished_ts = NULL,
                last_cycle_result = NULL,
                last_error = NULL,
                activity = NULL,
                activity_detail = NULL,
                activity_updated_at = NULL,
                activity_updated_ts = NULL
            """,
            (
                service,
                int(pid),
                now_text,
                float(now_ts),
                now_text,
                float(now_ts),
            ),
        )
        conn.commit()


def touch_runtime_heartbeat(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
    pid,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            """
            INSERT INTO runtime_status (
                service,
                status,
                pid,
                started_at,
                started_ts,
                heartbeat_at,
                heartbeat_ts
            )
            VALUES (?, 'running', ?, ?, ?, ?, ?)
            ON CONFLICT(service) DO UPDATE SET
                status = 'running',
                pid = excluded.pid,
                heartbeat_at = excluded.heartbeat_at,
                heartbeat_ts = excluded.heartbeat_ts
            """,
            (
                service,
                int(pid),
                now_text,
                float(now_ts),
                now_text,
                float(now_ts),
            ),
        )
        conn.commit()


def mark_runtime_cycle_started(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            """
            UPDATE runtime_status
            SET status = 'running',
                heartbeat_at = ?,
                heartbeat_ts = ?,
                last_cycle_started_at = ?,
                last_cycle_started_ts = ?,
                last_cycle_result = 'running',
                last_error = NULL
            WHERE service = ?
            """,
            (
                now_text,
                float(now_ts),
                now_text,
                float(now_ts),
                service,
            ),
        )
        conn.commit()


def mark_runtime_cycle_finished(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
    result,
    error,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            """
            UPDATE runtime_status
            SET status = 'running',
                heartbeat_at = ?,
                heartbeat_ts = ?,
                last_cycle_finished_at = ?,
                last_cycle_finished_ts = ?,
                last_cycle_result = ?,
                last_error = ?
            WHERE service = ?
            """,
            (
                now_text,
                float(now_ts),
                now_text,
                float(now_ts),
                result,
                str(error or "") or None,
                service,
            ),
        )
        conn.commit()


def mark_runtime_stopped(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
    status,
    error,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            """
            UPDATE runtime_status
            SET status = ?,
                heartbeat_at = ?,
                heartbeat_ts = ?,
                activity = NULL,
                activity_detail = NULL,
                last_error = COALESCE(?, last_error)
            WHERE service = ?
            """,
            (
                status,
                now_text,
                float(now_ts),
                str(error or "") or None,
                service,
            ),
        )
        conn.commit()


def update_runtime_activity(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
    activity,
    detail,
    transcode_result,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        if transcode_result:
            conn.execute(
                """
                UPDATE runtime_status
                SET activity = ?,
                    activity_detail = ?,
                    activity_updated_at = ?,
                    activity_updated_ts = ?,
                    last_transcode_result = ?,
                    last_transcode_detail = ?,
                    last_transcode_at = ?,
                    last_transcode_ts = ?
                WHERE service = ?
                """,
                (
                    str(activity or "") or None,
                    str(detail or "") or None,
                    now_text,
                    float(now_ts),
                    str(transcode_result),
                    str(detail or "") or None,
                    now_text,
                    float(now_ts),
                    service,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE runtime_status
                SET activity = ?,
                    activity_detail = ?,
                    activity_updated_at = ?,
                    activity_updated_ts = ?
                WHERE service = ?
                """,
                (
                    str(activity or "") or None,
                    str(detail or "") or None,
                    now_text,
                    float(now_ts),
                    service,
                ),
            )
        conn.commit()


def get_runtime_status(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    service,
):
    columns = (
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
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        row = conn.execute(
            """
            SELECT service,
                   status,
                   pid,
                   started_at,
                   started_ts,
                   heartbeat_at,
                   heartbeat_ts,
                   last_cycle_started_at,
                   last_cycle_started_ts,
                   last_cycle_finished_at,
                   last_cycle_finished_ts,
                   last_cycle_result,
                   last_error,
                   activity,
                   activity_detail,
                   activity_updated_at,
                   activity_updated_ts,
                   last_transcode_result,
                   last_transcode_detail,
                   last_transcode_at,
                   last_transcode_ts
            FROM runtime_status
            WHERE service = ?
            """,
            (service,),
        ).fetchone()

    return dict(zip(columns, row)) if row else None


def get_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel):
    """Return the last processed message_id for a channel."""
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        row = conn.execute(
            "SELECT last_seen_id FROM channel_state WHERE channel = ?",
            (channel,),
        ).fetchone()

    if row is None:
        return None

    return row[0]


def set_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel, message_id, now_text):
    """Save the processed-message boundary for a channel."""
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            """
            INSERT INTO channel_state (channel, last_seen_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                last_seen_id = excluded.last_seen_id,
                updated_at = excluded.updated_at
            """,
            (channel, int(message_id), now_text),
        )
        conn.commit()


def advance_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel, message_id, now_text):
    """Move the channel boundary forward only."""
    current_last_seen_id = get_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel)
    if current_last_seen_id is None or int(message_id) > current_last_seen_id:
        set_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel, message_id, now_text)


def has_channel_state(db_file, timeout_seconds, busy_timeout_ms, channel):
    return get_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel) is not None


def get_processed_message_state(db_file, timeout_seconds, busy_timeout_ms, channel, message_id):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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


def get_processed_group_message_ids(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    grouped_id,
):
    if grouped_id is None:
        return []

    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        rows = conn.execute(
            """
            SELECT message_id
            FROM processed_messages
            WHERE channel = ? AND grouped_id = ?
            ORDER BY message_id ASC
            """,
            (channel, int(grouped_id)),
        ).fetchall()

    return [row[0] for row in rows]


def has_pending_message(db_file, timeout_seconds, busy_timeout_ms, channel, message_id=None, grouped_id=None):
    message_value = int(message_id) if message_id is not None else None
    grouped_value = int(grouped_id) if grouped_id is not None else None

    if message_value is None and grouped_value is None:
        return False

    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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


def mark_processed_message(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    message_id,
    grouped_id,
    status,
    now_ts,
    now_text,
):
    grouped_value = int(grouped_id) if grouped_id else None

    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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


def calculate_retry_schedule(current_attempts, count_attempt, now_ts):
    attempts = current_attempts + 1 if count_attempt else current_attempts
    delay_attempts = attempts if count_attempt else max(current_attempts, 3)
    next_retry_ts = now_ts + get_retry_delay_seconds(delay_attempts)
    return attempts, next_retry_ts


def add_pending_message(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    message_id,
    grouped_id,
    error,
    now_ts,
    now_text,
    telegram_date_ts=None,
):
    grouped_value = int(grouped_id) if grouped_id else None
    telegram_date_ts = (
        float(telegram_date_ts)
        if telegram_date_ts is not None
        else float(now_ts)
    )

    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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
                telegram_date_ts,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(channel, message_id) DO UPDATE SET
                grouped_id = excluded.grouped_id,
                updated_at = excluded.updated_at,
                updated_ts = excluded.updated_ts,
                telegram_date_ts = COALESCE(
                    pending_messages.telegram_date_ts,
                    excluded.telegram_date_ts
                ),
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
                telegram_date_ts,
                error,
            ),
        )
        conn.commit()


def get_pending_attempts(db_file, timeout_seconds, busy_timeout_ms, channel, message_id):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        row = conn.execute(
            """
            SELECT attempts
            FROM pending_messages
            WHERE channel = ? AND message_id = ?
            """,
            (channel, int(message_id)),
        ).fetchone()

    return row[0] if row else 0


def update_pending_failure(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    message_id,
    attempts,
    error,
    now_ts,
    now_text,
    next_retry_ts,
    next_retry_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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


def get_pending_delivery_progress(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    message_id,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        row = conn.execute(
            """
            SELECT next_chunk_index, media_sent, rendered_text
            FROM pending_messages
            WHERE channel = ? AND message_id = ?
            """,
            (channel, int(message_id)),
        ).fetchone()

    if not row:
        return 0, False, None
    return max(0, int(row[0] or 0)), bool(row[1]), row[2]


def update_pending_delivery_progress(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    message_id,
    next_chunk_index=None,
    media_sent=None,
    rendered_text=None,
):
    assignments = []
    values = []

    if next_chunk_index is not None:
        assignments.append("next_chunk_index = MAX(next_chunk_index, ?)")
        values.append(max(0, int(next_chunk_index)))
    if media_sent is not None:
        assignments.append("media_sent = MAX(media_sent, ?)")
        values.append(1 if media_sent else 0)
    if rendered_text is not None:
        assignments.append("rendered_text = COALESCE(rendered_text, ?)")
        values.append(str(rendered_text))
    if not assignments:
        return

    values.extend((channel, int(message_id)))
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            f"""
            UPDATE pending_messages
            SET {', '.join(assignments)}
            WHERE channel = ? AND message_id = ?
            """,
            values,
        )
        conn.commit()


def delete_pending_message(db_file, timeout_seconds, busy_timeout_ms, channel, message_id):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        conn.execute(
            "DELETE FROM pending_messages WHERE channel = ? AND message_id = ?",
            (channel, int(message_id)),
        )
        conn.commit()


def archive_pending_failure(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    channel,
    message_id,
    grouped_id,
    album_message_ids,
    reason,
    failure_kind,
    source,
    attempts,
    now_ts,
    now_text,
):
    message_id = int(message_id)
    grouped_value = int(grouped_id) if grouped_id is not None else None
    delivery_ids = normalize_album_message_ids(message_id, album_message_ids)

    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        pending_row = conn.execute(
            """
            SELECT telegram_date_ts,
                   next_chunk_index,
                   media_sent,
                   rendered_text,
                   last_error
            FROM pending_messages
            WHERE channel = ? AND message_id = ?
            """,
            (channel, message_id),
        ).fetchone()
        if pending_row:
            telegram_date_ts = float(pending_row[0])
            next_chunk_index = max(0, int(pending_row[1] or 0))
            media_sent = 1 if pending_row[2] else 0
            rendered_text = pending_row[3]
            last_error = str(pending_row[4] or reason)
        else:
            telegram_date_ts = float(now_ts)
            next_chunk_index = 0
            media_sent = 0
            rendered_text = None
            last_error = str(reason)

        cursor = conn.execute(
            """
            INSERT INTO failed_deliveries (
                channel,
                message_id,
                grouped_id,
                album_message_ids,
                reason,
                last_error,
                failure_kind,
                source,
                attempts,
                telegram_date_ts,
                next_chunk_index,
                media_sent,
                rendered_text,
                failed_at,
                failed_ts,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                channel,
                message_id,
                grouped_value,
                json.dumps(delivery_ids),
                str(reason),
                last_error,
                str(failure_kind),
                str(source),
                max(0, int(attempts)),
                telegram_date_ts,
                next_chunk_index,
                media_sent,
                rendered_text,
                now_text,
                float(now_ts),
            ),
        )
        conn.execute(
            "DELETE FROM pending_messages WHERE channel = ? AND message_id = ?",
            (channel, message_id),
        )
        for delivery_message_id in delivery_ids:
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
                VALUES (?, ?, ?, 'failed', ?, ?)
                ON CONFLICT(channel, message_id) DO UPDATE SET
                    grouped_id = excluded.grouped_id,
                    status = excluded.status,
                    processed_at = excluded.processed_at,
                    processed_ts = excluded.processed_ts
                """,
                (
                    channel,
                    int(delivery_message_id),
                    grouped_value,
                    now_text,
                    float(now_ts),
                ),
            )
        conn.commit()
        archive_id = cursor.lastrowid
        if archive_id is None:
            raise sqlite3.DatabaseError("SQLite did not return a failed-delivery ID.")
        return int(archive_id)


def requeue_failed_delivery(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    archive_id,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        row = conn.execute(
            """
            SELECT channel,
                   message_id,
                   grouped_id,
                   album_message_ids,
                   telegram_date_ts,
                   next_chunk_index,
                   media_sent,
                   rendered_text,
                   reason
            FROM failed_deliveries
            WHERE id = ? AND status = 'open'
            """,
            (int(archive_id),),
        ).fetchone()
        if not row:
            return "missing"

        (
            channel,
            message_id,
            grouped_id,
            album_message_ids,
            telegram_date_ts,
            next_chunk_index,
            media_sent,
            rendered_text,
            reason,
        ) = row
        pending_exists = conn.execute(
            """
            SELECT 1
            FROM pending_messages
            WHERE channel = ? AND message_id = ?
            """,
            (channel, int(message_id)),
        ).fetchone()
        if pending_exists:
            return "already_pending"

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
                telegram_date_ts,
                last_error,
                next_chunk_index,
                media_sent,
                rendered_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel,
                int(message_id),
                int(grouped_id) if grouped_id is not None else None,
                now_text,
                float(now_ts),
                now_text,
                float(now_ts),
                now_text,
                float(now_ts),
                float(telegram_date_ts),
                str(reason),
                max(0, int(next_chunk_index or 0)),
                1 if media_sent else 0,
                rendered_text,
            ),
        )
        delivery_ids = parse_album_message_ids(album_message_ids, message_id)
        for delivery_message_id in delivery_ids:
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
                VALUES (?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(channel, message_id) DO UPDATE SET
                    grouped_id = excluded.grouped_id,
                    status = excluded.status,
                    processed_at = excluded.processed_at,
                    processed_ts = excluded.processed_ts
                """,
                (
                    channel,
                    int(delivery_message_id),
                    int(grouped_id) if grouped_id is not None else None,
                    now_text,
                    float(now_ts),
                ),
            )
        conn.execute(
            """
            UPDATE failed_deliveries
            SET status = 'requeued',
                resolved_at = ?,
                resolved_ts = ?
            WHERE id = ?
            """,
            (now_text, float(now_ts), int(archive_id)),
        )
        conn.commit()
        return "requeued"


def dismiss_failed_delivery(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    archive_id,
    now_ts,
    now_text,
):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        cursor = conn.execute(
            """
            UPDATE failed_deliveries
            SET status = 'dismissed',
                resolved_at = ?,
                resolved_ts = ?
            WHERE id = ? AND status = 'open'
            """,
            (now_text, float(now_ts), int(archive_id)),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_due_pending_messages(db_file, timeout_seconds, busy_timeout_ms, now_ts, limit):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        rows = conn.execute(
            """
            SELECT channel,
                   message_id,
                   grouped_id,
                   attempts,
                   last_error,
                   next_chunk_index,
                   media_sent,
                   rendered_text
            FROM pending_messages
            WHERE next_retry_ts <= ?
            ORDER BY telegram_date_ts ASC, next_retry_ts ASC, created_ts ASC
            LIMIT ?
            """,
            (now_ts, int(limit)),
        ).fetchall()

    return rows


def get_pending_count(db_file, timeout_seconds, busy_timeout_ms):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_messages").fetchone()
    return row[0] if row else 0


def get_pending_retry_status(db_file, timeout_seconds, busy_timeout_ms, now_ts):
    with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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


def migrate_legacy_seen_messages(
    db_file,
    timeout_seconds,
    busy_timeout_ms,
    legacy_seen,
    now_ts,
    now_text,
):
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
        if not has_channel_state(db_file, timeout_seconds, busy_timeout_ms, channel):
            set_last_seen_id(db_file, timeout_seconds, busy_timeout_ms, channel, message_id, now_text)
            migrated_channels += 1

    inserted = 0
    if processed_keys:
        with connect_state_db(db_file, timeout_seconds, busy_timeout_ms) as conn:
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

    return migrated_channels, inserted
