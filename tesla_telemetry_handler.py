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

# 텔레그램 설정
TELEGRAM_TOKEN = "8776022575:AAFvPkYGd0rLMh15CqzVsiKkY69YniOgvM0"
ADMIN_CHAT_ID = "8792879646"

# 전비 모니터링
WINDOW_SIZE_MINUTES = 3
THRESHOLD_EFFICIENCY = 4.5
data_window = deque()
last_alert_time = 0

# 일일 통계
daily_stats = {
    "total_distance": 0,
    "efficiencies": [],
    "drive_sessions": [],
    "date": datetime.now().date()
}

def send_message(text):
    """텔레그램 메시지 전송"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except:
        pass

def check_commands():
    """텔레그램 명령 확인"""
    last_id = 0
    print("🤖 Telegram listener active...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            res = requests.get(url, params={"offset": last_id + 1, "timeout": 15}, timeout=20)
            data = res.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    last_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    
                    if chat_id == ADMIN_CHAT_ID:
                        if text == "/update":
                            send_message("🔄 업데이트 중...")
                            with open(os.path.expanduser("~/tesla_telemetry_work/update_trigger"), "w") as f:
                                f.write("1")
                            os.kill(os.getppid(), signal.SIGTERM)
                            sys.exit(0)
                        elif text == "/status":
                            send_message("✅ **두삼이 관제 시스템 v3 가동 중**\n- 3분 평균 전비 감시 중\n- 실시간 데이터 대기 중")
                        elif text == "/daily":
                            dist = daily_stats["total_distance"]
                            effs = daily_stats["efficiencies"]
                            avg_eff = sum(effs) / len(effs) if effs else 0
                            send_message(f"📊 **오늘의 주행 요약**\n🚗 거리: {dist:.1f} km\n⚡ 전비: {avg_eff:.2f} km/kWh")
        except:
            pass
        time.sleep(1)

# 텔레그램 스레드 즉시 시작 (모듈 로드 시)
cmd_thread = threading.Thread(target=check_commands, daemon=True)
cmd_thread.start()

def calculate_efficiency(points):
    if not points: return None
    total_dist = 0
    total_energy = 0
    for p in points:
        speed = p.get("speed", 0) * 1.60934
        power = abs(p.get("power", 0))
        if speed > 0:
            total_dist += speed / 3600
            total_energy += power / 1000 / 3600
    if total_dist == 0 or total_energy == 0: return None
    return round(total_dist / total_energy, 2)

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
        
        daily_stats["total_distance"] += (speed * 1.60934 / 3600)
        if eff: daily_stats["efficiencies"].append(eff)

print("🚀 Handler module loaded and threads started.")
