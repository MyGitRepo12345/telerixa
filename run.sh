#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

APP_NAME="Telerixa"
VENV_DIR=".venv-linux"
PYTHON_BIN=""

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
  python - "$exit_code" <<'PY' || true
import json
import sys
import urllib.request

exit_code = sys.argv[1]

try:
    with open("config.json", "r", encoding="utf-8-sig") as config_file:
        config = json.load(config_file)
except Exception:
    raise SystemExit(0)

webhook_url = config.get("DISCORD_WEBHOOK_URL")
if not webhook_url:
    raise SystemExit(0)

alert_user_id = str(config.get("DISCORD_ALERT_USER_ID", "")).strip()
mention = f"<@{alert_user_id}> " if alert_user_id else ""

payload = {
    "content": (
        f"{mention}WARNING: Telerixa stopped on Steam Deck.\n"
        f"Exit code: `{exit_code}`\n"
        "Check the Konsole window or `logs/bot.log`."
    )
}

data = json.dumps(payload).encode("utf-8")
request = urllib.request.Request(
    webhook_url,
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()
except Exception:
    pass
PY
}

echo "Updating pip tooling..."
python -m pip install --upgrade pip setuptools wheel

echo "Installing Python dependencies..."
python -m pip install -r requirements.txt

echo "Checking Europe/Berlin timezone support..."
if ! python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin')" >/dev/null 2>&1; then
  echo "System timezone data is missing; installing Python tzdata fallback..."
  python -m pip install tzdata
fi

echo
echo "Bot is starting. Keep this terminal open."
echo "Press Ctrl+C to stop."
echo

set +e
python "$(pwd)/telerixa.py"
BOT_EXIT_CODE=$?
set -e

echo
echo "Bot stopped with exit code $BOT_EXIT_CODE."

if [ "$BOT_EXIT_CODE" -ne 0 ]; then
  echo "Sending Discord crash notification..."
  notify_discord_exit "$BOT_EXIT_CODE"
fi

read -r -p "Press Enter to close..."
exit "$BOT_EXIT_CODE"
