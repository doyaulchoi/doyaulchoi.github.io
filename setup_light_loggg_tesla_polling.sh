#!/data/data/com.termux/files/usr/bin/bash

# LIGHT LOGGG Tesla Fleet API polling + Telegram bot setup for Termux
#
# 기준:
# - 앱 폴더: ~/light_loggg_tesla
# - env 파일: ~/.light_loggg.env
# - Tesla token 파일: ~/.light_loggg_tesla_tokens.json
# - state 파일: ~/.light_loggg_state.json
# - logs: ~/light_loggg_tesla/logs
# - GitHub raw에서 코드 다운로드
#
# 주의:
# - 이 스크립트는 설치/초기 세팅용이다.
# - 민감값은 GitHub에 올리지 않고 ~/.light_loggg.env 에만 둔다.
# - 기존 env/token/state 파일은 있으면 덮어쓰지 않는다.

set -euo pipefail

APP_DIR="$HOME/light_loggg_tesla"
LOG_DIR="$APP_DIR/logs"

ENV_FILE="$HOME/.light_loggg.env"
TOKEN_FILE="$HOME/.light_loggg_tesla_tokens.json"
STATE_FILE="$HOME/.light_loggg_state.json"

RAW_BASE="${LIGHT_LOGGG_RAW_BASE:-https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main}"

POLLING_SCRIPT="$APP_DIR/light_loggg_tesla_polling.py"
BOT_SCRIPT="$APP_DIR/light_loggg_telegram_bot.py"
OAUTH_SCRIPT="$APP_DIR/light_loggg_tesla_oauth.py"
CHECK_SCRIPT="$APP_DIR/check_system.py"

BOOT_DIR="$HOME/.termux/boot"
BOOT_SCRIPT="$BOOT_DIR/start-light-loggg.sh"

printf '%s\n' "=== LIGHT LOGGG Tesla polling + Telegram bot setup 시작 ==="

mkdir -p "$APP_DIR" "$LOG_DIR"

printf '%s\n' "[1] Termux 패키지 확인/설치"

if command -v pkg >/dev/null 2>&1; then
  pkg update -y
  pkg install -y python curl wget openssh termux-api || true
fi

printf '%s\n' "[2] Python requests 설치"

python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install requests >/dev/null

printf '%s\n' "[3] GitHub raw 코드 다운로드"

download_file() {
  local filename="$1"
  local target="$APP_DIR/$filename"
  local temp="$APP_DIR/.${filename}.tmp"
  local url="$RAW_BASE/$filename"

  printf '%s\n' "다운로드: $filename"
  curl -fL --connect-timeout 15 --max-time 60 -o "$temp" "$url"

  if [ ! -s "$temp" ]; then
    rm -f "$temp"
    printf '%s\n' "오류: 다운로드 결과가 비어 있음: $filename" >&2
    exit 1
  fi

  mv "$temp" "$target"

  case "$filename" in
    *.py|*.sh)
      chmod +x "$target"
      ;;
  esac
}

download_file "light_loggg_tesla_polling.py"
download_file "light_loggg_telegram_bot.py"
download_file "light_loggg_tesla_oauth.py"
download_file "check_system.py"
download_file "tesla_telemetry_handler.py"
download_file "telemetry_server.py"
download_file "setup_tesla_telemetry.sh"
download_file "setup_tesla_telemetry_python.sh"

printf '%s\n' "[4] Python 문법 검사"

python -m py_compile "$POLLING_SCRIPT"
python -m py_compile "$BOT_SCRIPT"
python -m py_compile "$OAUTH_SCRIPT"
python -m py_compile "$CHECK_SCRIPT"

if [ -f "$APP_DIR/tesla_telemetry_handler.py" ]; then
  python -m py_compile "$APP_DIR/tesla_telemetry_handler.py"
fi

if [ -f "$APP_DIR/telemetry_server.py" ]; then
  python -m py_compile "$APP_DIR/telemetry_server.py"
fi

printf '%s\n' "[5] env 파일 생성/확인"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# LIGHT LOGGG runtime configuration
# 이 파일은 GitHub에 올리지 않는다.
# 공백이 있는 값은 반드시 따옴표로 감싼다.

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Tesla Fleet API
TESLA_CLIENT_ID=
TESLA_CLIENT_SECRET=
TESLA_VIN=5YJ3E1ECXMF912228
TESLA_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com
TESLA_SCOPE="openid offline_access user_data vehicle_device_data vehicle_location"

# LIGHT LOGGG polling intervals
LIGHT_LOGGG_POLL_ASLEEP_SECONDS=1800
LIGHT_LOGGG_POLL_ONLINE_SECONDS=300
LIGHT_LOGGG_POLL_DRIVING_SECONDS=10
LIGHT_LOGGG_POLL_CHARGING_SECONDS=60
LIGHT_LOGGG_POLL_ERROR_SECONDS=300

# LIGHT LOGGG options
LIGHT_LOGGG_REQUEST_TIMEOUT=25
LIGHT_LOGGG_THRESHOLD_KM_PER_KWH=4.5
LIGHT_LOGGG_WINDOW_MINUTES=3
LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS=60

# Optional
# LIGHT_LOGGG_RAW_BASE=https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main
# HOME_LAT=
# HOME_LON=
EOF

  chmod 600 "$ENV_FILE"
  printf '%s\n' "env 파일 생성됨: $ENV_FILE"
  printf '%s\n' "반드시 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TESLA_CLIENT_ID, TESLA_CLIENT_SECRET 값을 채워라."
else
  printf '%s\n' "env 파일 이미 있음. 덮어쓰지 않음: $ENV_FILE"
fi

printf '%s\n' "[6] Tesla token 파일 생성/확인"

if [ ! -f "$TOKEN_FILE" ]; then
  cat > "$TOKEN_FILE" <<'EOF'
{
  "refresh_token": "여기에 Tesla refresh_token 입력"
}
EOF

  chmod 600 "$TOKEN_FILE"
  printf '%s\n' "Tesla token 파일 생성됨: $TOKEN_FILE"
  printf '%s\n' "authorization_code flow 완료 후 refresh_token으로 교체해야 함."
else
  printf '%s\n' "Tesla token 파일 이미 있음. 덮어쓰지 않음: $TOKEN_FILE"
fi

printf '%s\n' "[7] Termux:Boot 스크립트 생성/갱신"

mkdir -p "$BOOT_DIR"

cat > "$BOOT_SCRIPT" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash

HOME_DIR="/data/data/com.termux/files/home"
APP_DIR="$HOME_DIR/light_loggg_tesla"
STATE_DIR="$HOME_DIR/light_loggg_tesla"
LOG_DIR="$STATE_DIR/logs"

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

  sleep 30

  echo "[1] Waiting for DNS/network..."

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

    echo "DNS not ready yet... $i"
    sleep 5
  done

  if [ "$NETWORK_READY" != "1" ]; then
    echo "DNS/network not ready after timeout. Continue anyway."
  fi

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

  sleep 2

  echo "[8] Removing old PID files..."
  rm -f "$POLLING_PID" "$BOT_PID"

  echo "[9] Starting polling..."
  cd "$APP_DIR" || exit 1
  nohup python "$POLLING_SCRIPT" >> "$LOG_DIR/polling.log" 2>&1 &
  echo $! > "$POLLING_PID"

  sleep 3

  echo "[10] Starting Telegram bot..."
  nohup python "$BOT_SCRIPT" >> "$LOG_DIR/telegram_bot.log" 2>&1 &
  echo $! > "$BOT_PID"

  echo "[11] PID files:"
  echo "polling pid: $(cat "$POLLING_PID" 2>/dev/null)"
  echo "bot pid: $(cat "$BOT_PID" 2>/dev/null)"

  echo "==== LIGHT LOGGG BOOT END $(date) ===="

} >> "$BOOT_LOG" 2>> "$BOOT_ERR"
EOF

chmod +x "$BOOT_SCRIPT"

printf '%s\n' "[8] run script 생성"

cat > "$APP_DIR/run_polling.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$APP_DIR"
set -a
. "$ENV_FILE"
set +a
python "$POLLING_SCRIPT"
EOF

chmod +x "$APP_DIR/run_polling.sh"

cat > "$APP_DIR/run_bot.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$APP_DIR"
set -a
. "$ENV_FILE"
set +a
python "$BOT_SCRIPT"
EOF

chmod +x "$APP_DIR/run_bot.sh"

printf '%s\n' "[9] 설치 후 확인 명령"

cat <<EOF

=== 설치 완료 ===

1회 polling 테스트:
python "$POLLING_SCRIPT" --once

시스템 진단:
python "$CHECK_SCRIPT"

수동 전체 재시작:
"$BOOT_SCRIPT"

로그 확인:
tail -n 50 "$LOG_DIR/polling.log"
tail -n 50 "$LOG_DIR/telegram_bot.log"
tail -n 50 "$LOG_DIR/boot.log"
tail -n 50 "$LOG_DIR/boot-error.log" 2>/dev/null

Telegram 명령:
 /status
 /daily
 /weekly
 /update

주의:
- Termux:Boot 앱을 한 번 직접 실행해야 부팅 리시버가 활성화될 수 있음.
- 미패드 설정에서 Termux / Termux:Boot 배터리 제한 없음, 자동 시작 허용 필요.
- env/token 파일은 GitHub에 절대 올리지 말 것.

EOF

printf '%s\n' "=== LIGHT LOGGG Tesla polling + Telegram bot setup 종료 ==="
