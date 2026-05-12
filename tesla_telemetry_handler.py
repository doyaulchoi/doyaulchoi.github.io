#!/usr/bin/env python3
import sys
import json
import requests
import os
import signal
import time
from datetime import datetime, timedelta
from collections import deque, defaultdict
import threading
import pytz

# 텔레그램 설정
TELEGRAM_TOKEN = "8776022575:AAFvPkYGd0rLMh15CqzVsiKkY69YniOgvM0"
# 초기 CHAT_ID 설정 (메시지를 받으면 자동으로 업데이트됨)
ADMIN_CHAT_ID = "8792879646"

# 전비 모니터링
WINDOW_SIZE_MINUTES = 3
THRESHOLD_EFFICIENCY = 4.5
data_window = deque()
last_alert_time = 0

# 일일 통계
daily_stats = {
    "start_odometer": None,
    "total_distance": 0,
    "total_time_seconds": 0,
    "efficiencies": [],
    "drive_sessions": [],
    "acceleration_events": [],
    "deceleration_events": [],
    "charging_sessions": [],
    "abnormal_soc_time": 0,
    "date": datetime.now().date()
}

# 주간 통계
weekly_stats = {
    "days": defaultdict(lambda: {
        "distance": 0,
        "energy_used": 0,
        "drive_count": 0,
        "rapid_accel_count": 0,
        "rapid_decel_count": 0,
        "charging_count": 0,
        "avg_charge_speed": 0,
        "abnormal_soc_time": 0
    }),
    "week_start": datetime.now()
}

def send_message(text, chat_id=None):
    """텔레그램 메시지 전송"""
    target_id = chat_id or ADMIN_CHAT_ID
    if not target_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": target_id, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"Send Error: {e}")

def check_commands():
    """텔레그램 명령 확인 및 ID 자동 감지"""
    global ADMIN_CHAT_ID
    last_id = 0
    print("🤖 Command listener started...")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            res = requests.get(url, params={"offset": last_id + 1, "timeout": 20}, timeout=30)
            data = res.json()
            
            if data.get("ok"):
                for update in data.get("result", []):
                    last_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    
                    if not chat_id: continue
                    
                    # 관리자 ID 업데이트 (첫 메시지 발송자 또는 기존 관리자)
                    if not ADMIN_CHAT_ID:
                        ADMIN_CHAT_ID = chat_id
                        send_message("🎯 관리자로 등록되었습니다!", chat_id)
                    
                    if chat_id == ADMIN_CHAT_ID:
                        if text == "/update":
                            send_message("🔄 시스템을 업데이트하고 재시작합니다...")
                            with open(os.path.expanduser("~/tesla_telemetry_work/update_trigger"), "w") as f:
                                f.write("1")
                            os.kill(os.getppid(), signal.SIGTERM)
                            sys.exit(0)
                        elif text == "/status":
                            send_message("✅ **두삼이 관제 시스템 가동 중**\n- 모니터링: 3분 평균 전비\n- 기준: 4.5 km/kWh\n- 상태: 데이터 수신 대기 중")
                        elif text == "/daily":
                            send_daily_summary()
                        elif text == "/weekly":
                            send_weekly_summary()
                        elif text == "/start":
                            send_message("🚀 두삼이 관제 봇에 오신 것을 환영합니다!\n/status - 현재 상태 확인\n/daily - 오늘 주행 요약\n/weekly - 주간 주행 요약\n/update - 원격 업데이트")
        except Exception as e:
            print(f"Update Error: {e}")
        time.sleep(1)

def calculate_efficiency(points):
    if not points: return None
    total_dist = 0
    total_energy = 0
    for p in points:
        speed = p.get("speed", 0) * 1.60934
        power = abs(p.get("power", 0))
        if speed > 0:
            total_dist += speed / 3600
            total_energy += power / 1000 / 3600 # kWh
    if total_dist == 0 or total_energy == 0: return None
    return round(total_dist / total_energy, 2)

def send_daily_summary():
    dist = daily_stats["total_distance"]
    if dist == 0:
        send_message("📊 오늘 주행 기록이 없습니다.")
        return
    
    effs = daily_stats["efficiencies"]
    avg_eff = sum(effs) / len(effs) if effs else 0
    
    msg = f"""📊 **오늘의 주행 요약**
🚗 주행거리: {dist:.1f} km
⚡ 평균전비: {avg_eff:.2f} km/kWh
📍 주행 세션: {len(daily_stats['drive_sessions'])}회"""
    send_message(msg)

def send_weekly_summary():
    send_message("📈 주간 요약 기능이 활성화되었습니다. 데이터가 쌓이면 금요일에 발송됩니다.")

def process_data(data):
    global last_alert_time
    current_time = datetime.now()
    data["ts"] = current_time
    data_window.append(data)
    
    while data_window and data_window[0]["ts"] < current_time - timedelta(minutes=WINDOW_SIZE_MINUTES):
        data_window.popleft()
    
    speed = data.get("speed", 0)
    if speed > 0:
        eff = calculate_efficiency(list(data_window))
        if eff and eff < THRESHOLD_EFFICIENCY:
            now = time.time()
            if now - last_alert_time > 60:
                send_message(f"⚠️ **전비 경고!**\n최근 3분 평균: `{eff} km/kWh`")
                last_alert_time = now
        
        # 주행 거리 업데이트
        daily_stats["total_distance"] += (speed * 1.60934 / 3600)
        if eff: daily_stats["efficiencies"].append(eff)

if __name__ == "__main__":
    cmd_thread = threading.Thread(target=check_commands, daemon=True)
    cmd_thread.start()
    
    print("🚀 Telemetry Handler Started")
    # 표준 입력 데이터 처리
    for line in sys.stdin:
        try:
            data = json.loads(line)
            process_data(data)
        except:
            continue
