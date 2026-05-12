import os
import time
import json
import requests
import logging
from datetime import datetime, timedelta

# 설정 로드
CONFIG_PATH = os.path.expanduser("~/.light_loggg_config.json")
STATE_PATH = os.path.expanduser("~/.light_loggg_state.json")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser("~/light_loggg_polling.log")),
        logging.StreamHandler()
    ]
)

def load_config():
    if not os.path.exists(CONFIG_PATH):
        logging.error("Config file not found.")
        return None
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, 'r') as f:
            return json.load(f)
    return {"last_charging_state": None, "charging_start_time": None, "stage2_sent": False}

def save_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)

def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logging.error(f"Failed to send telegram message: {e}")

def get_tesla_data(config):
    # 실제 Tesla Fleet API 호출 로직 (사용자의 기존 토큰 및 VIN 사용)
    # 여기서는 예시 구조를 반환하며, 실제 구현 시 config의 정보를 사용합니다.
    headers = {"Authorization": f"Bearer {config['tesla_access_token']}"}
    url = f"https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/{config['vin']}/vehicle_data"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json()['response']
        else:
            logging.error(f"Tesla API error: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Tesla API request failed: {e}")
        return None

def main():
    config = load_config()
    if not config:
        return

    state = load_state()
    logging.info("Tesla Polling Started")

    while True:
        data = get_tesla_data(config)
        if data:
            charge_state = data.get('charge_state', {})
            current_charging_state = charge_state.get('charging_state')
            battery_level = charge_state.get('battery_level')
            
            # 1단계: 충전 시작 즉시 알림
            if current_charging_state == "Charging" and state.get("last_charging_state") != "Charging":
                msg = f"⚡ 충전이 시작되었습니다!\n현재 배터리: {battery_level}%"
                send_telegram_message(config['telegram_token'], config['chat_id'], msg)
                state["charging_start_time"] = datetime.now().isoformat()
                state["stage2_sent"] = False
            
            # 2단계: 충전 시작 3분 후 상세 알림
            if current_charging_state == "Charging" and not state.get("stage2_sent"):
                start_time_str = state.get("charging_start_time")
                if start_time_str:
                    start_time = datetime.fromisoformat(start_time_str)
                    if datetime.now() > start_time + timedelta(minutes=3):
                        charge_rate = charge_state.get('charge_rate', 0)
                        power = charge_state.get('charger_power', 0)
                        eta = charge_state.get('minutes_to_full_charge', 0)
                        msg = (f"ℹ️ 충전 상세 정보 (3분 경과)\n"
                               f"배터리: {battery_level}%\n"
                               f"충전 속도: {charge_rate} km/h\n"
                               f"충전 전력: {power} kW\n"
                               f"완충까지 남은 시간: {eta}분")
                        send_telegram_message(config['telegram_token'], config['chat_id'], msg)
                        state["stage2_sent"] = True

            # 충전 중단 시 상태 초기화
            if current_charging_state != "Charging" and state.get("last_charging_state") == "Charging":
                msg = f"🛑 충전이 중단되었습니다.\n최종 배터리: {battery_level}%"
                send_telegram_message(config['telegram_token'], config['chat_id'], msg)
                state["charging_start_time"] = None
                state["stage2_sent"] = False

            state["last_charging_state"] = current_charging_state
            save_state(state)
        
        time.sleep(60)  # 1분 간격 폴링

if __name__ == "__main__":
    main()
