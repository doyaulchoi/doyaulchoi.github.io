#!/data/data/com.termux/files/usr/bin/bash

# LIGHT LOGGG Tesla Fleet API polling setup for Termux
# Author: Manus AI

set -euo pipefail

WORK_DIR="$HOME/light_loggg_tesla"
APP_SCRIPT="$WORK_DIR/light_loggg_tesla_polling.py"
ENV_FILE="$HOME/.light_loggg.env"
TOKEN_FILE="$HOME/.light_loggg_tesla_tokens.json"
STATE_FILE="$HOME/.light_loggg_state.json"
RAW_BASE="https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main"

printf '%s\n' "LIGHT LOGGG Tesla 폴링 설치를 시작합니다."
mkdir -p "$WORK_DIR"

if command -v pkg >/dev/null 2>&1; then
  pkg update -y
  pkg install -y python wget termux-services || pkg install -y python wget
fi

python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install requests >/dev/null

wget -q -O "$APP_SCRIPT" "$RAW_BASE/light_loggg_tesla_polling.py"
chmod +x "$APP_SCRIPT"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# LIGHT LOGGG runtime configuration
# 실제 값으로 바꾸세요. 토큰은 GitHub에 올리지 않습니다.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TESLA_VIN=5YJ3E1ECXMF912228
TESLA_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com
LIGHT_LOGGG_THRESHOLD_KM_PER_KWH=4.5
LIGHT_LOGGG_WINDOW_MINUTES=3
# HOME_LAT=
# HOME_LON=
EOF
  chmod 600 "$ENV_FILE"
  printf '%s\n' "환경 파일을 만들었습니다. $ENV_FILE 을 열어 텔레그램 값을 입력하세요."
fi

if [ ! -f "$TOKEN_FILE" ]; then
  cat > "$TOKEN_FILE" <<'EOF'
{
  "refresh_token": "여기에 Tesla refresh_token 입력"
}
EOF
  chmod 600 "$TOKEN_FILE"
  printf '%s\n' "토큰 파일을 만들었습니다. $TOKEN_FILE 에 Tesla refresh_token을 입력하세요."
fi

cat > "$WORK_DIR/run.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$WORK_DIR"
while true; do
  python "$APP_SCRIPT" --token-file "$TOKEN_FILE" --state-file "$STATE_FILE"
  echo "LIGHT LOGGG exited. Restarting in 10 seconds."
  sleep 10
done
EOF
chmod +x "$WORK_DIR/run.sh"

printf '%s\n' "1회 테스트 명령"
printf '%s\n' "python $APP_SCRIPT --once --token-file $TOKEN_FILE --state-file $STATE_FILE"
printf '%s\n' "상시 실행 명령"
printf '%s\n' "$WORK_DIR/run.sh"
