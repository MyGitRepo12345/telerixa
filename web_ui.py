from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
import json
import logging
import os
import re
import socket
import sqlite3
import tempfile
import webbrowser
from logging.handlers import RotatingFileHandler

from i18n import configure_language, get_language_options, normalize_language, tr


CONFIG_FILE = Path("config.json")
LOG_DIR = Path("logs")
UI_LOG_FILE = LOG_DIR / "ui.log"
BOT_LOG_FILE = LOG_DIR / "bot.log"
HOST = os.environ.get("TG_FORWARDER_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("TG_FORWARDER_UI_PORT", "8765"))
DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = DB_TIMEOUT_SECONDS * 1000

CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)

LARGE_FILE_ACTION_KEYS = (
    "send_text_link",
    "try_send_then_text",
    "skip_post",
)


class UIConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_large_file_action_options():
    return {
        action_key: tr(f"ui.action.{action_key}")
        for action_key in LARGE_FILE_ACTION_KEYS
    }


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger("web_ui")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        UI_LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


UI_LOGGER = setup_logging()


def log_event(message):
    UI_LOGGER.info(message)


def open_default_browser():
    if os.environ.get("TG_FORWARDER_NO_BROWSER") == "1":
        log_event("Browser auto-open is disabled by TG_FORWARDER_NO_BROWSER=1.")
        return

    url = f"http://{HOST}:{PORT}/"
    try:
        opened = webbrowser.open(url, new=2)
    except Exception as e:
        log_event(f"Could not open browser automatically: {e}")
        return

    if opened:
        log_event(f"Opened default browser: {url}")
    else:
        log_event(f"Could not open browser automatically. Open manually: {url}")


def default_config():
    return {
        "DISCORD_WEBHOOK_URL": "",
        "TELEGRAM_API_ID": 0,
        "TELEGRAM_API_HASH": "",
        "TELEGRAM_CHANNELS": [],
        "LANGUAGE": "ru",
        "CHECK_INTERVAL": 60,
        "MAX_MESSAGE_LENGTH": 2000,
        "TIMEZONE": "Europe/Berlin",
        "DISCORD_FILE_LIMIT_MB": 25,
        "LARGE_FILE_ACTION": "send_text_link",
        "STARTUP_CATCH_UP_LIMIT": 10,
        "MAX_QUEUE_ATTEMPTS": 24,
    }


def load_config():
    config = default_config()
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
            if isinstance(loaded, dict):
                config.update(loaded)
    except FileNotFoundError:
        pass
    config["LANGUAGE"] = normalize_language(config.get("LANGUAGE", "ru"))
    return config


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(CONFIG_FILE.parent),
        delete=False,
    ) as tmp:
        json.dump(config, tmp, ensure_ascii=False, indent=4)
        tmp.write("\n")
        tmp_name = tmp.name

    os.replace(tmp_name, CONFIG_FILE)


def normalize_channel(raw_channel):
    channel = raw_channel.strip()
    if not channel:
        return ""

    lower_channel = channel.lower()
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if lower_channel.startswith(prefix):
            channel = channel[len(prefix):]
            break

    channel = channel.strip().lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [part for part in channel.split("/") if part]

    if len(parts) >= 2 and parts[0].lower() == "s":
        return parts[1]
    if parts:
        return parts[0]

    return channel


def parse_channels(raw_channels):
    channels = []
    invalid_items = []
    duplicate_items = []
    seen = set()

    for raw_line in raw_channels.replace(",", "\n").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        channel = normalize_channel(raw_line)
        if not channel or not CHANNEL_RE.fullmatch(channel):
            invalid_items.append(raw_line)
            continue

        channel_key = channel.lower()
        if channel_key not in seen:
            channels.append(channel)
            seen.add(channel_key)
        else:
            duplicate_items.append(raw_line)

    return channels, invalid_items, duplicate_items


def parse_file_limit(raw_value):
    raw_value = str(raw_value).strip()
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.file_limit_int")

    if limit < 1 or limit > 500:
        return None, tr("ui.file_limit_range")

    return limit, ""


def parse_catch_up_limit(raw_value):
    raw_value = str(raw_value).strip()
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.catch_up_int")

    if limit < 0 or limit > 500:
        return None, tr("ui.catch_up_range")

    return limit, ""


def parse_max_queue_attempts(raw_value):
    raw_value = str(raw_value).strip()
    try:
        attempts = int(raw_value)
    except (TypeError, ValueError):
        return None, tr("ui.max_queue_attempts_int")

    if attempts < 1 or attempts > 200:
        return None, tr("ui.max_queue_attempts_range")

    return attempts, ""


def validate_webhook(webhook):
    if not webhook:
        return tr("ui.webhook_empty")

    if not webhook.startswith(DISCORD_WEBHOOK_PREFIXES):
        return tr("ui.webhook_prefix")

    return ""


def form_values_from_config(config):
    return {
        "discord_webhook_url": config.get("DISCORD_WEBHOOK_URL", ""),
        "telegram_channels": "\n".join(config.get("TELEGRAM_CHANNELS", [])),
        "language": normalize_language(config.get("LANGUAGE", "ru")),
        "discord_file_limit_mb": str(config.get("DISCORD_FILE_LIMIT_MB", 25)),
        "large_file_action": config.get("LARGE_FILE_ACTION", "send_text_link"),
        "startup_catch_up_limit": str(config.get("STARTUP_CATCH_UP_LIMIT", 10)),
        "max_queue_attempts": str(config.get("MAX_QUEUE_ATTEMPTS", 24)),
    }


def render_select(name, current_value, options):
    items = []
    for value, label in options.items():
        selected = " selected" if value == current_value else ""
        items.append(
            f'<option value="{escape(value)}"{selected}>{escape(label)}</option>'
        )
    return f'<select id="{escape(name)}" name="{escape(name)}">{"".join(items)}</select>'


def connect_db(db_file):
    conn = sqlite3.connect(
        db_file,
        timeout=DB_TIMEOUT_SECONDS,
        factory=UIConnection,
    )
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_pending_count(config):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return None

    try:
        with connect_db(db_file) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_messages"
            ).fetchone()
    except sqlite3.Error:
        return None

    return row[0] if row else 0


def get_pending_items(config, limit=10):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return []

    try:
        with connect_db(db_file) as conn:
            rows = conn.execute(
                """
                SELECT channel,
                       message_id,
                       grouped_id,
                       attempts,
                       next_retry_at,
                       next_retry_ts,
                       last_error
                FROM pending_messages
                ORDER BY next_retry_ts ASC, created_ts ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
    except sqlite3.Error:
        return []

    return rows


def clear_pending_queue(config):
    db_file = Path(config.get("STATE_DB_FILE", "bot_state.db"))
    if not db_file.exists():
        return 0

    now = datetime.now().astimezone()
    with connect_db(db_file) as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_messages").fetchone()
        deleted_count = row[0] if row else 0
        conn.execute(
            """
            UPDATE processed_messages
            SET status = 'dropped',
                processed_at = ?,
                processed_ts = ?
            WHERE status = 'queued'
            """,
            (now.isoformat(), now.timestamp()),
        )
        conn.execute("DELETE FROM pending_messages")
        conn.commit()

    return deleted_count


def read_log_tail(path, max_lines=30, levels=None):
    if not path.exists():
        return []

    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []

    if levels:
        level_markers = tuple(f" {level}:" for level in levels)
        lines = [line for line in lines if any(marker in line for marker in level_markers)]

    return lines[-max_lines:]


def render_log_panel(title, lines):
    if lines:
        body = "\n".join(escape(line) for line in lines)
    else:
        body = tr("ui.no_log_entries")

    return f"""
        <div class="log-panel">
          <h2>{escape(title)}</h2>
          <pre>{body}</pre>
        </div>
    """


def render_queue_panel(pending_count, pending_items):
    count_text = "-" if pending_count is None else str(pending_count)
    disabled = " disabled" if not pending_count else ""
    now_ts = datetime.now().astimezone().timestamp()

    if pending_items:
        item_rows = []
        for channel, message_id, grouped_id, attempts, next_retry_at, next_retry_ts, last_error in pending_items:
            retry_delay = ""
            if next_retry_ts is not None:
                seconds = max(0, int(float(next_retry_ts) - now_ts))
                retry_delay = tr("ui.retry_in_seconds", seconds=seconds)
            retry_text = next_retry_at or retry_delay or "-"
            grouped_text = tr("ui.album_suffix", grouped_id=grouped_id) if grouped_id else ""
            error_text = str(last_error or "").strip()
            if len(error_text) > 220:
                error_text = error_text[:217] + "..."
            item_rows.append(
                "<li>"
                f"<strong>@{escape(str(channel))}/{escape(str(message_id))}</strong>"
                f"<span>{escape(tr('ui.queue_attempts', attempts=attempts, grouped_text=grouped_text))}</span>"
                f"<span>{escape(tr('ui.queue_next_attempt', retry_text=retry_text))}</span>"
                f"<span>{escape(tr('ui.queue_error', error=error_text or '-'))}</span>"
                "</li>"
            )
        pending_html = f'<ul class="queue-items">{"".join(item_rows)}</ul>'
    else:
        pending_html = f'<p class="queue-empty">{escape(tr("ui.queue_empty"))}</p>'

    return f"""
    <section class="queue-panel">
      <div class="queue-head">
        <h2>{escape(tr("ui.queue_title"))}</h2>
        <p>{escape(tr("ui.queue_count", count=count_text))}</p>
      </div>
      <form method="post" action="/clear-queue">
        <button class="danger-button" type="submit"{disabled}>{escape(tr("ui.clear_queue"))}</button>
      </form>
      {pending_html}
    </section>
    """


def redact_webhook(webhook):
    if not webhook:
        return "(empty)"

    parts = webhook.split("/")
    if len(parts) >= 7:
        return f"{parts[0]}//{parts[2]}/.../{parts[5]}/***"

    return "***"


def describe_config_changes(old_config, new_config):
    changes = []

    if old_config.get("DISCORD_WEBHOOK_URL", "") != new_config.get("DISCORD_WEBHOOK_URL", ""):
        changes.append(
            "Discord webhook changed: "
            f"{redact_webhook(old_config.get('DISCORD_WEBHOOK_URL', ''))} -> "
            f"{redact_webhook(new_config.get('DISCORD_WEBHOOK_URL', ''))}"
        )

    old_channels = old_config.get("TELEGRAM_CHANNELS", [])
    new_channels = new_config.get("TELEGRAM_CHANNELS", [])
    added_channels = [channel for channel in new_channels if channel not in old_channels]
    removed_channels = [channel for channel in old_channels if channel not in new_channels]

    if added_channels:
        changes.append(f"Telegram channels added: {', '.join(added_channels)}")
    if removed_channels:
        changes.append(f"Telegram channels removed: {', '.join(removed_channels)}")

    old_limit = old_config.get("DISCORD_FILE_LIMIT_MB", 25)
    new_limit = new_config.get("DISCORD_FILE_LIMIT_MB", 25)
    if old_limit != new_limit:
        changes.append(f"Discord file limit changed: {old_limit} MB -> {new_limit} MB")

    old_action = old_config.get("LARGE_FILE_ACTION", "send_text_link")
    new_action = new_config.get("LARGE_FILE_ACTION", "send_text_link")
    if old_action != new_action:
        action_options = get_large_file_action_options()
        changes.append(
            "Large file action changed: "
            f"{action_options.get(old_action, old_action)} -> "
            f"{action_options.get(new_action, new_action)}"
        )

    old_language = normalize_language(old_config.get("LANGUAGE", "ru"))
    new_language = normalize_language(new_config.get("LANGUAGE", "ru"))
    if old_language != new_language:
        changes.append(tr("ui.change_language", old=old_language, new=new_language))

    old_catch_up = old_config.get("STARTUP_CATCH_UP_LIMIT", 10)
    new_catch_up = new_config.get("STARTUP_CATCH_UP_LIMIT", 10)
    if old_catch_up != new_catch_up:
        changes.append(f"Startup catch-up changed: {old_catch_up} -> {new_catch_up} posts")

    old_max_attempts = old_config.get("MAX_QUEUE_ATTEMPTS", 24)
    new_max_attempts = new_config.get("MAX_QUEUE_ATTEMPTS", 24)
    if old_max_attempts != new_max_attempts:
        changes.append(f"Queue retry limit changed: {old_max_attempts} -> {new_max_attempts} attempts")

    return changes


def render_page(config, notice="", error="", form_values=None):
    selected_language = normalize_language(
        (form_values or {}).get("language") or config.get("LANGUAGE", "ru")
    )
    configure_language(selected_language)
    values = form_values or form_values_from_config(config)
    values["language"] = selected_language
    action = values.get("large_file_action", "send_text_link")
    if action not in LARGE_FILE_ACTION_KEYS:
        action = "send_text_link"

    pending_count = get_pending_count(config)
    pending_items = get_pending_items(config)
    queue_text = (
        tr("ui.queue_status_unknown")
        if pending_count is None
        else tr("ui.queue_status", count=pending_count)
    )
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    action_select = render_select("large_file_action", action, get_large_file_action_options())
    language_select = render_select("language", selected_language, get_language_options())
    queue_panel_html = render_queue_panel(pending_count, pending_items)
    ui_log_html = render_log_panel(tr("ui.events_ui"), read_log_tail(UI_LOG_FILE, max_lines=25))
    bot_log_html = render_log_panel(
        tr("ui.events_bot"),
        read_log_tail(BOT_LOG_FILE, max_lines=35),
    )

    return f"""<!doctype html>
<html lang="{escape(selected_language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telerixa</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f5f7f8;
      --panel: #ffffff;
      --text: #1f2a2e;
      --muted: #5b6b72;
      --line: #d9e1e5;
      --accent: #27745f;
      --accent-strong: #1f5f4e;
      --danger: #9f2d2d;
      --notice-bg: #e8f5ef;
      --error-bg: #fae9e9;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #15191b;
        --panel: #202628;
        --text: #eef3f4;
        --muted: #a7b4b9;
        --line: #344044;
        --accent: #53b99b;
        --accent-strong: #6dc8ad;
        --danger: #f08282;
        --notice-bg: #193c33;
        --error-bg: #422121;
      }}
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}

    main {{
      width: min(920px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 44px;
    }}

    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 22px;
    }}

    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.15;
      font-weight: 720;
    }}

    .subtitle {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
    }}

    .status {{
      flex: 0 0 auto;
      color: var(--muted);
      font-size: 13px;
      text-align: right;
    }}

    form {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
    }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}

    .field {{
      display: flex;
      min-width: 0;
      flex-direction: column;
      gap: 7px;
    }}

    .field.full {{
      grid-column: 1 / -1;
    }}

    label {{
      font-size: 13px;
      font-weight: 650;
    }}

    input,
    textarea,
    select {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: transparent;
      color: var(--text);
      font: inherit;
      font-size: 15px;
      padding: 10px 11px;
      outline: none;
    }}

    textarea {{
      min-height: 150px;
      resize: vertical;
      line-height: 1.45;
    }}

    input:focus,
    textarea:focus,
    select:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent);
    }}

    .hint {{
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}

    .actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      margin-top: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
    }}

    button {{
      min-height: 42px;
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      color: #ffffff;
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      padding: 10px 16px;
    }}

    button:hover {{
      background: var(--accent-strong);
    }}

    button:disabled {{
      cursor: default;
      opacity: 0.5;
    }}

    .danger-button {{
      background: var(--danger);
    }}

    .danger-button:hover {{
      background: color-mix(in srgb, var(--danger) 84%, #000000);
    }}

    .notice,
    .error {{
      margin-bottom: 16px;
      border-radius: 7px;
      padding: 11px 13px;
      font-size: 14px;
      line-height: 1.45;
    }}

    .notice {{
      background: var(--notice-bg);
      color: var(--accent-strong);
    }}

    .error {{
      background: var(--error-bg);
      color: var(--danger);
    }}

    .logs {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      margin-top: 18px;
    }}

    .queue-panel {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
      gap: 14px 16px;
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 16px;
    }}

    .queue-head h2 {{
      margin: 0 0 4px;
      font-size: 14px;
      line-height: 1.2;
    }}

    .queue-head p,
    .queue-empty {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }}

    .queue-panel form {{
      margin: 0;
      border: 0;
      background: transparent;
      padding: 0;
    }}

    .queue-items {{
      grid-column: 1 / -1;
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}

    .queue-items li {{
      display: grid;
      gap: 3px;
      border-top: 1px solid var(--line);
      padding-top: 9px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}

    .queue-items strong {{
      color: var(--text);
      font-size: 13px;
    }}

    .queue-items span {{
      min-width: 0;
    }}

    .log-panel {{
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}

    .log-panel h2 {{
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.2;
    }}

    .log-panel pre {{
      width: 100%;
      min-height: 180px;
      max-height: 320px;
      margin: 0;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.45;
    }}

    @media (max-width: 720px) {{
      main {{
        width: min(100vw - 20px, 920px);
        padding-top: 18px;
      }}

      header {{
        display: block;
      }}

      .status {{
        margin-top: 10px;
        text-align: left;
      }}

      form {{
        padding: 16px;
      }}

      .grid {{
        grid-template-columns: 1fr;
      }}

      .logs {{
        grid-template-columns: 1fr;
      }}

      .queue-panel {{
        grid-template-columns: 1fr;
      }}

      h1 {{
        font-size: 24px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Telerixa</h1>
        <p class="subtitle">{escape(tr("ui.subtitle"))}</p>
      </div>
      <div class="status">UI: {escape(HOST)}:{PORT}<br>{escape(queue_text)}</div>
    </header>

    {notice_html}
    {error_html}

    <form method="post" action="/">
      <div class="grid">
        <div class="field full">
          <label for="discord_webhook_url">Discord webhook</label>
          <input id="discord_webhook_url" name="discord_webhook_url" type="url" autocomplete="off" value="{escape(values.get("discord_webhook_url", ""))}">
          <div class="hint">{escape(tr("ui.discord_webhook_hint"))}</div>
        </div>

        <div class="field full">
          <label for="telegram_channels">{escape(tr("ui.telegram_channels"))}</label>
          <textarea id="telegram_channels" name="telegram_channels" spellcheck="false">{escape(values.get("telegram_channels", ""))}</textarea>
          <div class="hint">{escape(tr("ui.telegram_channels_hint"))}</div>
        </div>

        <div class="field">
          <label for="discord_file_limit_mb">{escape(tr("ui.discord_file_limit"))}</label>
          <input id="discord_file_limit_mb" name="discord_file_limit_mb" type="number" min="1" max="500" step="1" value="{escape(values.get("discord_file_limit_mb", ""))}">
          <div class="hint">{escape(tr("ui.discord_file_limit_hint"))}</div>
        </div>

        <div class="field">
          <label for="large_file_action">{escape(tr("ui.large_videos"))}</label>
          {action_select}
          <div class="hint">{escape(tr("ui.large_videos_hint"))}</div>
        </div>

        <div class="field">
          <label for="startup_catch_up_limit">{escape(tr("ui.startup_catch_up"))}</label>
          <input id="startup_catch_up_limit" name="startup_catch_up_limit" type="number" min="0" max="500" step="1" value="{escape(values.get("startup_catch_up_limit", ""))}">
          <div class="hint">{escape(tr("ui.startup_catch_up_hint"))}</div>
        </div>

        <div class="field">
          <label for="max_queue_attempts">{escape(tr("ui.max_queue_attempts"))}</label>
          <input id="max_queue_attempts" name="max_queue_attempts" type="number" min="1" max="200" step="1" value="{escape(values.get("max_queue_attempts", ""))}">
          <div class="hint">{escape(tr("ui.max_queue_attempts_hint"))}</div>
        </div>

        <div class="field">
          <label for="language">{escape(tr("ui.language"))}</label>
          {language_select}
          <div class="hint">{escape(tr("ui.language_hint"))}</div>
        </div>
      </div>

      <div class="actions">
        <button type="submit">{escape(tr("ui.save"))}</button>
      </div>
    </form>

    {queue_panel_html}

    <section class="logs">
      {ui_log_html}
      {bot_log_html}
    </section>
  </main>
</body>
</html>"""


class SingleInstanceHTTPServer(HTTPServer):
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ConfigHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return

        log_event(f"Settings page opened from {self.client_address[0]}")
        self.respond(render_page(load_config()))

    def do_POST(self):
        if self.path == "/clear-queue":
            self.handle_clear_queue()
            return

        if self.path != "/":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        config = load_config()

        form_values = {
            "discord_webhook_url": form.get("discord_webhook_url", [""])[0].strip(),
            "telegram_channels": form.get("telegram_channels", [""])[0],
            "language": normalize_language(form.get("language", [config.get("LANGUAGE", "ru")])[0]),
            "discord_file_limit_mb": form.get("discord_file_limit_mb", [""])[0].strip(),
            "large_file_action": form.get("large_file_action", ["send_text_link"])[0],
            "startup_catch_up_limit": form.get("startup_catch_up_limit", ["10"])[0].strip(),
            "max_queue_attempts": form.get("max_queue_attempts", ["24"])[0].strip(),
        }
        configure_language(form_values["language"])

        errors = []
        webhook = form_values["discord_webhook_url"]
        webhook_error = validate_webhook(webhook)
        if webhook_error:
            errors.append(webhook_error)

        channels, invalid_channels, duplicate_channels = parse_channels(form_values["telegram_channels"])
        if invalid_channels:
            invalid_list = ", ".join(invalid_channels[:5])
            if len(invalid_channels) > 5:
                invalid_list += tr("ui.more_items", count=len(invalid_channels) - 5)
            errors.append(
                tr("ui.invalid_channels", items=invalid_list)
            )
        if duplicate_channels:
            duplicate_list = ", ".join(duplicate_channels[:5])
            if len(duplicate_channels) > 5:
                duplicate_list += tr("ui.more_items", count=len(duplicate_channels) - 5)
            errors.append(
                tr("ui.duplicate_channels", items=duplicate_list)
            )
        if not channels:
            errors.append(tr("ui.no_channels"))

        limit, limit_error = parse_file_limit(form_values["discord_file_limit_mb"])
        if limit_error:
            errors.append(limit_error)

        action = form_values["large_file_action"]
        if action not in LARGE_FILE_ACTION_KEYS:
            errors.append(tr("ui.unknown_large_file_action"))

        catch_up_limit, catch_up_error = parse_catch_up_limit(form_values["startup_catch_up_limit"])
        if catch_up_error:
            errors.append(catch_up_error)

        max_queue_attempts, max_queue_attempts_error = parse_max_queue_attempts(form_values["max_queue_attempts"])
        if max_queue_attempts_error:
            errors.append(max_queue_attempts_error)

        if errors:
            error_text = " ".join(errors)
            log_event(f"Save rejected: {error_text}")
            self.respond(render_page(config, error=error_text, form_values=form_values))
            return

        new_config = dict(config)
        new_config["LANGUAGE"] = form_values["language"]
        new_config["DISCORD_WEBHOOK_URL"] = webhook
        new_config["TELEGRAM_CHANNELS"] = channels
        new_config["DISCORD_FILE_LIMIT_MB"] = limit
        new_config["LARGE_FILE_ACTION"] = action
        new_config["STARTUP_CATCH_UP_LIMIT"] = catch_up_limit
        new_config["MAX_QUEUE_ATTEMPTS"] = max_queue_attempts

        changes = describe_config_changes(config, new_config)
        save_config(new_config)

        if changes:
            log_event("Settings saved:")
            for change in changes:
                log_event(f"  - {change}")
        else:
            log_event("Settings saved: no changes.")

        self.respond(render_page(new_config, notice=tr("ui.saved_notice")))

    def handle_clear_queue(self):
        config = load_config()

        try:
            deleted_count = clear_pending_queue(config)
        except sqlite3.Error as e:
            configure_language(config.get("LANGUAGE", "ru"))
            error_text = tr("ui.clear_queue_failed", error=e)
            log_event(error_text)
            self.respond(render_page(config, error=error_text))
            return

        log_event(f"Pending queue cleared: {deleted_count} messages.")
        configure_language(config.get("LANGUAGE", "ru"))
        self.respond(render_page(config, notice=tr("ui.queue_cleared", count=deleted_count)))

    def log_message(self, format, *args):
        return

    def respond(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    try:
        server = SingleInstanceHTTPServer((HOST, PORT), ConfigHandler)
    except OSError as e:
        log_event(f"Cannot start settings UI at http://{HOST}:{PORT}: {e}")
        log_event("Port is already busy. Close the existing UI window/process and start again.")
        return

    log_event(f"Settings UI is running at http://{HOST}:{PORT}")
    open_default_browser()
    log_event("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("Settings UI stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
