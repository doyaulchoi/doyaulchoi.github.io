#!/data/data/com.termux/files/usr/bin/bash

HOME_DIR="/data/data/com.termux/files/home"
APP_DIR="$HOME_DIR/light_loggg_tesla"
STATE_DIR="$HOME_DIR/light_loggg_tesla"
LOG_DIR="$STATE_DIR/logs"

POLLING_SCRIPT="$APP_DIR/light_loggg_tesla_polling.py"
BOT_SCRIPT="$APP_DIR/light_loggg_telegram_bot.py"
COMMAND_SERVER_SCRIPT="$APP_DIR/light_loggg_command_server.py"
COMMAND_SERVER_PID="$APP_DIR/command_server.pid"
COMMAND_SERVER_LOG="$LOG_DIR/command_server.log"

POLLING_PID="$STATE_DIR/polling.pid"
BOT_PID="$STATE_DIR/telegram_bot.pid"

ENV_FILE="$HOME_DIR/.light_loggg.env"
BOOT_LOG="$LOG_DIR/boot.log"
BOOT_ERR="$LOG_DIR/boot-error.log"

mkdir -p "$LOG_DIR"

{
  echo "==== LIGHT LOGGG BOOT START $(date) ===="

  sleep 30

  echo "[1] Waiting for DNS/network..."

  for i in $(seq 1 60); do
    python - <<'PY'
import socket, sys
hosts = [
    "api.telegram.org",
    "fleet-api.prd.na.vn.cloud.tesla.com",
]
for h in hosts:
    socket.gethostbyname(h)
sys.exit(0)
PY
    if [ $? -eq 0 ]; then
      echo "DNS/network ready"
      break
    fi

    echo "DNS not ready yet... $i"
    sleep 5
  done

  echo "[2] Acquiring wake lock..."
  termux-wake-lock || true

  echo "[3] Starting sshd..."
  sshd || true

  echo "[4] Checking app directory..."
  if [ ! -d "$APP_DIR" ]; then
    echo "APP_DIR not found: $APP_DIR"
    exit 1
  fi

  echo "[5] Checking scripts..."
  if [ ! -f "$POLLING_SCRIPT" ]; then
    echo "Polling script not found: $POLLING_SCRIPT"
    exit 1
  fi

  if [ ! -f "$BOT_SCRIPT" ]; then
    echo "Telegram bot script not found: $BOT_SCRIPT"
    exit 1
  fi

  if [ ! -f "$COMMAND_SERVER_SCRIPT" ]; then
    echo "Command server script not found: $COMMAND_SERVER_SCRIPT"
    exit 1
  fi

  echo "[6] Loading env..."
  if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
  else
    echo "ENV file not found: $ENV_FILE"
    exit 1
  fi

  echo "[7] Killing old processes..."
  pkill -f "light_loggg_tesla_polling.py" || true
  pkill -f "light_loggg_telegram_bot.py" || true
  pkill -f "light_loggg_command_server.py" || true

  sleep 2

  echo "[8] Removing old PID files..."
  rm -f "$POLLING_PID" "$BOT_PID" "$COMMAND_SERVER_PID"

  echo "[9] Starting polling..."
  cd "$APP_DIR" || exit 1
  nohup python "$POLLING_SCRIPT" >> "$LOG_DIR/polling.log" 2>&1 &
  echo $! > "$POLLING_PID"

  sleep 3

  echo "[10] Starting Telegram bot..."
  nohup python "$BOT_SCRIPT" >> "$LOG_DIR/telegram_bot.log" 2>&1 &
  echo $! > "$BOT_PID"

  sleep 2

  echo "[11] Starting command server..."
  if [ -f "$COMMAND_SERVER_SCRIPT" ]; then
    nohup python "$COMMAND_SERVER_SCRIPT" --daemon >> "$COMMAND_SERVER_LOG" 2>&1 &
    echo $! > "$COMMAND_SERVER_PID"
  else
    echo "Command server script not found: $COMMAND_SERVER_SCRIPT"
  fi

  echo "[12] PID files:"
  echo "polling pid: $(cat "$POLLING_PID" 2>/dev/null)"
  echo "bot pid: $(cat "$BOT_PID" 2>/dev/null)"
  echo "command server pid: $(cat "$COMMAND_SERVER_PID" 2>/dev/null)"

  echo "==== LIGHT LOGGG BOOT END $(date) ===="

} >> "$BOOT_LOG" 2>> "$BOOT_ERR"
