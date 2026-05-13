#!/data/data/com.termux/files/usr/bin/bash

# LIGHT LOGGG Tesla Fleet Telemetry setup - Go fleet-telemetry version
#
# 목적:
# - Tesla 공식 fleet-telemetry Go 서버를 Termux에서 빌드/실행
# - handler는 ~/light_loggg_tesla/tesla_telemetry_handler.py 기준 사용
# - cloudflared trycloudflare 터널로 외부 endpoint 확보
#
# 주의:
# - 현재 메인 운영은 light_loggg_tesla_polling.py 기반 polling 방식이다.
# - 이 스크립트는 telemetry 실험/확장용이다.
# - Telegram token 등 민감값은 ~/.light_loggg.env에만 둔다.
# - Termux에서 Go 빌드는 느릴 수 있다. 미패드에서는 Python telemetry 버전이 더 현실적이다.

set -euo pipefail

APP_DIR="$HOME/light_loggg_tesla"
WORK_DIR="$HOME/tesla_telemetry_work"
TELEMETRY_DIR="$WORK_DIR/fleet-telemetry"
LOG_DIR="$WORK_DIR/logs"

ENV_FILE="$HOME/.light_loggg.env"

HANDLER_SCRIPT="$APP_DIR/tesla_telemetry_handler.py"
CONFIG_FILE="$WORK_DIR/config.json"
TELEMETRY_BINARY="$TELEMETRY_DIR/telemetry_binary"

TUNNEL_LOG="$WORK_DIR/tunnel.log"
SERVER_LOG="$WORK_DIR/fleet_telemetry.log"
UPDATE_TRIGGER="$WORK_DIR/update_trigger"

RAW_BASE="${LIGHT_LOGGG_RAW_BASE:-https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main}"

PORT="${LIGHT_LOGGG_TELEMETRY_PORT:-8080}"

printf '%s\n' "=== LIGHT LOGGG Tesla Fleet Telemetry Go setup 시작 ==="

mkdir -p "$APP_DIR" "$WORK_DIR" "$LOG_DIR"

printf '%s\n' "[1] Termux 패키지 확인/설치"

if command -v pkg >/dev/null 2>&1; then
  pkg update -y
  pkg install -y golang git python curl wget cloudflared termux-api || true
fi

printf '%s\n' "[2] Python requests 설치"

python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install requests >/dev/null

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

# Telemetry
LIGHT_LOGGG_TELEMETRY_PORT=8080
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

printf '%s\n' "[4] handler 다운로드"

curl -fL --connect-timeout 15 --max-time 60 \
  -o "$APP_DIR/.tesla_telemetry_handler.py.tmp" \
  "$RAW_BASE/tesla_telemetry_handler.py"

mv "$APP_DIR/.tesla_telemetry_handler.py.tmp" "$HANDLER_SCRIPT"
chmod +x "$HANDLER_SCRIPT"

python -m py_compile "$HANDLER_SCRIPT"

printf '%s\n' "[5] 실행 스크립트 생성"

cat > "$WORK_DIR/run_telemetry_go.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash

set -u

APP_DIR="$APP_DIR"
WORK_DIR="$WORK_DIR"
TELEMETRY_DIR="$TELEMETRY_DIR"
LOG_DIR="$LOG_DIR"

ENV_FILE="$ENV_FILE"

HANDLER_SCRIPT="$HANDLER_SCRIPT"
CONFIG_FILE="$CONFIG_FILE"
TELEMETRY_BINARY="$TELEMETRY_BINARY"

TUNNEL_LOG="$TUNNEL_LOG"
SERVER_LOG="$SERVER_LOG"
UPDATE_TRIGGER="$UPDATE_TRIGGER"

RAW_BASE="$RAW_BASE"
PORT="$PORT"

mkdir -p "\$APP_DIR" "\$WORK_DIR" "\$LOG_DIR"

if [ -f "\$ENV_FILE" ]; then
  set -a
  . "\$ENV_FILE"
  set +a
else
  echo "ENV file not found: \$ENV_FILE" | tee -a "\$SERVER_LOG"
fi

while true; do
  echo "==================================================" | tee -a "\$SERVER_LOG"
  echo "Fleet telemetry Go cycle start: \$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "\$SERVER_LOG"
  echo "==================================================" | tee -a "\$SERVER_LOG"

  rm -f "\$UPDATE_TRIGGER"

  echo "[1] Download latest handler" | tee -a "\$SERVER_LOG"

  curl -fL --connect-timeout 15 --max-time 60 \\
    -o "\$APP_DIR/.tesla_telemetry_handler.py.tmp" \\
    "\$RAW_BASE/tesla_telemetry_handler.py" && \\
    mv "\$APP_DIR/.tesla_telemetry_handler.py.tmp" "\$HANDLER_SCRIPT"

  chmod +x "\$HANDLER_SCRIPT"

  python -m py_compile "\$HANDLER_SCRIPT" || {
    echo "handler py_compile failed. Restarting in 30 seconds." | tee -a "\$SERVER_LOG"
    sleep 30
    continue
  }

  echo "[2] Prepare fleet-telemetry source" | tee -a "\$SERVER_LOG"

  if [ ! -d "\$TELEMETRY_DIR/.git" ]; then
    rm -rf "\$TELEMETRY_DIR"
    git clone https://github.com/teslamotors/fleet-telemetry.git "\$TELEMETRY_DIR" >> "\$SERVER_LOG" 2>&1 || {
      echo "git clone failed. Restarting in 60 seconds." | tee -a "\$SERVER_LOG"
      sleep 60
      continue
    }
  else
    cd "\$TELEMETRY_DIR" || {
      echo "cannot cd telemetry dir" | tee -a "\$SERVER_LOG"
      sleep 30
      continue
    }
    git pull >> "\$SERVER_LOG" 2>&1 || true
  fi

  echo "[3] Build telemetry binary" | tee -a "\$SERVER_LOG"

  cd "\$TELEMETRY_DIR" || {
    echo "cannot cd telemetry dir" | tee -a "\$SERVER_LOG"
    sleep 30
    continue
  }

  go build -o "\$TELEMETRY_BINARY" ./cmd/telemetry >> "\$SERVER_LOG" 2>&1 || {
    echo "go build failed. Restarting in 60 seconds." | tee -a "\$SERVER_LOG"
    sleep 60
    continue
  }

  echo "[4] Write config" | tee -a "\$SERVER_LOG"

  cat > "\$CONFIG_FILE" <<CONFIG_EOF
{
  "host": "0.0.0.0",
  "port": \$PORT,
  "log_level": "info",
  "storage": {
    "type": "file",
    "path": "\$LOG_DIR"
  }
}
CONFIG_EOF

  echo "[5] Ensure cloudflared tunnel" | tee -a "\$SERVER_LOG"

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

  echo "[6] Start fleet telemetry server + handler" | tee -a "\$SERVER_LOG"

  "\$TELEMETRY_BINARY" -config "\$CONFIG_FILE" 2>> "\$SERVER_LOG" | python "\$HANDLER_SCRIPT" >> "\$SERVER_LOG" 2>&1

  if [ -f "\$UPDATE_TRIGGER" ]; then
    echo "Update trigger detected. Restarting in 2 seconds." | tee -a "\$SERVER_LOG"
    sleep 2
  else
    echo "Fleet telemetry exited. Restarting in 10 seconds." | tee -a "\$SERVER_LOG"
    sleep 10
  fi
done
EOF

chmod +x "$WORK_DIR/run_telemetry_go.sh"

printf '%s\n' "[6] 상태 확인 명령 안내"

cat <<EOF

=== setup 완료 ===

Go telemetry 실행:
$WORK_DIR/run_telemetry_go.sh

로그 확인:
tail -n 100 "$SERVER_LOG"
tail -n 80 "$TUNNEL_LOG"

cloudflared endpoint 확인:
grep -o 'https://[-a-z0-9.]*\\.trycloudflare\\.com' "$TUNNEL_LOG" | head -n 1

주의:
- Go 빌드는 Termux에서 오래 걸릴 수 있음.
- 미패드에서는 setup_tesla_telemetry_python.sh 쪽이 더 현실적임.
- 현재 메인 운영은 polling + Telegram bot 구조가 우선임.

EOF

printf '%s\n' "=== LIGHT LOGGG Tesla Fleet Telemetry Go setup 종료 ==="
