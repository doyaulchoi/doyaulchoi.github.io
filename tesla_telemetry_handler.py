import sys
import json
import requests
from datetime import datetime, timedelta
from collections import deque

# 텔레그램 설정
TELEGRAM_TOKEN = "8776022575:AAFvPkYGd0rLMh15CqzVsiKkY69YniOgvM0"
CHAT_ID = "8792879646"

# 전비 모니터링 설정
WINDOW_SIZE_MINUTES = 3
THRESHOLD_EFFICIENCY = 4.5

# 데이터 저장을 위한 큐 (최근 3분 데이터)
# 데이터가 약 1초마다 들어온다고 가정할 때 (3분 = 180개)
data_window = deque()

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send telegram message: {e}")

def calculate_efficiency(data_points):
    if not data_points:
        return None
    
    # 평균 속도 (mph -> kmh) 및 평균 출력 (kW) 계산
    # 테슬라 Telemetry 필드명은 실제 서버 구현에 따라 다를 수 있으므로 
    # 일반적인 필드명(speed, power)을 기준으로 작성합니다.
    total_speed_kmh = 0
    total_power_kw = 0
    count = 0
    
    for dp in data_points:
        speed = dp.get("speed", 0) * 1.60934 # mph to kmh
        power = dp.get("power", 0) # kW (출력)
        
        # 주행 중인 데이터만 포함 (속도가 0 이상일 때)
        if speed > 0:
            total_speed_kmh += speed
            total_power_kw += abs(power) # 소비 전력 절대값
            count += 1
            
    if count == 0 or total_power_kw == 0:
        return None
        
    avg_speed = total_speed_kmh / count
    avg_power = total_power_kw / count
    
    # 전비 계산: km/h / kW = km/kWh
    efficiency = avg_speed / avg_power
    return round(efficiency, 2)

def process_telemetry_data(data):
    current_time = datetime.now()
    data["timestamp_internal"] = current_time
    data_window.append(data)
    
    # 3분 이상 된 데이터 삭제
    while data_window and data_window[0]["timestamp_internal"] < current_time - timedelta(minutes=WINDOW_SIZE_MINUTES):
        data_window.popleft()
        
    # 주행 중일 때만 전비 계산 및 알림 (최근 데이터 기준 속도가 있을 때)
    if data.get("speed", 0) > 0:
        eff = calculate_efficiency(data_window)
        
        if eff and eff < THRESHOLD_EFFICIENCY:
            # 너무 잦은 알림 방지 (마지막 알림 후 1분 대기 로직 등 추가 가능)
            send_telegram_message(f"📉 **전비 경고!**\n최근 {WINDOW_SIZE_MINUTES}분 평균 전비가 **{eff} km/kWh**로 떨어졌습니다.\n(기준: {THRESHOLD_EFFICIENCY} km/kWh)")

if __name__ == "__main__":
    print("Tesla Telemetry Efficiency Monitor Started...")
    send_telegram_message("🚀 **두삼이 전비 모니터링 시스템이 시작되었습니다.**\n- 기준: 최근 3분 평균 4.5 km/kWh 이하 시 알림")
    
    for line in sys.stdin:
        try:
            # Telemetry 서버의 로그 출력 형태에 따라 파싱 로직이 달라질 수 있습니다.
            # 여기서는 JSON 형태의 로그 라인을 가정합니다.
            data = json.loads(line)
            process_telemetry_data(data)
        except:
            continue
