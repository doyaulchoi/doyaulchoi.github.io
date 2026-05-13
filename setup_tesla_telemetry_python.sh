#!/data/data/com.termux/files/usr/bin/bash

# LIGHT LOGGG Tesla Fleet Telemetry setup - Python version
#
# 목적:
# - Go 빌드 없이 Python Flask telemetry server 실행
# - cloudflared trycloudflare 터널로 외부 endpoint 확보
# - GitHub raw에서 telemetry_server.py / tesla_telemetry_handler.py 갱신
#
# 주의:
# - 현재 메인 운영은 light_loggg_tesla_polling.py 기반 polling 방식이다.
# - 이 스크립트는 telemetry 실험/확장용이다.
# - Telegram token 등 민감값은 ~/.light_loggg.env에만 둔다.

set -euo pipefail

APP_DIR="$HOME/light_loggg_tesla"
WORK_DIR="$HOME/tesla_telemetry_work"
LOG_DIR="$WORK_DIR/logs"

ENV_FILE="$HOME/.light_loggg.env"

SERVER_SCRIPT="$APP_DIR/telemetry_server.py"
HANDLER_SCRIPT="$APP_DIR/tesla_telemetry_handler.py"

TUNNEL_LOG="$WORK_DIR/tunnel.log"
SERVER_LOG="$WORK_DIR/telemetry_server.log"
UPDATE_TRIGGER="$WORK_DIR/update_trigger"

RAW_BASE="${LIGHT_LOGGG_RAW_BASE:-https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main}"

HOST="${LIGHT_LOGGG_TELEMETRY_HOST:-0.0.0.0}"
PORT="${LIGHT_LOGGG_TELEMETRY_PORT:-8080}"

printf '%s\n' "=== LIGHT LOGGG Tesla Telemetry Python setup 시작 ==="

mkdir -p "$APP_DIR" "$WORK_DIR" "$LOG_DIR"

printf '%s\n' "[1] Termux 패키지 확인/설치"

if command -v pkg >/dev/null 2>&1; then
  pkg update -y
  pkg install -y python curl wget cloudflared termux-api || true
fi

printf '%s\n' "[2] Python 패키지 설치"

python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install flask requests >/dev/null

printf '%s\n' "[3] env 파일 확인"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# LIGHT LOGGG runtime configuration
# 이 파일은 GitHub에 올리지 않는다.

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Tesla Fleet API
TESLA_CLIENT_ID=
TESLA_CLIENT_SECRET=
TESLA_VIN=5YJ3E1ECXMF912228
TESLA_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com
TESLA_SCOPE="openid offline_access user_data vehicle_device_data vehicle_location"

# Telemetry server
LIGHT_LOGGG_TELEMETRY_HOST=0.0.0.0
LIGHT_LOGGG_TELEMETRY_PORT=8080
LIGHT_LOGGG_TELEMETRY_HANDLER=~/light_loggg_tesla/tesla_telemetry_handler.py

# Telemetry efficiency options
LIGHT_LOGGG_THRESHOLD_KM_PER_KWH=4.5
LIGHT_LOGGG_WINDOW_MINUTES=3
LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS=60
LIGHT_LOGGG_REQUEST_TIMEOUT=25
EOF

  chmod 600 "$ENV_FILE"
  printf '%s\n' "env 파일 생성됨: $ENV_FILE"
  printf '%s\n' "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 등 필요한 값을 채워야 함."
else
  printf '%s\n' "env 파일 이미 있음. 덮어쓰지 않음: $ENV_FILE"
fi

printf '%s\n' "[4] GitHub raw에서 telemetry 파일 다운로드"

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
  chmod +x "$target"
}

download_file "telemetry_server.py"
download_file "tesla_telemetry_handler.py"

printf '%s\n' "[5] Python 문법 검사"

python -m py_compile "$SERVER_SCRIPT"
python -m py_compile "$HANDLER_SCRIPT"

printf '%s\n' "[6] 실행 스크립트 생성"

cat > "$WORK_DIR/run_telemetry_python.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash

set -u

APP_DIR="$APP_DIR"
WORK_DIR="$WORK_DIR"
LOG_DIR="$LOG_DIR"

ENV_FILE="$ENV_FILE"
SERVER_SCRIPT="$SERVER_SCRIPT"
HANDLER_SCRIPT="$HANDLER_SCRIPT"

TUNNEL_LOG="$TUNNEL_LOG"
SERVER_LOG="$SERVER_LOG"
UPDATE_TRIGGER="$UPDATE_TRIGGER"

HOST="$HOST"
PORT="$PORT"

mkdir -p "\$WORK_DIR" "\$LOG_DIR"

if [ -f "\$ENV_FILE" ]; then
  set -a
  . "\$ENV_FILE"
  set +a
else
  echo "ENV file not found: \$ENV_FILE"
fi

while true; do
  echo "==================================================" | tee -a "\$SERVER_LOG"
  echo "Telemetry Python cycle start: \$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "\$SERVER_LOG"
  echo "==================================================" | tee -a "\$SERVER_LOG"

  rm -f "\$UPDATE_TRIGGER"

  echo "[1] Download latest telemetry files" | tee -a "\$SERVER_LOG"

  curl -fL --connect-timeout 15 --max-time 60 \\
    -o "\$APP_DIR/.telemetry_server.py.tmp" \\
    "$RAW_BASE/telemetry_server.py" && \\
    mv "\$APP_DIR/.telemetry_server.py.tmp" "\$SERVER_SCRIPT"

  curl -fL --connect-timeout 15 --max-time 60 \\
    -o "\$APP_DIR/.tesla_telemetry_handler.py.tmp" \\
    "$RAW_BASE/tesla_telemetry_handler.py" && \\
    mv "\$APP_DIR/.tesla_telemetry_handler.py.tmp" "\$HANDLER_SCRIPT"

  chmod +x "\$SERVER_SCRIPT" "\$HANDLER_SCRIPT"

  echo "[2] Compile check" | tee -a "\$SERVER_LOG"
  python -m py_compile "\$SERVER_SCRIPT" "\$HANDLER_SCRIPT" || {
    echo "py_compile failed. Restarting in 30 seconds." | tee -a "\$SERVER_LOG"
    sleep 30
    continue
  }

  echo "[3] Ensure cloudflared tunnel" | tee -a "\$SERVER_LOG"

  if ! pgrep -f "cloudflared tunnel --url http://localhost:\$PORT" >/dev/null 2>&1; then
    pkill -f "cloudflared tunnel --url" >/dev/null 2>&1 || true
    rm -f "\$TUNNEL_LOG"

    nohup cloudflared tunnel --url "http://localhost:\$PORT" \\
      > "\$TUNNEL_LOG" 2>&1 &

    sleep 8
  fi

  TUNNEL_URL=\$(grep -o 'https://[-a-z0-9.]*\\.trycloudflare\\.com' "\$TUNNEL_LOG" 2>/dev/null | head -n 1 || true)

  if [ -z "\$TUNNEL_URL" ]; then
    TUNNEL_URL="pending"
  fi

  echo "Active tunnel: \$TUNNEL_URL" | tee -a "\$SERVER_LOG"
  echo "Telemetry endpoint: \$TUNNEL_URL/api/1/vehicles/<vehicle_id>/telemetry" | tee -a "\$SERVER_LOG"

  echo "[4] Start telemetry server" | tee -a "\$SERVER_LOG"

  LIGHT_LOGGG_TELEMETRY_HOST="\${LIGHT_LOGGG_TELEMETRY_HOST:-\$HOST}" \\
  LIGHT_LOGGG_TELEMETRY_PORT="\${LIGHT_LOGGG_TELEMETRY_PORT:-\$PORT}" \\
  LIGHT_LOGGG_TELEMETRY_HANDLER="\${LIGHT_LOGGG_TELEMETRY_HANDLER:-\$HANDLER_SCRIPT}" \\
  python "\$SERVER_SCRIPT" "\$HANDLER_SCRIPT" >> "\$SERVER_LOG" 2>&1

  if [ -f "\$UPDATE_TRIGGER" ]; then
    echo "Update trigger detected. Restarting in 2 seconds." | tee -a "\$SERVER_LOG"
    sleep 2
  else
    echo "Telemetry server exited. Restarting in 10 seconds." | tee -a "\$SERVER_LOG"
    sleep 10
  fi
done
EOF

chmod +x "$WORK_DIR/run_telemetry_python.sh"

printf '%s\n' "[7] 상태 확인 명령 안내"

cat <<EOF

=== setup 완료 ===

telemetry Python 실행:
$WORK_DIR/run_telemetry_python.sh

로그 확인:
tail -n 80 "$SERVER_LOG"
tail -n 80 "$TUNNEL_LOG"

헬스체크:
curl http://127.0.0.1:$PORT/health

cloudflared endpoint 확인:
grep -o 'https://[-a-z0-9.]*\\.trycloudflare\\.com' "$TUNNEL_LOG" | head -n 1

주의:
- 이 telemetry 방식은 실험/확장용이다.
- 현재 메인 운영은 polling + Telegram bot 구조가 우선이다.
- Telegram token은 ~/.light_loggg.env에만 둬야 한다.

EOF

printf '%s\n' "=== LIGHT LOGGG Tesla Telemetry Python setup 종료 ==="
