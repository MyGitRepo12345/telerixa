#!/usr/bin/env sh
set -eu

TARGET_DIR="${1:-}"
TMP_DIR="${2:-/home/deck/.telerixa_deploy}"
START_BOT="${3:-1}"
DEPLOY_REMOTE_VERSION="20260708-telerixa-v0.2.5"

fail() {
  echo "ERROR: $1" >&2
  exit 1
}

stop_target_processes() {
  process_pattern="$1"
  pids="$(pgrep -f "$process_pattern" 2>/dev/null || true)"
  [ -n "$pids" ] || return 0

  echo "$pids" | while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    process_cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    if [ "$process_cwd" = "$TARGET_DIR" ]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}

start_bot_in_konsole() {
  KONSOLE_START_LOG="$TARGET_DIR/logs/konsole_start.log"
  : > "$KONSOLE_START_LOG"

  if ! command -v konsole >/dev/null 2>&1; then
    echo "konsole command was not found." >> "$KONSOLE_START_LOG"
    return 1
  fi

  uid="$(id -u)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$uid}"
  if [ ! -d "$runtime_dir" ]; then
    echo "Runtime dir was not found: $runtime_dir" >> "$KONSOLE_START_LOG"
    return 1
  fi

  display_value="${DISPLAY:-}"
  wayland_value="${WAYLAND_DISPLAY:-}"
  dbus_addr="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$runtime_dir/bus}"
  xauthority_value="${XAUTHORITY:-$HOME/.Xauthority}"

  echo "Looking for GUI session environment..." >> "$KONSOLE_START_LOG"
  for process_name in kwin_wayland plasmashell plasma_session dolphin konsole steam; do
    pids="$(pgrep -u "$uid" -x "$process_name" 2>/dev/null || true)"
    [ -n "$pids" ] || continue
    for env_pid in $pids; do
      env_file="/proc/$env_pid/environ"
      [ -r "$env_file" ] || continue

      env_display="$(tr '\0' '\n' < "$env_file" | sed -n 's/^DISPLAY=//p' | head -n 1)"
      env_wayland="$(tr '\0' '\n' < "$env_file" | sed -n 's/^WAYLAND_DISPLAY=//p' | head -n 1)"
      env_runtime="$(tr '\0' '\n' < "$env_file" | sed -n 's/^XDG_RUNTIME_DIR=//p' | head -n 1)"
      env_dbus="$(tr '\0' '\n' < "$env_file" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p' | head -n 1)"
      env_xauthority="$(tr '\0' '\n' < "$env_file" | sed -n 's/^XAUTHORITY=//p' | head -n 1)"

      [ -n "$env_runtime" ] && runtime_dir="$env_runtime"
      [ -n "$env_display" ] && display_value="$env_display"
      [ -n "$env_wayland" ] && wayland_value="$env_wayland"
      [ -n "$env_dbus" ] && dbus_addr="$env_dbus"
      [ -n "$env_xauthority" ] && xauthority_value="$env_xauthority"

      echo "Loaded GUI env from $process_name pid $env_pid." >> "$KONSOLE_START_LOG"
      break 2
    done
  done

  if [ -z "$wayland_value" ]; then
    wayland_path="$(find "$runtime_dir" -maxdepth 1 -type s -name 'wayland-*' -print 2>/dev/null | head -n 1)"
    if [ -n "$wayland_path" ]; then
      wayland_value="$(basename "$wayland_path")"
    fi
  fi

  qt_platform="xcb"
  if [ -n "$wayland_value" ] && [ -S "$runtime_dir/$wayland_value" ]; then
    qt_platform="wayland"
  elif [ -z "$display_value" ]; then
    echo "No usable Wayland socket or X11 DISPLAY was found." >> "$KONSOLE_START_LOG"
    echo "Runtime dir contents:" >> "$KONSOLE_START_LOG"
    ls -la "$runtime_dir" >> "$KONSOLE_START_LOG" 2>&1 || true
    return 1
  fi

  {
    echo "DEPLOY_REMOTE_VERSION=$DEPLOY_REMOTE_VERSION"
    echo "Trying to open Konsole at $(date)."
    echo "QT_QPA_PLATFORM=$qt_platform"
    echo "DISPLAY=$display_value"
    echo "WAYLAND_DISPLAY=$wayland_value"
    echo "XDG_RUNTIME_DIR=$runtime_dir"
    echo "DBUS_SESSION_BUS_ADDRESS=$dbus_addr"
    echo "XAUTHORITY=$xauthority_value"
  } >> "$KONSOLE_START_LOG"

  if command -v systemd-run >/dev/null 2>&1; then
    unit_name="telerixa-konsole-$(date +%s)"
    echo "Trying systemd-run --user: $unit_name" >> "$KONSOLE_START_LOG"
    if env \
      XDG_RUNTIME_DIR="$runtime_dir" \
      DBUS_SESSION_BUS_ADDRESS="$dbus_addr" \
      systemd-run --user --collect --unit="$unit_name" \
        --setenv=QT_QPA_PLATFORM="$qt_platform" \
        --setenv=DISPLAY="$display_value" \
        --setenv=WAYLAND_DISPLAY="$wayland_value" \
        --setenv=XDG_RUNTIME_DIR="$runtime_dir" \
        --setenv=DBUS_SESSION_BUS_ADDRESS="$dbus_addr" \
        --setenv=XAUTHORITY="$xauthority_value" \
        konsole --workdir "$TARGET_DIR" --hold -e bash -lc './run.sh' >> "$KONSOLE_START_LOG" 2>&1; then
      sleep 2
      if pgrep -af "[r]un[.]sh" >/dev/null 2>&1 || pgrep -af "telerixa[.]py" >/dev/null 2>&1; then
        return 0
      fi

      echo "systemd-run accepted the unit, but bot process did not appear." >> "$KONSOLE_START_LOG"
      if command -v systemctl >/dev/null 2>&1; then
        echo "--- systemctl --user status $unit_name ---" >> "$KONSOLE_START_LOG"
        env \
          XDG_RUNTIME_DIR="$runtime_dir" \
          DBUS_SESSION_BUS_ADDRESS="$dbus_addr" \
          systemctl --user status "$unit_name" --no-pager >> "$KONSOLE_START_LOG" 2>&1 || true
      fi
      if command -v journalctl >/dev/null 2>&1; then
        echo "--- journalctl --user -u $unit_name ---" >> "$KONSOLE_START_LOG"
        env \
          XDG_RUNTIME_DIR="$runtime_dir" \
          DBUS_SESSION_BUS_ADDRESS="$dbus_addr" \
          journalctl --user -u "$unit_name" --no-pager -n 80 >> "$KONSOLE_START_LOG" 2>&1 || true
      fi
    fi
  fi

  echo "Trying direct Konsole launch." >> "$KONSOLE_START_LOG"

  (
    cd "$TARGET_DIR"
    if command -v setsid >/dev/null 2>&1; then
      setsid env \
        QT_QPA_PLATFORM="$qt_platform" \
        DISPLAY="$display_value" \
        WAYLAND_DISPLAY="$wayland_value" \
        XDG_RUNTIME_DIR="$runtime_dir" \
        DBUS_SESSION_BUS_ADDRESS="$dbus_addr" \
        XAUTHORITY="$xauthority_value" \
        konsole --workdir "$TARGET_DIR" --hold -e bash -lc './run.sh' >> "$KONSOLE_START_LOG" 2>&1 &
    else
      env \
        QT_QPA_PLATFORM="$qt_platform" \
        DISPLAY="$display_value" \
        WAYLAND_DISPLAY="$wayland_value" \
        XDG_RUNTIME_DIR="$runtime_dir" \
        DBUS_SESSION_BUS_ADDRESS="$dbus_addr" \
        XAUTHORITY="$xauthority_value" \
        konsole --workdir "$TARGET_DIR" --hold -e bash -lc './run.sh' >> "$KONSOLE_START_LOG" 2>&1 &
    fi
  )

  sleep 1
  if pgrep -af "[r]un[.]sh" >/dev/null 2>&1 || pgrep -af "telerixa[.]py" >/dev/null 2>&1; then
    return 0
  fi

  echo "Konsole launch did not leave a visible run.sh or telerixa.py process." >> "$KONSOLE_START_LOG"
  return 1
}

if [ -z "$TARGET_DIR" ]; then
  fail "Target directory was not provided."
fi

[ -d "$TARGET_DIR" ] || fail "Target directory does not exist: $TARGET_DIR"
[ -f "$TARGET_DIR/config.json" ] || fail "config.json is missing in target. Refusing to deploy to a suspicious folder."

echo "Deploy remote script version: $DEPLOY_REMOTE_VERSION"

for file in telerixa.py i18n.py web_ui.py requirements.txt run.sh run_ui.sh; do
  [ -f "$TMP_DIR/$file" ] || fail "Missing staged file: $TMP_DIR/$file"
done

[ -f "$TMP_DIR/locales/en.json" ] || fail "Missing staged file: $TMP_DIR/locales/en.json"
[ -f "$TMP_DIR/locales/ru.json" ] || fail "Missing staged file: $TMP_DIR/locales/ru.json"
[ -f "$TMP_DIR/telerixa_core/__init__.py" ] || fail "Missing staged file: $TMP_DIR/telerixa_core/__init__.py"
[ -f "$TMP_DIR/telerixa_core/constants.py" ] || fail "Missing staged file: $TMP_DIR/telerixa_core/constants.py"
[ -f "$TMP_DIR/telerixa_core/formatting.py" ] || fail "Missing staged file: $TMP_DIR/telerixa_core/formatting.py"
[ -f "$TMP_DIR/telerixa_core/logging_setup.py" ] || fail "Missing staged file: $TMP_DIR/telerixa_core/logging_setup.py"
[ -f "$TMP_DIR/telerixa_core/models.py" ] || fail "Missing staged file: $TMP_DIR/telerixa_core/models.py"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CHECK_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CHECK_BIN="python"
else
  fail "Python was not found on Steam Deck."
fi

echo "Validating staged Python syntax on Steam Deck..."
"$PYTHON_CHECK_BIN" -m py_compile "$TMP_DIR/telerixa.py" "$TMP_DIR/i18n.py" "$TMP_DIR/web_ui.py" "$TMP_DIR/telerixa_core/__init__.py" "$TMP_DIR/telerixa_core/constants.py" "$TMP_DIR/telerixa_core/formatting.py" "$TMP_DIR/telerixa_core/logging_setup.py" "$TMP_DIR/telerixa_core/models.py"

echo "Stopping running bot/UI processes if they exist..."
stop_target_processes "[p]ython[0-9.]* .*telerixa[.]py"
stop_target_processes "[p]ython[0-9.]* .*Script[.]py"
stop_target_processes "[p]ython[0-9.]* .*web_ui[.]py"
stop_target_processes "[r]un[.]sh"
stop_target_processes "[r]un_ui[.]sh"
sleep 1

BACKUP_DIR="$TARGET_DIR/.deploy_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "Rotating deploy backups, keeping last 5..."
find "$TARGET_DIR" -maxdepth 1 -type d -name ".deploy_backup_*" \
  | sort -r \
  | tail -n +6 \
  | while IFS= read -r old_backup; do
      rm -rf "$old_backup"
    done

echo "Backing up current code to: $BACKUP_DIR"
for file in Script.py telerixa.py i18n.py web_ui.py requirements.txt run.sh run_ui.sh; do
  if [ -f "$TARGET_DIR/$file" ]; then
    cp "$TARGET_DIR/$file" "$BACKUP_DIR/$file"
  fi
done
if [ -d "$TARGET_DIR/locales" ]; then
  cp -R "$TARGET_DIR/locales" "$BACKUP_DIR/locales"
fi
if [ -d "$TARGET_DIR/telerixa_core" ]; then
  cp -R "$TARGET_DIR/telerixa_core" "$BACKUP_DIR/telerixa_core"
fi

echo "Installing new code..."
for file in telerixa.py i18n.py web_ui.py requirements.txt run.sh run_ui.sh; do
  cp "$TMP_DIR/$file" "$TARGET_DIR/$file"
done
rm -rf "$TARGET_DIR/locales"
cp -R "$TMP_DIR/locales" "$TARGET_DIR/locales"
rm -rf "$TARGET_DIR/telerixa_core"
cp -R "$TMP_DIR/telerixa_core" "$TARGET_DIR/telerixa_core"

rm -f "$TARGET_DIR/Script.py"
chmod +x "$TARGET_DIR/run.sh" "$TARGET_DIR/run_ui.sh"

echo "Validating Python syntax on Steam Deck..."
"$PYTHON_CHECK_BIN" -m py_compile "$TARGET_DIR/telerixa.py" "$TARGET_DIR/i18n.py" "$TARGET_DIR/web_ui.py" "$TARGET_DIR/telerixa_core/__init__.py" "$TARGET_DIR/telerixa_core/constants.py" "$TARGET_DIR/telerixa_core/formatting.py" "$TARGET_DIR/telerixa_core/logging_setup.py" "$TARGET_DIR/telerixa_core/models.py"

rm -rf "$TMP_DIR"

if [ "$START_BOT" = "1" ]; then
  mkdir -p "$TARGET_DIR/logs"
  echo "Starting bot in a visible Konsole window..."
  if ! start_bot_in_konsole; then
    echo "WARNING: could not open Konsole from SSH."
    echo "Details:"
    echo "  cat '$TARGET_DIR/logs/konsole_start.log'"
    echo "Bot was not started automatically. Start it on Steam Deck with:"
    echo "  cd '$TARGET_DIR' && ./run.sh"
  fi
  sleep 2

  if pgrep -af "telerixa.py" >/dev/null 2>&1; then
    echo "Bot process is running:"
    pgrep -af "telerixa.py" || true
  elif pgrep -af "run.sh" >/dev/null 2>&1; then
    echo "Bot launcher is running; telerixa.py should appear after dependency checks:"
    pgrep -af "run.sh" || true
  else
    echo "WARNING: bot process is not visible yet."
    echo "Check bot log on Steam Deck:"
    echo "  tail -n 80 '$TARGET_DIR/logs/bot.log'"
    echo "Or start it manually:"
    echo "  cd '$TARGET_DIR' && ./run.sh"
  fi
else
  echo "Bot autostart skipped."
fi

echo "Deploy complete."
echo "Target: $TARGET_DIR"
echo "Runtime files were not touched: config.json, bot_state.db, tg_session.session, logs/"
