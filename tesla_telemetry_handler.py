import sys
import json
import requests
from datetime import datetime, timedelta

# 텔레그램 설정
TELEGRAM_TOKEN = "8776022575:AAFvPkYGd0rLMh15CqzVsiKkY69YniOgvM0"
CHAT_ID = "8792879646"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send telegram message: {e}")

def process_telemetry_data(data):
    # 테슬라 Telemetry 데이터는 보통 Protobuf 형식이지만, 
    # 여기서는 서버가 파싱하여 JSON으로 넘겨준다고 가정하고 로직을 짭니다.
    
    # 예시: 특정 조건 체크
    # 1. 과속 알림 (110km/h 초과)
    speed_mph = data.get("speed", 0)
    speed_kmh = round(speed_mph * 1.60934, 1)
    if speed_kmh > 110:
        send_telegram_message(f"⚠️ **과속 주의!** 현재 속도: {speed_kmh} km/h")

    # 2. 배터리 저전압 알림 (20% 이하)
    battery = data.get("battery_level", 100)
    if battery <= 20:
        send_telegram_message(f"🚨 **배터리 부족!** 현재 잔량: {battery}%")

    # 3. 목적지 도착 알림 (특정 좌표 범위 내)
    # (위치 기반 로직 추가 가능)

if __name__ == "__main__":
    # 서버로부터 파이프를 통해 실시간 데이터를 전달받는 구조
    print("Tesla Telemetry Handler Started...")
    send_telegram_message("🚀 **두삼이 관제 시스템이 시작되었습니다.** (미패드 서버)")
    
    for line in sys.stdin:
        try:
            data = json.loads(line)
            process_telemetry_data(data)
        except:
            continue
