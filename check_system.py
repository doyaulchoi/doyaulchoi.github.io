import os
import json
import requests
import subprocess
import sys

CONFIG_PATH = os.path.expanduser("~/.light_loggg_config.json")
STATE_PATH = os.path.expanduser("~/.light_loggg_state.json")
BOT_SCRIPT_PATH = os.path.expanduser("~/light_loggg_tesla/light_loggg_telegram_bot.py")
POLLING_SCRIPT_PATH = os.path.expanduser("~/light_loggg_tesla/light_loggg_tesla_polling.py")

def print_status(message, status):
    print(f"[{\'✅\' if status else \'❌\'}] {message}")

def check_file_exists(path, description):
    exists = os.path.exists(path)
    print_status(f"{description} 파일 존재 여부: {path}", exists)
    return exists

def check_json_content(path, required_keys, description):
    if not os.path.exists(path):
        return False, f"{description} 파일이 존재하지 않습니다."
    try:
        with open(path, \'r\') as f:
            data = json.load(f)
            missing_keys = [key for key in required_keys if key not in data]
            if missing_keys:
                return False, f"{description} 파일에 필수 키 누락: {\', \'.join(missing_keys)}"
            return True, f"{description} 파일 내용 유효함."
    except json.JSONDecodeError:
        return False, f"{description} 파일 JSON 형식 오류."
    except Exception as e:
        return False, f"{description} 파일 읽기 오류: {e}"

def check_telegram_connectivity(token, chat_id):
    if not token or not chat_id:
        return False, "텔레그램 토큰 또는 채팅 ID가 없습니다."
    test_message_url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        response = requests.get(test_message_url, timeout=5 )
        if response.status_code == 200 and response.json().get("ok"):
            return True, "텔레그램 API 연결 성공."
        else:
            return False, f"텔레그램 API 연결 실패: {response.status_code} - {response.text}"
    except requests.exceptions.RequestException as e:
        return False, f"텔레그램 API 요청 오류: {e}"

def check_process_running(script_name):
    try:
        # \'ps aux\' 대신 \'pgrep -f\'를 사용하여 정확도를 높입니다.
        # Termux 환경에서 \'ps aux\'가 모든 정보를 제공하지 않을 수 있습니다.
        result = subprocess.run([\'pgrep\', \'-f\', script_name], capture_output=True, text=True, check=False)
        pids = result.stdout.strip().split(\'\\n\')
        pids = [p for p in pids if p.isdigit()]
        if pids:
            return True, f"{script_name} 프로세스 실행 중 (PID: {\', \'.join(pids)})."
        return False, f"{script_name} 프로세스가 실행 중이 아닙니다."
    except Exception as e:
        return False, f"프로세스 확인 중 오류 발생: {e}"

def main():
    print("\\n--- Tesla Monitoring System 진단 시작 ---")

    # 1. 설정 파일 존재 및 내용 확인
    config_exists = check_file_exists(CONFIG_PATH, "설정")
    config_valid, config_msg = check_json_content(CONFIG_PATH, ["telegram_token", "chat_id", "tesla_access_token", "vin"], "설정")
    print_status(config_msg, config_valid)

    config = None
    if config_valid:
        with open(CONFIG_PATH, \'r\') as f:
            config = json.load(f)

    # 2. 상태 파일 존재 및 내용 확인
    state_exists = check_file_exists(STATE_PATH, "상태")
    # 상태 파일은 필수가 아니므로, 내용 유효성 검사는 하지 않습니다.

    # 3. 스크립트 파일 존재 확인
    bot_script_exists = check_file_exists(BOT_SCRIPT_PATH, "텔레그램 봇 스크립트")
    polling_script_exists = check_file_exists(POLLING_SCRIPT_PATH, "테슬라 폴링 스크립트")

    # 4. 텔레그램 연결 확인
    telegram_connected = False
    telegram_msg = ""
    if config:
        telegram_connected, telegram_msg = check_telegram_connectivity(config.get("telegram_token"), config.get("chat_id"))
    print_status(telegram_msg, telegram_connected)

    # 5. 프로세스 실행 여부 확인
    bot_running, bot_running_msg = check_process_running("light_loggg_telegram_bot.py")
    print_status(bot_running_msg, bot_running)

    polling_running, polling_running_msg = check_process_running("light_loggg_tesla_polling.py")
    print_status(polling_running_msg, polling_running)

    print("\\n--- 진단 결과 요약 ---")
    if config_valid and bot_script_exists and polling_script_exists and telegram_connected and bot_running and polling_running:
        print("✅ 모든 시스템 구성 요소가 정상적으로 작동하는 것으로 보입니다.")
        print("   만약 여전히 문제가 있다면, 텔레그램 봇의 `chat_id`가 올바른지 다시 확인해 주세요.")
    else:
        print("❌ 시스템에 문제가 발견되었습니다. 아래 지침을 따라 문제를 해결해 주세요.")
        print("\\n--- 문제 해결 가이드 ---")
        if not config_exists:
            print(f"1. 설정 파일이 없습니다. `~/.light_loggg_config.json` 파일을 생성하고 필수 정보를 입력해주세요.")
        elif not config_valid:
            print(f"1. 설정 파일 내용이 올바르지 않습니다. `~/.light_loggg_config.json` 파일을 열어 필수 키(`telegram_token`, `chat_id`, `tesla_access_token`, `vin`)가 모두 있는지, JSON 형식이 올바른지 확인해주세요.")
        
        if not bot_script_exists or not polling_script_exists:
            print(f"2. 스크립트 파일이 없습니다. `~/light_loggg_tesla/` 디렉토리에 `light_loggg_telegram_bot.py`와 `light_loggg_tesla_polling.py` 파일이 있는지 확인해주세요. 없다면, 이전 안내에 따라 다시 다운로드 해주세요.")
        
        if not telegram_connected:
            print(f"3. 텔레그램 API 연결에 문제가 있습니다. 설정 파일의 `telegram_token`과 `chat_id`가 올바른지 확인하고, 텔레그램 봇이 차단되지 않았는지 확인해주세요.")
        
        if not bot_running or not polling_running:
            print(f"4. 봇 또는 폴링 프로세스가 실행 중이 아닙니다. 이전 안내에 따라 `pkill -f light_loggg_` 명령어로 기존 프로세스를 모두 종료한 후, `nohup` 명령어로 두 스크립트를 다시 실행해주세요.")
        
        print("\\n--- 자동 복구 시도 ---")
        print("기존 프로세스를 종료하고 최신 스크립트를 다운로드 후 재시작합니다.")
        subprocess.run("pkill -f light_loggg_", shell=True)
        subprocess.run("mkdir -p ~/light_loggg_tesla", shell=True)
        subprocess.run("curl -L https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_tesla/light_loggg_telegram_bot.py -o ~/light_loggg_tesla/light_loggg_telegram_bot.py", shell=True )
        subprocess.run("curl -L https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_tesla/light_loggg_tesla_polling.py -o ~/light_loggg_tesla/light_loggg_tesla_polling.py", shell=True )
        subprocess.run("nohup python3 ~/light_loggg_tesla/light_loggg_telegram_bot.py > ~/light_loggg_bot.log 2>&1 &", shell=True)
        subprocess.run("nohup python3 ~/light_loggg_tesla/light_loggg_tesla_polling.py > ~/light_loggg_polling.log 2>&1 &", shell=True)
        print("자동 복구 명령을 실행했습니다. 잠시 후 다시 이 스크립트를 실행하여 상태를 확인하거나, 텔레그램에서 /status 명령어를 시도해 보세요.")

    print("\\n--- Tesla Monitoring System 진단 종료 ---")

if __name__ == "__main__":
    main()
