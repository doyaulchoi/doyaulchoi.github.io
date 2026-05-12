import os
import json
import requests
import subprocess
import time
import logging

# 설정 로드
CONFIG_PATH = os.path.expanduser("~/.light_loggg_config.json")
STATE_PATH = os.path.expanduser("~/.light_loggg_state.json")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser("~/light_loggg_bot.log")),
        logging.StreamHandler()
    ]
)

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def get_offset():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, 'r') as f:
            state = json.load(f)
            return state.get("bot_offset", 0)
    return 0

def save_offset(offset):
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, 'r') as f:
            state = json.load(f)
    state["bot_offset"] = offset
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)

def send_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def handle_update(config):
    repo_url = "https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_tesla"
    files = ["light_loggg_telegram_bot.py", "light_loggg_tesla_polling.py"]
    
    results = []
    for file in files:
        cmd = f"curl -L {repo_url}/{file} -o ~/light_loggg_tesla/{file}"
        process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if process.returncode == 0:
            results.append(f"✅ {file} 업데이트 성공")
        else:
            results.append(f"❌ {file} 업데이트 실패: {process.stderr}")
    
    msg = "🔄 시스템 업데이트 결과:\n" + "\n".join(results)
    msg += "\n\n시스템을 재시작합니다..."
    send_message(config['telegram_token'], config['chat_id'], msg)
    
    # 프로세스 재시작 로직
    # 1. 폴링 프로세스 종료 및 재시작
    subprocess.run("pkill -f light_loggg_tesla_polling.py", shell=True)
    subprocess.run("nohup python3 ~/light_loggg_tesla/light_loggg_tesla_polling.py > ~/light_loggg_polling.log 2>&1 &", shell=True)
    
    # 2. 봇 프로세스 재시작 (자기 자신을 다시 실행하고 현재 프로세스 종료)
    os.execv('/usr/bin/python3', ['python3', os.path.expanduser('~/light_loggg_tesla/light_loggg_telegram_bot.py')])

def main():
    config = load_config()
    if not config:
        logging.error("Config not found")
        return

    token = config['telegram_token']
    offset = get_offset()
    
    logging.info("Telegram Bot Started")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url).json()
            
            if response.get("ok"):
                for update in response.get("result", []):
                    offset = update["update_id"] + 1
                    save_offset(offset)
                    
                    message = update.get("message", {})
                    text = message.get("text", "")
                    chat_id = message.get("chat", {}).get("id")
                    
                    if str(chat_id) != str(config['chat_id']):
                        continue

                    if text == "/status":
                        # 폴링 스크립트가 저장한 최신 상태 읽기
                        if os.path.exists(STATE_PATH):
                            with open(STATE_PATH, 'r') as f:
                                state = json.load(f)
                            status_msg = f"📊 현재 상태: {state.get('last_charging_state', '알 수 없음')}"
                        else:
                            status_msg = "📊 상태 정보를 불러올 수 없습니다."
                        send_message(token, chat_id, status_msg)
                    
                    elif text == "/update":
                        handle_update(config)
                    
                    elif text.startswith("/"):
                        help_msg = ("❓ 알 수 없는 명령어입니다.\n\n"
                                    "사용 가능한 명령어:\n"
                                    "/status - 현재 차량 상태 확인\n"
                                    "/update - 시스템 최신 버전 업데이트")
                        send_message(token, chat_id, help_msg)
                        
        except Exception as e:
            logging.error(f"Error in bot loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
