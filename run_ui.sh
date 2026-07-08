#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

UI_URL="http://127.0.0.1:8765/"
PYTHON_BIN=""

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python is not installed or is not available in PATH."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ -f ".venv-linux/bin/activate" ]; then
  source ".venv-linux/bin/activate"
fi

open_default_browser() {
  if [ "${TG_FORWARDER_NO_BROWSER:-}" = "1" ]; then
    return
  fi

  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$UI_URL" >/dev/null 2>&1 &
  elif command -v gio >/dev/null 2>&1; then
    gio open "$UI_URL" >/dev/null 2>&1 &
  else
    "$PYTHON_BIN" -m webbrowser "$UI_URL" >/dev/null 2>&1 &
  fi
}

if "$PYTHON_BIN" -c "import socket; s=socket.socket(); s.settimeout(0.5); raise SystemExit(0 if s.connect_ex(('127.0.0.1', 8765)) == 0 else 1)" >/dev/null 2>&1; then
  echo "Settings UI is already running at $UI_URL"
  echo "Opening default browser..."
  open_default_browser
  read -r -p "Press Enter to close..."
  exit 0
fi

echo "Starting settings UI at $UI_URL"
echo "Press Ctrl+C to stop."
echo

"$PYTHON_BIN" web_ui.py
