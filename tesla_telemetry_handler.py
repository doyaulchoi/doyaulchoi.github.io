import sys
import json
import requests
import os
import signal
import subprocess
from datetime import datetime, timedelta
from collections import deque
import threading
import time

# 텔레그램 설정
TELEGRAM_TOKEN = "8776022575:AAFvPkYGd0rLMh15CqzVsiKkY69YniOgvM0"
CHAT_ID = "8792879646"

# 전비 모니터링 설정
WINDOW_SIZE_MINUTES = 3
THRESHOLD_EFFICIENCY = 4.5
data_window = deque()

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send telegram message: {e}")

def check_remote_commands():
    """텔레그램 메시지를 확인하여 원격 명령 처리"""
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}"
            res = requests.get(url, timeout=10).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "")
                    sender_id = str(message.get("from", {}).get("id", ""))
                    
                    # 보안 확인: 등록된 사용자(엔지니어님)로부터 온 명령만 실행
                    # (이후 Manus가 직접 신호를 보낼 수 있도록 로직 확장 가능)
                    if sender_id == CHAT_ID:
                        if text == "/update":
                            send_telegram_message("🔄 **원격 업데이트를 시작합니다...**")
                            with open(os.path.expanduser("~/update_trigger"), "w") as f:
                                f.write("update")
                            os.kill(os.getppid(), signal.SIGTERM)
                            sys.exit(0)
                        elif text == "/status":
                            send_telegram_message("✅ **시스템 가동 중**\n- 모니터링: 최근 3분 전비\n- 기준: 4.5 km/kWh")
                            
        except Exception as e:
            print(f"Error checking commands: {e}")
        time.sleep(5)

def calculate_efficiency(data_points):
    if not data_points: return None
    total_speed_kmh = 0
    total_power_kw = 0
    count = 0
    for dp in data_points:
        speed = dp.get("speed", 0) * 1.60934
        power = dp.get("power", 0)
        if speed > 0:
            total_speed_kmh += speed
            total_power_kw += abs(power)
            count += 1
    if count == 0 or total_power_kw == 0: return None
    return round(total_speed_kmh / total_power_kw, 2)

def process_telemetry_data(data):
    current_time = datetime.now()
    data["timestamp_internal"] = current_time
    data_window.append(data)
    while data_window and data_window[0]["timestamp_internal"] < current_time - timedelta(minutes=WINDOW_SIZE_MINUTES):
        data_window.popleft()
    if data.get("speed", 0) > 0:
        eff = calculate_efficiency(data_window)
        if eff and eff < THRESHOLD_EFFICIENCY:
            send_telegram_message(f"📉 **전비 경고!**\n최근 {WINDOW_SIZE_MINUTES}분 평균 전비가 **{eff} km/kWh**입니다.")

if __name__ == "__main__":
    cmd_thread = threading.Thread(target=check_remote_commands, daemon=True)
    cmd_thread.start()
    
    print("Tesla Telemetry Handler V4 (Remote Control Ready) Started...")
    send_telegram_message("🚀 **두삼이 관제 시스템 가동 시작**\n(원격 제어 준비 완료)")
    
    for line in sys.stdin:
        try:
            # Telemetry 서버가 뱉는 로그를 한 줄씩 읽어 처리
            data = json.loads(line)
            process_telemetry_data(data)
        except:
            continue
