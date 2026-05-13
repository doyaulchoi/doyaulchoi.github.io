#!/data/data/com.termux/files/usr/bin/bash

# LIGHT LOGGG Termux:Boot startup script
#
# 설치 위치:
#   ~/.termux/boot/start-light-loggg.sh
#
# GitHub 원본:
#   start-light-loggg.sh
#
# 역할:
# - 부팅 후 네트워크/DNS 준비 대기
# - termux-wake-lock 유지
# - sshd 실행
# - public config 확인
# - ~/.light_loggg.env 로드
# - 기존 polling/bot 프로세스 종료
# - ~/light_loggg_tesla 기준으로 polling + Telegram bot 시작
# - PID 파일 저장
#
# 주의:
# - token/secret은 이 파일에 넣지 않는다.
# - 민감값은 ~/.light_loggg.env에만 둔다.
# - 공개 설정은 ~/light_loggg_tesla/light_loggg_public_config.json에 둔다.

HOME_DIR="/data/data/com.termux/files/home"

APP_DIR="$HOME_DIR/light_loggg_tesla"
STATE_DIR="$APP_DIR"
LOG_DIR="$APP_DIR/logs"

PUBLIC_CONFIG_FILE="$APP_DIR/light_loggg_public_config.json"

POLLING_SCRIPT="$APP_DIR/light_loggg_tesla_polling.py"
BOT_SCRIPT="$APP_DIR/light_loggg_telegram_bot.py"

POLLING_PID="$STATE_DIR/polling.pid"
BOT_PID="$STATE_DIR/telegram_bot.pid"

ENV_FILE="$HOME_DIR/.light_loggg.env"

BOOT_LOG="$LOG_DIR/boot.log"
BOOT_ERR="$LOG_DIR/boot-error.log"

mkdir -p "$LOG_DIR"

{
  echo "==== LIGHT LOGGG BOOT START $(date) ===="

  echo "[0] Basic paths"
  echo "HOME_DIR=$HOME_DIR"
  echo "APP_DIR=$APP_DIR"
  echo "LOG_DIR=$LOG_DIR"
  echo "PUBLIC_CONFIG_FILE=$PUBLIC_CONFIG_FILE"
  echo "POLLING_SCRIPT=$POLLING_SCRIPT"
  echo "BOT_SCRIPT=$BOT_SCRIPT"
  echo "ENV_FILE=$ENV_FILE"
  echo "POLLING_PID=$POLLING_PID"
  echo "BOT_PID=$BOT_PID"

  echo "[1] Initial boot delay"
  sleep 30

  echo "[2] Waiting for DNS/network"

  NETWORK_READY=0

  for i in $(seq 1 60); do
    python - <<'PY'
import socket

hosts = [
    "api.telegram.org",
    "fleet-api.prd.na.vn.cloud.tesla.com",
]

for host in hosts:
    socket.gethostbyname(host)
PY

    if [ $? -eq 0 ]; then
      NETWORK_READY=1
      echo "DNS/network ready"
      break
    fi

    echo "DNS not ready yet... attempt=$i"
    sleep 5
  done

  if [ "$NETWORK_READY" != "1" ]; then
    echo "DNS/network not ready after timeout. Continue anyway."
  fi

  echo "[3] Acquiring wake lock"

  if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || true
    echo "termux-wake-lock requested"
  else
    echo "termux-wake-lock command not found"
  fi

  echo "[4] Starting sshd"

  if command -v sshd >/dev/null 2>&1; then
    sshd || true
    echo "sshd start requested"
  else
    echo "sshd command not found"
  fi

  echo "[5] Checking app directory"

  if [ ! -d "$APP_DIR" ]; then
    echo "APP_DIR not found: $APP_DIR"
    exit 1
  fi

  echo "[6] Checking files"

  if [ ! -f "$PUBLIC_CONFIG_FILE" ]; then
    echo "Public config not found: $PUBLIC_CONFIG_FILE"
    exit 1
  fi

  if [ ! -f "$POLLING_SCRIPT" ]; then
    echo "Polling script not found: $POLLING_SCRIPT"
    exit 1
  fi

  if [ ! -f "$BOT_SCRIPT" ]; then
    echo "Telegram bot script not found: $BOT_SCRIPT"
    exit 1
  fi

  if [ ! -f "$ENV_FILE" ]; then
    echo "ENV file not found: $ENV_FILE"
    exit 1
  fi

  echo "[7] Checking public config JSON"

  python -m json.tool "$PUBLIC_CONFIG_FILE" >/dev/null
  if [ $? -ne 0 ]; then
    echo "Public config JSON check failed"
    exit 1
  fi

  echo "Public config JSON OK"

  echo "[8] Public config summary"

  python - <<PY
import json
from pathlib import Path

path = Path("$PUBLIC_CONFIG_FILE")
data = json.loads(path.read_text(encoding="utf-8"))

polling = data.get("polling") or {}
alerts = data.get("alerts") or {}
external = data.get("external_commands") or {}
request = data.get("request") or {}
morning = data.get("morning_alert") or {}

print("polling.asleep_seconds=" + str(polling.get("asleep_seconds", "-")))
print("polling.online_seconds=" + str(polling.get("online_seconds", "-")))
print("polling.driving_seconds=" + str(polling.get("driving_seconds", "-")))
print("polling.charging_seconds=" + str(polling.get("charging_seconds", "-")))
print("polling.error_seconds=" + str(polling.get("error_seconds", "-")))
print("alerts.threshold_km_per_kwh=" + str(alerts.get("threshold_km_per_kwh", "-")))
print("alerts.window_minutes=" + str(alerts.get("window_minutes", "-")))
print("alerts.alert_cooldown_seconds=" + str(alerts.get("alert_cooldown_seconds", "-")))
print("external_commands.drive_boost_seconds=" + str(external.get("drive_boost_seconds", "-")))
print("request.timeout_seconds=" + str(request.get("timeout_seconds", "-")))
print("morning_alert.hour=" + str(morning.get("hour", "-")))
print("morning_alert.minute=" + str(morning.get("minute", "-")))
PY

  echo "[9] Checking Python syntax"

  python -m py_compile "$POLLING_SCRIPT"
  if [ $? -ne 0 ]; then
    echo "Polling script py_compile failed"
    exit 1
  fi

  echo "Polling script py_compile OK"

  python -m py_compile "$BOT_SCRIPT"
  if [ $? -ne 0 ]; then
    echo "Telegram bot script py_compile failed"
    exit 1
  fi

  echo "Telegram bot script py_compile OK"

  echo "[10] Loading env"

  set -a
  . "$ENV_FILE"
  set +a

  echo "[11] Env sanity check"

  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -z "${TELEGRAM_TOKEN:-}" ]; then
    echo "TELEGRAM_BOT_TOKEN / TELEGRAM_TOKEN missing"
  else
    echo "Telegram token exists"
  fi

  if [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "TELEGRAM_CHAT_ID missing"
  else
    echo "Telegram chat id exists"
  fi

  if [ -z "${TESLA_CLIENT_ID:-}" ]; then
    echo "TESLA_CLIENT_ID missing"
  else
    echo "TESLA_CLIENT_ID exists"
  fi

  if [ -z "${TESLA_CLIENT_SECRET:-}" ]; then
    echo "TESLA_CLIENT_SECRET missing"
  else
    echo "TESLA_CLIENT_SECRET exists"
  fi

  if [ -z "${TESLA_VIN:-}" ]; then
    echo "TESLA_VIN missing. Polling will use first vehicle from account."
  else
    echo "TESLA_VIN exists: $TESLA_VIN"
  fi

  if [ -z "${TESLA_API_BASE:-}" ]; then
    echo "TESLA_API_BASE missing, script default may be used"
  else
    echo "TESLA_API_BASE=$TESLA_API_BASE"
  fi

  if [ -z "${TESLA_SCOPE:-}" ]; then
    echo "TESLA_SCOPE missing, script default may be used"
  else
    echo "TESLA_SCOPE exists"
  fi

  echo "[12] Checking env overrides"

  if env | grep -q '^LIGHT_LOGGG_POLL_'; then
    echo "LIGHT_LOGGG_POLL_* env override detected:"
    env | grep '^LIGHT_LOGGG_POLL_' || true
  else
    echo "No LIGHT_LOGGG_POLL_* env override. Public config polling values will be used."
  fi

  if env | grep -q '^LIGHT_LOGGG_.*SECONDS\|^LIGHT_LOGGG_WINDOW_MINUTES\|^LIGHT_LOGGG_THRESHOLD'; then
    echo "LIGHT_LOGGG override values:"
    env | grep -E '^LIGHT_LOGGG_.*SECONDS|^LIGHT_LOGGG_WINDOW_MINUTES|^LIGHT_LOGGG_THRESHOLD' || true
  fi

  echo "[13] Killing old processes"

  pkill -f "light_loggg_tesla_polling.py" || true
  pkill -f "light_loggg_telegram_bot.py" || true

  sleep 2

  echo "[14] Removing old PID files"

  rm -f "$POLLING_PID" "$BOT_PID"

  echo "[15] Starting polling"

  cd "$APP_DIR" || exit 1

  nohup python "$POLLING_SCRIPT" >> "$LOG_DIR/polling.log" 2>&1 &
  POLLING_NEW_PID=$!
  echo "$POLLING_NEW_PID" > "$POLLING_PID"

  echo "polling pid: $POLLING_NEW_PID"

  sleep 3

  echo "[16] Starting Telegram bot"

  nohup python "$BOT_SCRIPT" >> "$LOG_DIR/telegram_bot.log" 2>&1 &
  BOT_NEW_PID=$!
  echo "$BOT_NEW_PID" > "$BOT_PID"

  echo "bot pid: $BOT_NEW_PID"

  echo "[17] Process check"

  ps aux | grep -E "light_loggg_tesla_polling.py|light_loggg_telegram_bot.py" | grep -v grep || true

  echo "[18] PID files"

  echo "polling pid file: $(cat "$POLLING_PID" 2>/dev/null)"
  echo "bot pid file: $(cat "$BOT_PID" 2>/dev/null)"

  echo "[19] Recent polling log"

  tail -n 10 "$LOG_DIR/polling.log" 2>/dev/null || true

  echo "[20] Recent telegram bot log"

  tail -n 10 "$LOG_DIR/telegram_bot.log" 2>/dev/null || true

  echo "==== LIGHT LOGGG BOOT END $(date) ===="

} >> "$BOOT_LOG" 2>> "$BOOT_ERR"
