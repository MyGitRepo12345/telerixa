#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

APP_NAME="Telerixa"
VENV_DIR=".venv-linux"
PYTHON_BIN=""
SCRIPT_DIR="$(pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$0")"

if [ ! -t 0 ] && [ "${TELERIXA_VISIBLE_TERMINAL:-0}" != "1" ]; then
  if command -v konsole >/dev/null 2>&1; then
    konsole \
      --workdir "$SCRIPT_DIR" \
      --hold \
      -e env TELERIXA_VISIBLE_TERMINAL=1 bash "$SCRIPT_PATH" \
      >/dev/null 2>&1 &
    exit 0
  fi
fi

echo "Starting $APP_NAME"
echo

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python is not installed or is not available in PATH."
  echo "On SteamOS Desktop Mode, install Python first, then run this script again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -f "telerixa.py" ]; then
  echo "telerixa.py was not found. Run this file from the bot folder."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -f "config.json" ]; then
  echo "config.json was not found. Put it next to telerixa.py before starting the bot."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ -d ".venv" ] && [ ! -f ".venv/bin/activate" ]; then
  echo "Found Windows-style .venv; SteamOS will use $VENV_DIR instead."
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating local virtual environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "$VENV_DIR exists, but it does not look like a Linux virtual environment."
  echo "Recreating $VENV_DIR..."
  rm -rf "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

notify_discord_exit() {
  exit_code="$1"
  python - "$exit_code" <<'PY'
import asyncio
import json
import sys

import aiohttp

exit_code = sys.argv[1]

async def main():
    try:
        with open("config.json", "r", encoding="utf-8-sig") as config_file:
            config = json.load(config_file)
    except Exception as exc:
        print(f"Could not read config.json for crash notification: {exc}")
        raise SystemExit(2)

    webhook_url = config.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is empty; crash notification skipped.")
        raise SystemExit(2)

    alert_user_id = str(config.get("DISCORD_ALERT_USER_ID", "")).strip()
    mention = f"<@{alert_user_id}> " if alert_user_id else ""

    payload = {
        "content": (
            f"{mention}WARNING: Telerixa stopped on Steam Deck.\n"
            f"Exit code: `{exit_code}`\n"
            "Check the Konsole window or `logs/bot.log`."
        )
    }
    if alert_user_id:
        payload["allowed_mentions"] = {"users": [alert_user_id]}

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, timeout=timeout) as response:
                body = await response.text()
                if response.status not in (200, 204):
                    print(f"Discord crash notification failed: HTTP {response.status}: {body[:500]}")
                    raise SystemExit(3)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Discord crash notification failed: {exc}")
        raise SystemExit(4)

    print("Discord crash notification sent.")

asyncio.run(main())
PY
}

echo "Installing Python dependencies..."
python -m pip install --disable-pip-version-check -r requirements.txt

echo "Checking Europe/Berlin timezone support..."
if ! python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin')" >/dev/null 2>&1; then
  echo "WARNING: Europe/Berlin timezone data is unavailable."
  echo "Telerixa will use the system local timezone fallback."
fi

echo
echo "Bot is starting. Keep this terminal open."
echo "Press Ctrl+C to stop."
echo

set +e
TELERIXA_OWNER_PID="$$" python "$(pwd)/telerixa.py"
BOT_EXIT_CODE=$?
set -e

echo
echo "Bot stopped with exit code $BOT_EXIT_CODE."

case "$BOT_EXIT_CODE" in
  0)
    ;;
  130|143)
    echo "Bot stopped by signal/control flow; crash notification skipped."
    ;;
  *)
    echo "Sending Discord crash notification..."
    if ! notify_discord_exit "$BOT_EXIT_CODE"; then
      echo "WARNING: Discord crash notification was not sent."
    fi
    ;;
esac

read -r -p "Press Enter to close..."
exit "$BOT_EXIT_CODE"
