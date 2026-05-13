#!/data/data/com.termux/files/usr/bin/bash

# LIGHT LOGGG Tesla Fleet API polling + Telegram bot setup for Termux
#
# 기준:
# - 앱 폴더: ~/light_loggg_tesla
# - 공개 설정: ~/light_loggg_tesla/light_loggg_public_config.json
# - 비공개 env: ~/.light_loggg.env
# - Tesla token 파일: ~/.light_loggg_tesla_tokens.json
# - state 파일: ~/.light_loggg_state.json
# - logs: ~/light_loggg_tesla/logs
# - boot script 설치 위치: ~/.termux/boot/start-light-loggg.sh
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

PUBLIC_CONFIG_FILE="$APP_DIR/light_loggg_public_config.json"

RAW_BASE="${LIGHT_LOGGG_RAW_BASE:-https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main}"

POLLING_SCRIPT="$APP_DIR/light_loggg_tesla_polling.py"
BOT_SCRIPT="$APP_DIR/light_loggg_telegram_bot.py"
OAUTH_SCRIPT="$APP_DIR/light_loggg_tesla_oauth.py"
CHECK_SCRIPT="$APP_DIR/check_system.py"

BOOT_SOURCE_SCRIPT="$APP_DIR/start-light-loggg.sh"
BOOT_DIR="$HOME/.termux/boot"
BOOT_TARGET_SCRIPT="$BOOT_DIR/start-light-loggg.sh"

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

printf '%s\n' "[3] GitHub raw 코드/공개 설정 다운로드"

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

download_file "light_loggg_public_config.json"
download_file "light_loggg_tesla_polling.py"
download_file "light_loggg_telegram_bot.py"
download_file "light_loggg_tesla_oauth.py"
download_file "check_system.py"
download_file "start-light-loggg.sh"
download_file "tesla_telemetry_handler.py"
download_file "telemetry_server.py"
download_file "setup_tesla_telemetry.sh"
download_file "setup_tesla_telemetry_python.sh"

printf '%s\n' "[4] 공개 설정 JSON 검사"

python -m json.tool "$PUBLIC_CONFIG_FILE" >/dev/null
printf '%s\n' "public config JSON OK: $PUBLIC_CONFIG_FILE"

printf '%s\n' "[5] Python 문법 검사"

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

printf '%s\n' "[6] 비공개 env 파일 생성/확인"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# LIGHT LOGGG private runtime configuration
# 이 파일은 GitHub에 올리지 않는다.
# 공백이 있는 값은 반드시 따옴표로 감싼다.
#
# 공개 가능한 polling/alert 설정은 GitHub의 light_loggg_public_config.json에 둔다.
# 이 파일에는 token/secret/chat_id/VIN 등 공개하지 않을 값을 둔다.

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Tesla Fleet API
TESLA_CLIENT_ID=
TESLA_CLIENT_SECRET=
TESLA_VIN=5YJ3E1ECXMF912228
TESLA_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com
TESLA_SCOPE="openid offline_access user_data vehicle_device_data vehicle_location"

# Optional
# TESLA_REDIRECT_URI=https://doyaulchoi.github.io/index.html
# LIGHT_LOGGG_RAW_BASE=https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main
# LIGHT_LOGGG_STATE_FILE=/data/data/com.termux/files/home/.light_loggg_state.json

# Optional overrides
# 아래 값들은 light_loggg_public_config.json보다 우선 적용된다.
# 평소에는 주석으로 두고, 임시 override가 필요할 때만 사용한다.
# LIGHT_LOGGG_POLL_ASLEEP_SECONDS=1800
# LIGHT_LOGGG_POLL_ONLINE_SECONDS=300
# LIGHT_LOGGG_POLL_DRIVING_SECONDS=10
# LIGHT_LOGGG_POLL_CHARGING_SECONDS=60
# LIGHT_LOGGG_POLL_ERROR_SECONDS=300
# LIGHT_LOGGG_THRESHOLD_KM_PER_KWH=4.5
# LIGHT_LOGGG_WINDOW_MINUTES=3
# LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS=60
# LIGHT_LOGGG_EXTERNAL_DRIVE_BOOST_SECONDS=180
# LIGHT_LOGGG_REQUEST_TIMEOUT=25
# LIGHT_LOGGG_MORNING_ALERT_HOUR=6
# LIGHT_LOGGG_MORNING_ALERT_MINUTE=30
EOF

  chmod 600 "$ENV_FILE"
  printf '%s\n' "env 파일 생성됨: $ENV_FILE"
  printf '%s\n' "반드시 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TESLA_CLIENT_ID, TESLA_CLIENT_SECRET 값을 채워라."
else
  printf '%s\n' "env 파일 이미 있음. 덮어쓰지 않음: $ENV_FILE"
fi

printf '%s\n' "[7] Tesla token 파일 생성/확인"

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

printf '%s\n' "[8] Termux:Boot 스크립트 설치"

mkdir -p "$BOOT_DIR"

if [ ! -f "$BOOT_SOURCE_SCRIPT" ]; then
  printf '%s\n' "오류: boot script 원본 없음: $BOOT_SOURCE_SCRIPT" >&2
  exit 1
fi

cp "$BOOT_SOURCE_SCRIPT" "$BOOT_TARGET_SCRIPT"
chmod +x "$BOOT_TARGET_SCRIPT"

printf '%s\n' "Termux:Boot script 설치 완료: $BOOT_TARGET_SCRIPT"

printf '%s\n' "[9] run helper script 생성"

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

cat > "$APP_DIR/update_from_github.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash

set -euo pipefail

APP_DIR="$APP_DIR"
RAW_BASE="\${LIGHT_LOGGG_RAW_BASE:-$RAW_BASE}"

cd "\$APP_DIR"

download_file() {
  local filename="\$1"
  local temp=".\${filename}.tmp"
  local url="\$RAW_BASE/\$filename"

  echo "download: \$filename"
  curl -fL --connect-timeout 15 --max-time 60 -o "\$temp" "\$url"

  if [ ! -s "\$temp" ]; then
    rm -f "\$temp"
    echo "empty download: \$filename" >&2
    exit 1
  fi

  mv "\$temp" "\$filename"

  case "\$filename" in
    *.py|*.sh)
      chmod +x "\$filename"
      ;;
  esac
}

download_file "light_loggg_public_config.json"
download_file "light_loggg_tesla_polling.py"
download_file "light_loggg_telegram_bot.py"
download_file "light_loggg_tesla_oauth.py"
download_file "check_system.py"
download_file "start-light-loggg.sh"
download_file "tesla_telemetry_handler.py"
download_file "telemetry_server.py"
download_file "setup_light_loggg_tesla_polling.sh"
download_file "setup_tesla_telemetry.sh"
download_file "setup_tesla_telemetry_python.sh"

python -m json.tool light_loggg_public_config.json >/dev/null
python -m py_compile light_loggg_tesla_polling.py
python -m py_compile light_loggg_telegram_bot.py
python -m py_compile light_loggg_tesla_oauth.py
python -m py_compile check_system.py

mkdir -p "$BOOT_DIR"
cp start-light-loggg.sh "$BOOT_TARGET_SCRIPT"
chmod +x "$BOOT_TARGET_SCRIPT"

echo "update complete"
EOF

chmod +x "$APP_DIR/update_from_github.sh"

printf '%s\n' "[10] 설치 후 확인 명령"

cat <<EOF

=== 설치 완료 ===

비공개 env 편집:
nano "$ENV_FILE"

필수 입력값:
TELEGRAM_BOT_TOKEN="새_텔레그램_봇_토큰"
TELEGRAM_CHAT_ID="8792879646"
TESLA_CLIENT_ID="네_CLIENT_ID"
TESLA_CLIENT_SECRET="네_CLIENT_SECRET"
TESLA_VIN=5YJ3E1ECXMF912228
TESLA_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com
TESLA_SCOPE="openid offline_access user_data vehicle_device_data vehicle_location"

공개 설정 확인:
cat "$PUBLIC_CONFIG_FILE"
python -m json.tool "$PUBLIC_CONFIG_FILE" >/dev/null && echo CONFIG_OK

1회 polling 테스트:
python "$POLLING_SCRIPT" --once

시스템 진단:
python "$CHECK_SCRIPT"

수동 전체 재시작:
"$BOOT_TARGET_SCRIPT"

수동 GitHub 업데이트:
"$APP_DIR/update_from_github.sh"

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
 /poll_now
 /driving_start
 /driving_stop

주의:
- Termux:Boot 앱을 한 번 직접 실행해야 부팅 리시버가 활성화될 수 있음.
- 미패드 설정에서 Termux / Termux:Boot 배터리 제한 없음, 자동 시작 허용 필요.
- env/token 파일은 GitHub에 절대 올리지 말 것.
- polling 주기 등 공개 설정은 light_loggg_public_config.json에서 관리한다.
- env에 LIGHT_LOGGG_POLL_* 값이 있으면 public config보다 env가 우선 적용된다.

EOF

printf '%s\n' "=== LIGHT LOGGG Tesla polling + Telegram bot setup 종료 ==="
