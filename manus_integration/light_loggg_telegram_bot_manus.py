#!/usr/bin/env python3
"""Telegram command listener for LIGHT LOGGG Tesla polling.

This bot listens for Telegram commands such as /status, /daily, /weekly,
and /update. It also forwards unknown messages to Manus for a response.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List

import requests


KST = timezone(timedelta(hours=9))

REQUEST_TIMEOUT = int(os.getenv("LIGHT_LOGGG_REQUEST_TIMEOUT", "25"))

RAW_BASE = os.getenv(
    "LIGHT_LOGGG_RAW_BASE",
    "https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main",
)
REPO_URL = os.getenv(
    "LIGHT_LOGGG_REPO_URL",
    "https://github.com/doyaulchoi/doyaulchoi.github.io.git",
)

REPO_BRANCH = os.getenv("LIGHT_LOGGG_REPO_BRANCH", "main")

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"

DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
DEFAULT_PID_FILE = APP_DIR / "polling.pid"
BOT_PID_FILE = APP_DIR / "telegram_bot.pid"
BOT_OFFSET_FILE = APP_DIR / "telegram_bot_offset.json"

PUBLIC_CONFIG_FILE = APP_DIR / "light_loggg_public_config.json"

DEFAULT_LOG_FILE = LOG_DIR / "polling.log"
BOT_LOG_FILE = LOG_DIR / "telegram_bot.log"
COMMAND_SERVER_LOG_FILE = LOG_DIR / "command_server.log"
UPDATE_LOG_FILE = LOG_DIR / "update.log"

COMMAND_SERVER_PID_FILE = APP_DIR / "command_server.pid"

POLLING_SCRIPT_PATH = APP_DIR / "light_loggg_tesla_polling.py"
BOT_SCRIPT_PATH = APP_DIR / "light_loggg_telegram_bot.py"
COMMAND_SERVER_SCRIPT_PATH = APP_DIR / "light_loggg_command_server.py"
CHECK_SCRIPT_PATH = APP_DIR / "check_system.py"

BOOT_SOURCE_FILE = APP_DIR / "start-light-loggg.sh"
BOOT_TARGET_DIR = Path.home() / ".termux" / "boot"
BOOT_TARGET_FILE = BOOT_TARGET_DIR / "start-light-loggg.sh"

COMMAND_FILE = APP_DIR / "command.json"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("\'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_kst() -> datetime:
    return datetime.now(KST)


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_update_log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with UPDATE_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(f"[{now_kst().isoformat()}] {text}\n")


def read_pid(pid_file: Path) -> Optional[int]:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def process_alive(pid_file: Path = DEFAULT_PID_FILE) -> Optional[bool]:
    pid = read_pid(pid_file)
    if pid is None: return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError: return False
    except PermissionError: return True
    except Exception: return None


def split_telegram_text(text: str, limit: int = 4000) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1: cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks


def load_bot_offset() -> int:
    return int(load_json(BOT_OFFSET_FILE, {"offset": 0}).get("offset", 0))


def save_bot_offset(offset: int) -> None:
    atomic_write_json(BOT_OFFSET_FILE, {"offset": offset})


def format_status(state_file: Path) -> str:
    state = load_json(state_file, {})
    last_poll = state.get("last_poll") or {}
    status = last_poll.get("status") or "unknown"
    battery = last_poll.get("battery_level")
    battery_text = f"{battery}%" if battery is not None else "확인 불가"
    charging_state = last_poll.get("charging_state") or "None"
    
    lines = [
        "두삼이 현재 상태",
        f"- 차량 상태: {status}",
        f"- 배터리: {battery_text}",
        f"- 충전 상태: {charging_state}",
        f"- 마지막 업데이트: {last_poll.get(\'time\', \'기록 없음\')}"
    ]
    return "\n".join(lines)


def format_daily_summary(state: Dict[str, Any]) -> str:
    daily = state.get("daily") or {}
    dist = daily.get("total_distance_km", 0.0)
    return f"오늘의 주행 요약\n- 총 주행거리: {dist:.2f} km"


def format_weekly_summary(state: Dict[str, Any]) -> str:
    weekly = state.get("weekly") or {}
    dist = weekly.get("total_distance_km", 0.0)
    return f"이번 주 주행 요약\n- 총 주행거리: {dist:.2f} km"


def run_command(args: List[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def git_update_repo() -> str:
    run_command(["git", "remote", "set-url", "origin", REPO_URL], cwd=APP_DIR)
    run_command(["git", "fetch", "origin", REPO_BRANCH], cwd=APP_DIR)
    run_command(["git", "reset", "--hard", f"origin/{REPO_BRANCH}"], cwd=APP_DIR)
    return "Git 업데이트 완료"


def stop_polling_process() -> str:
    pid = read_pid(DEFAULT_PID_FILE)
    if pid:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    DEFAULT_PID_FILE.unlink(missing_ok=True)
    return "Polling 프로세스 종료 시도"


def start_polling_process() -> int:
    python_executable = sys.executable
    log_file = DEFAULT_LOG_FILE.open("a", encoding="utf-8")
    process = subprocess.Popen([python_executable, str(POLLING_SCRIPT_PATH)], cwd=str(APP_DIR), stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    DEFAULT_PID_FILE.write_text(str(process.pid) + "\n", encoding="utf-8")
    return process.pid


def run_system_check() -> tuple[int, str]:
    if not CHECK_SCRIPT_PATH.exists(): return 1, "check_system.py 없음"
    res = run_command([sys.executable, str(CHECK_SCRIPT_PATH)], cwd=APP_DIR)
    return res.returncode, res.stdout


def summarize_check_result(exit_code: int, output: str) -> tuple[bool, str]:
    return (exit_code == 0, "시스템 점검 완료" if exit_code == 0 else f"점검 실패: {output}")


def write_command(command: Dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(COMMAND_FILE, command)


def update_and_restart_polling(telegram_bot: Any, chat_id: str) -> None:
    try:
        telegram_bot.send(chat_id, "업데이트를 시작합니다...")
        git_update_repo()
        stop_polling_process()
        start_polling_process()
        telegram_bot.send(chat_id, "업데이트 및 재시작 완료!")
        time.sleep(1)
        python_executable = sys.executable
        subprocess.Popen([python_executable, str(BOT_SCRIPT_PATH)], cwd=str(APP_DIR), start_new_session=True)
        os._exit(0)
    except Exception as e:
        telegram_bot.send(chat_id, f"업데이트 중 오류 발생: {e}")


class ManusClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("MANUS_API_KEY")
        self.api_base = "https://api.manus.ai/v2"
        self.task_id = "agent-default-main_task"

    def ask(self, text: str) -> str:
        if not self.api_key:
            return "MANUS_API_KEY가 설정되지 않아 답변을 드릴 수 없습니다."

        headers = {"x-manus-api-key": self.api_key, "Content-Type": "application/json"}
        
        # 1. Send message
        try:
            send_url = f"{self.api_base}/task.sendMessage"
            payload = {"task_id": self.task_id, "message": {"content": text}}
            res = requests.post(send_url, headers=headers, json=payload, timeout=30)
            if res.status_code != 200:
                return f"Manus API 호출 실패 (send): {res.status_code}"
        except Exception as e:
            return f"Manus API 연결 오류: {e}"

        # 2. Poll for reply
        list_url = f"{self.api_base}/task.listMessages"
        params = {"task_id": self.task_id, "limit": 5, "order": "desc"}
        
        for _ in range(15): # Max 30 seconds
            time.sleep(2)
            try:
                res = requests.get(list_url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    messages = data.get("messages", [])
                    for msg in messages:
                        if msg.get("role") == "assistant" and msg.get("content"):
                            return msg.get("content")
            except Exception: pass
            
        return "Manus의 답변이 늦어지고 있습니다. 잠시 후 다시 시도해 주세요."


class TelegramBot:
    def __init__(self, state_file: Path) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.state_file = state_file
        self.manus = ManusClient()
        if not self.token: raise RuntimeError("TELEGRAM_BOT_TOKEN이 필요합니다.")
        self.offset = load_bot_offset()

    def send(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for chunk in split_telegram_text(text):
            try: requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=REQUEST_TIMEOUT)
            except Exception: pass

    def handle(self, chat_id: str, text: str) -> None:
        text = (text or "").strip()
        if not text: return
        
        command = text.split()[0].lower()
        state = load_json(self.state_file, {})

        if command in {"/start", "start"}:
            self.send(chat_id, "🤖 LIGHT LOGGG 명령어\n\n/status, /daily, /weekly, /update, /poll_now, /driving_start, /driving_stop\n\n명령어 외의 텍스트를 입력하면 Manus가 답변합니다.")
        elif command == "/status": self.send(chat_id, format_status(self.state_file))
        elif command == "/daily": self.send(chat_id, format_daily_summary(state))
        elif command == "/weekly": self.send(chat_id, format_weekly_summary(state))
        elif command == "/update": update_and_restart_polling(self, chat_id)
        elif command == "/poll_now":
            write_command({"command": "poll_now", "time": now_kst().isoformat()})
            self.send(chat_id, "명령 생성 완료")
        elif command == "/driving_start":
            write_command({"command": "driving_start", "seconds": 180, "time": now_kst().isoformat()})
            self.send(chat_id, "주행 boost 요청됨")
        elif command == "/driving_stop":
            write_command({"command": "driving_stop", "time": now_kst().isoformat()})
            self.send(chat_id, "주행 boost 해제됨")
        else:
            # Forward to Manus
            self.send(chat_id, "🔍 Manus에게 물어보는 중...")
            reply = self.manus.ask(text)
            self.send(chat_id, reply)

    def run_forever(self) -> None:
        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                res = requests.get(url, params={"offset": self.offset + 1, "timeout": 25}, timeout=35).json()
                if not res.get("ok"):
                    time.sleep(5)
                    continue
                for update in res.get("result", []):
                    self.offset = max(self.offset, int(update.get("update_id", 0)))
                    save_bot_offset(self.offset)
                    msg = update.get("message") or {}
                    cid = str((msg.get("chat") or {}).get("id") or "")
                    if cid == self.chat_id: self.handle(cid, msg.get("text", ""))
            except Exception: time.sleep(5)


def main() -> int:
    load_dotenv(Path(".env"))
    load_dotenv(Path.home() / ".light_loggg.env")
    TelegramBot(DEFAULT_STATE_FILE).run_forever()
    return 0

if __name__ == "__main__":
    main()
