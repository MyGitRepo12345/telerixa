from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import os
import shutil
import sqlite3
import subprocess


DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
MINIMUM_DISK_FREE_BYTES = 512 * 1024 * 1024
WARNING_DISK_FREE_BYTES = 2 * 1024 * 1024 * 1024


def diagnostic_result(component, status, code, **details):
    return {
        "component": component,
        "status": status,
        "code": code,
        "details": details,
    }


def format_bytes(value):
    value = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    precision = 0 if unit in {"B", "KB"} else 1
    return f"{size:.{precision}f} {unit}"


def check_sqlite(config):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return diagnostic_result("sqlite", "error", "database_missing")

    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{db_file.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            return diagnostic_result(
                "sqlite",
                "error",
                "integrity_failed",
                error=str(integrity[0] if integrity else "unknown"),
            )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.Error as error:
        return diagnostic_result(
            "sqlite",
            "error",
            "database_error",
            error=str(error),
        )
    finally:
        if connection is not None:
            connection.close()

    required_tables = {
        "channel_state",
        "pending_messages",
        "processed_messages",
        "runtime_status",
        "failed_deliveries",
    }
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        return diagnostic_result(
            "sqlite",
            "warning",
            "schema_outdated",
            tables=", ".join(missing_tables),
        )
    return diagnostic_result(
        "sqlite",
        "success",
        "database_ok",
        size=format_bytes(db_file.stat().st_size),
    )


def check_disk(base_dir):
    try:
        usage = shutil.disk_usage(Path(base_dir).resolve())
    except OSError as error:
        return diagnostic_result(
            "disk",
            "error",
            "disk_error",
            error=str(error),
        )

    free_ratio = usage.free / usage.total if usage.total else 0
    details = {
        "free": format_bytes(usage.free),
        "total": format_bytes(usage.total),
        "percent": f"{free_ratio * 100:.1f}",
    }
    if usage.free < MINIMUM_DISK_FREE_BYTES:
        return diagnostic_result("disk", "error", "disk_critical", **details)
    if usage.free < WARNING_DISK_FREE_BYTES or free_ratio < 0.05:
        return diagnostic_result("disk", "warning", "disk_low", **details)
    return diagnostic_result("disk", "success", "disk_ok", **details)


def check_ffmpeg():
    executable = shutil.which("ffmpeg")
    if not executable:
        return diagnostic_result("ffmpeg", "warning", "ffmpeg_missing")

    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return diagnostic_result(
            "ffmpeg",
            "error",
            "ffmpeg_error",
            error=str(error),
        )
    if completed.returncode != 0:
        return diagnostic_result(
            "ffmpeg",
            "error",
            "ffmpeg_exit_error",
            exit_code=completed.returncode,
        )

    version_line = (completed.stdout or completed.stderr or "").splitlines()
    version = version_line[0].strip() if version_line else "ffmpeg"
    return diagnostic_result(
        "ffmpeg",
        "success",
        "ffmpeg_ok",
        version=version[:160],
    )


def check_discord(config, open_url=urlopen):
    webhook_url = str(config.get("DISCORD_WEBHOOK_URL", "")).strip()
    if not webhook_url.startswith(DISCORD_WEBHOOK_PREFIXES):
        return diagnostic_result("discord", "error", "discord_not_configured")

    request = Request(
        webhook_url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "Telerixa diagnostics",
        },
    )
    try:
        with open_url(request, timeout=5) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            status = int(status)
            body = response.read(64 * 1024)
    except HTTPError as error:
        if error.code in {401, 403, 404}:
            return diagnostic_result(
                "discord",
                "error",
                "discord_rejected",
                http_status=error.code,
            )
        return diagnostic_result(
            "discord",
            "warning",
            "discord_http_error",
            http_status=error.code,
        )
    except (URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        return diagnostic_result(
            "discord",
            "warning",
            "discord_network_error",
            error=str(reason),
        )

    if status != 200:
        return diagnostic_result(
            "discord",
            "warning",
            "discord_http_error",
            http_status=status,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return diagnostic_result("discord", "error", "discord_invalid_response")
    if not isinstance(payload, dict) or not payload.get("id"):
        return diagnostic_result("discord", "error", "discord_invalid_response")
    return diagnostic_result("discord", "success", "discord_ok")


def check_telegram(config, base_dir):
    try:
        api_id = int(config.get("TELEGRAM_API_ID", 0))
    except (TypeError, ValueError):
        api_id = 0
    api_hash = str(config.get("TELEGRAM_API_HASH", "")).strip()
    if api_id <= 0 or not api_hash or api_hash == "your_telegram_api_hash":
        return diagnostic_result("telegram", "error", "telegram_not_configured")

    session_file = Path(base_dir) / "tg_session.session"
    if not session_file.exists():
        return diagnostic_result("telegram", "warning", "telegram_session_missing")

    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{session_file.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        session = connection.execute(
            "SELECT dc_id FROM sessions LIMIT 1"
        ).fetchone()
    except sqlite3.Error as error:
        return diagnostic_result(
            "telegram",
            "error",
            "telegram_session_error",
            error=str(error),
        )
    finally:
        if connection is not None:
            connection.close()

    if not session:
        return diagnostic_result("telegram", "warning", "telegram_session_empty")
    return diagnostic_result(
        "telegram",
        "success",
        "telegram_session_ok",
        dc_id=int(session[0]),
    )


def run_diagnostics(config, base_dir=Path(".")):
    checks = (
        ("sqlite", lambda: check_sqlite(config)),
        ("discord", lambda: check_discord(config)),
        ("telegram", lambda: check_telegram(config, base_dir)),
        ("disk", lambda: check_disk(base_dir)),
        ("ffmpeg", check_ffmpeg),
    )
    results = []
    for component, check in checks:
        try:
            results.append(check())
        except Exception as error:
            results.append(
                diagnostic_result(
                    component,
                    "error",
                    "internal_error",
                    error=str(error),
                )
            )
    return results
