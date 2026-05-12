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
CHAT_ID = "8792879646"

# 전비 모니터링
WINDOW_SIZE_MINUTES = 3
THRESHOLD_EFFICIENCY = 4.5
data_window = deque()
last_alert_time = 0

# 일일 통계 (매 자정에 리셋)
daily_stats = {
    "start_odometer": None,
    "end_odometer": None,
    "start_soc": None,
    "end_soc": None,
    "total_distance": 0,
    "total_time_seconds": 0,
    "speeds": [],
    "efficiencies": [],
    "power_readings": [],
    "last_location": None,
    "drive_sessions": [],  # 각 주행 세션의 정보
    "date": datetime.now().date()
}

# 주간 통계 (매주 월요일 자정에 리셋)
weekly_stats = {
    "days": defaultdict(lambda: {
        "distance": 0,
        "avg_speed": 0,
        "avg_efficiency": 0,
        "energy_used": 0,
        "drive_count": 0
    }),
    "week_start": None
}

# 상태 추적
current_session = {
    "driving": False,
    "session_start_time": None,
    "session_start_odometer": None,
    "session_start_soc": None,
    "session_speeds": [],
    "session_efficiencies": []
}

def send_message(text):
    """텔레그램 메시지 전송"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=5)
    except:
        pass

def check_commands():
    """텔레그램 명령 확인"""
    last_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            res = requests.get(url, params={"offset": last_id + 1}, timeout=10)
            data = res.json()
            
            if data.get("ok"):
                for update in data.get("result", []):
                    last_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    user_id = str(msg.get("from", {}).get("id", ""))
                    
                    if user_id == CHAT_ID:
                        if text == "/update":
                            send_message("🔄 업데이트 시작합니다...")
                            with open(os.path.expanduser("~/tesla_telemetry_work/update_trigger"), "w") as f:
                                f.write("1")
                            os.kill(os.getppid(), signal.SIGTERM)
                            sys.exit(0)
                        elif text == "/status":
                            send_message("✅ 시스템 가동 중\n- 모니터링: 최근 3분 전비\n- 기준: 4.5 km/kWh\n- 일일/주간 요약 기능 활성화")
                        elif text == "/daily":
                            send_daily_summary()
                        elif text == "/weekly":
                            send_weekly_summary()
        except:
            pass
        time.sleep(3)

def calculate_efficiency(points):
    """전비 계산 (km/kWh)"""
    if not points:
        return None
    total_distance = 0
    total_energy = 0
    
    for p in points:
        speed = p.get("speed", 0) * 1.60934  # mph to km/h
        power = abs(p.get("power", 0))
        
        if speed > 0:
            # 1초 단위 거리 계산 (속도는 km/h)
            distance_km = speed / 3600
            total_distance += distance_km
            total_energy += power / 1000  # W to kW
    
    if total_distance == 0 or total_energy == 0:
        return None
    
    return round(total_distance / total_energy, 2)

def is_home(location):
    """집 위치 판단 (대략적인 위도/경도)"""
    if not location:
        return False
    
    # 집 위치 (예시 - 실제로는 엔지니어님의 집 좌표로 설정 필요)
    HOME_LAT = 37.4979  # 대략적인 좌표
    HOME_LON = 127.0276
    RADIUS_KM = 0.5  # 500m 반경
    
    lat = location.get("latitude", 0)
    lon = location.get("longitude", 0)
    
    # 간단한 거리 계산 (대략적)
    distance = ((lat - HOME_LAT) ** 2 + (lon - HOME_LON) ** 2) ** 0.5
    return distance < (RADIUS_KM / 111)  # 1도 ≈ 111km

def format_daily_summary():
    """일일 주행 요약 포맷팅"""
    if daily_stats["total_distance"] == 0:
        return "📊 오늘 주행 기록 없음"
    
    avg_speed = 0
    if daily_stats["total_time_seconds"] > 0:
        avg_speed = (daily_stats["total_distance"] / daily_stats["total_time_seconds"]) * 3600
    
    avg_efficiency = 0
    if daily_stats["efficiencies"]:
        avg_efficiency = sum(daily_stats["efficiencies"]) / len(daily_stats["efficiencies"])
    
    soc_change = 0
    if daily_stats["start_soc"] and daily_stats["end_soc"]:
        soc_change = daily_stats["start_soc"] - daily_stats["end_soc"]
    
    summary = f"""📊 **오늘의 주행 요약**

🚗 주행거리: {daily_stats['total_distance']:.1f} km
⏱️ 평균속도: {avg_speed:.1f} km/h
⚡ 평균전비: {avg_efficiency:.2f} km/kWh
🔋 배터리 소비: {soc_change:.1f}%
📍 주행 세션: {len(daily_stats['drive_sessions'])}회

시간: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""
    
    return summary

def format_weekly_summary():
    """주간 주행 요약 포맷팅"""
    total_distance = sum(day["distance"] for day in weekly_stats["days"].values())
    total_drives = sum(day["drive_count"] for day in weekly_stats["days"].values())
    
    if total_distance == 0:
        return "📊 이번 주 주행 기록 없음"
    
    avg_efficiency_weekly = 0
    total_energy = sum(day["energy_used"] for day in weekly_stats["days"].values())
    if total_energy > 0:
        avg_efficiency_weekly = total_distance / total_energy
    
    summary = f"""📈 **주간 주행 요약**

🚗 총 주행거리: {total_distance:.1f} km
⏱️ 주행 횟수: {total_drives}회
⚡ 평균전비: {avg_efficiency_weekly:.2f} km/kWh
🔋 총 에너지 소비: {total_energy:.1f} kWh

📅 기간: {weekly_stats['week_start'].strftime('%Y-%m-%d')} ~ {datetime.now().strftime('%Y-%m-%d')}"""
    
    return summary

def send_daily_summary():
    """일일 요약 발송"""
    summary = format_daily_summary()
    send_message(summary)

def send_weekly_summary():
    """주간 요약 발송"""
    today_summary = format_daily_summary()
    weekly_summary = format_weekly_summary()
    combined = f"{today_summary}\n\n{weekly_summary}"
    send_message(combined)

def check_summary_triggers():
    """일일/주간 요약 발송 타이밍 확인"""
    while True:
        now = datetime.now()
        
        # 매일 21:00 (9시) 체크
        if now.hour == 21 and now.minute == 0:
            send_daily_summary()
            time.sleep(60)
        
        # 매주 금요일 21:00 체크
        if now.weekday() == 4 and now.hour == 21 and now.minute == 0:  # 4 = Friday
            send_weekly_summary()
            time.sleep(60)
        
        # 일일 통계 리셋 (자정)
        if now.hour == 0 and now.minute == 0:
            reset_daily_stats()
            time.sleep(60)
        
        # 주간 통계 리셋 (매주 월요일 자정)
        if now.weekday() == 0 and now.hour == 0 and now.minute == 0:
            reset_weekly_stats()
            time.sleep(60)
        
        time.sleep(30)

def reset_daily_stats():
    """일일 통계 리셋"""
    global daily_stats
    daily_stats = {
        "start_odometer": None,
        "end_odometer": None,
        "start_soc": None,
        "end_soc": None,
        "total_distance": 0,
        "total_time_seconds": 0,
        "speeds": [],
        "efficiencies": [],
        "power_readings": [],
        "last_location": None,
        "drive_sessions": [],
        "date": datetime.now().date()
    }

def reset_weekly_stats():
    """주간 통계 리셋"""
    global weekly_stats
    weekly_stats = {
        "days": defaultdict(lambda: {
            "distance": 0,
            "avg_speed": 0,
            "avg_efficiency": 0,
            "energy_used": 0,
            "drive_count": 0
        }),
        "week_start": datetime.now()
    }

def process_data(data):
    """테슬라 Telemetry 데이터 처리"""
    global last_alert_time, current_session, daily_stats
    
    current_time = datetime.now()
    data["ts"] = current_time
    data_window.append(data)
    
    # 3분 윈도우 유지
    while data_window and data_window[0]["ts"] < current_time - timedelta(minutes=WINDOW_SIZE_MINUTES):
        data_window.popleft()
    
    speed = data.get("speed", 0)
    soc = data.get("soc", 0)
    odometer = data.get("odometer", 0)
    location = data.get("location", {})
    power = data.get("power", 0)
    
    # 주행 중 감지
    is_driving = speed > 1
    
    # 주행 시작
    if is_driving and not current_session["driving"]:
        current_session["driving"] = True
        current_session["session_start_time"] = current_time
        current_session["session_start_odometer"] = odometer
        current_session["session_start_soc"] = soc
        
        if daily_stats["start_odometer"] is None:
            daily_stats["start_odometer"] = odometer
        if daily_stats["start_soc"] is None:
            daily_stats["start_soc"] = soc
    
    # 주행 중
    if is_driving:
        current_session["session_speeds"].append(speed)
        daily_stats["speeds"].append(speed)
        daily_stats["power_readings"].append(power)
        daily_stats["last_location"] = location
        
        # 전비 계산 및 경고
        eff = calculate_efficiency(list(data_window))
        if eff:
            current_session["session_efficiencies"].append(eff)
            daily_stats["efficiencies"].append(eff)
            
            if eff < THRESHOLD_EFFICIENCY:
                now = time.time()
                if now - last_alert_time > 60:
                    send_message(f"📉 전비 경고!\n최근 {WINDOW_SIZE_MINUTES}분 평균: **{eff} km/kWh**")
                    last_alert_time = now
    
    # 주행 종료
    elif current_session["driving"] and not is_driving:
        current_session["driving"] = False
        
        # 세션 통계 계산
        if current_session["session_start_time"]:
            session_duration = (current_time - current_session["session_start_time"]).total_seconds()
            session_distance = odometer - current_session["session_start_odometer"]
            session_soc_change = current_session["session_start_soc"] - soc
            
            daily_stats["total_distance"] += session_distance
            daily_stats["total_time_seconds"] += session_duration
            daily_stats["end_odometer"] = odometer
            daily_stats["end_soc"] = soc
            
            session_info = {
                "distance": session_distance,
                "duration": session_duration,
                "soc_change": session_soc_change,
                "avg_speed": sum(current_session["session_speeds"]) / len(current_session["session_speeds"]) if current_session["session_speeds"] else 0
            }
            daily_stats["drive_sessions"].append(session_info)
        
        # 집 도착 확인 (6시 이후)
        if current_time.hour >= 18 and is_home(location):
            send_message("🏠 집에 도착했습니다!\n" + format_daily_summary())
        
        # 세션 초기화
        current_session = {
            "driving": False,
            "session_start_time": None,
            "session_start_odometer": None,
            "session_start_soc": None,
            "session_speeds": [],
            "session_efficiencies": []
        }

if __name__ == "__main__":
    # 명령 감시 스레드
    cmd_thread = threading.Thread(target=check_commands, daemon=True)
    cmd_thread.start()
    
    # 요약 발송 타이밍 체크 스레드
    summary_thread = threading.Thread(target=check_summary_triggers, daemon=True)
    summary_thread.start()
    
    send_message("🚀 두삼이 관제 시스템 v2 가동 시작\n- 일일/주간 주행 요약 기능 추가")
    
    # 표준 입력에서 데이터 읽기
    for line in sys.stdin:
        try:
            data = json.loads(line)
            process_data(data)
        except:
            continue
