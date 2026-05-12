#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import signal
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
import requests

KST = timezone(timedelta(hours=9))
REQUEST_TIMEOUT = int(os.getenv("LIGHT_LOGGG_REQUEST_TIMEOUT", "25"))
DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
DEFAULT_PID_FILE = Path.home() / "light_loggg_tesla" / "polling.pid"
DEFAULT_LOG_FILE = Path.home() / "light_loggg_tesla" / "logs" / "polling.log"
POLLING_SCRIPT_PATH = Path(__file__).parent / "light_loggg_tesla_polling.py"

def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists(): return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("\"").strip("\'")

def now_kst() -> datetime: return datetime.now(KST)

def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists(): return dict(default)
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return dict(default)

def save_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"Error saving JSON: {e}", file=sys.stderr)

def as_float(value: Any) -> Optional[float]:
    try: return float(value) if value is not None and value != "" else None
    except: return None

def parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value: return None
    try: return datetime.fromisoformat(value)
    except: return None

def process_alive(pid_file: Path = DEFAULT_PID_FILE) -> bool:
    if not pid_file.exists(): return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except: return False

def tail_log(log_file: Path = DEFAULT_LOG_FILE, lines: int = 3) -> str:
    if not log_file.exists(): return "로그 없음"
    try:
        res = subprocess.run(["tail", "-n", str(lines), str(log_file)], capture_output=True, text=True)
        return res.stdout.strip() or "최근 로그 없음"
    except: return "로그 확인 실패"

def format_status(state_file: Path) -> str:
    state = load_json(state_file, {})
    last = state.get("last_poll") or {}
    running = process_alive()
    
    last_time = parse_dt(last.get("time"))
    if last_time:
        age = max(0, int((now_kst() - last_time.astimezone(KST)).total_seconds()))
        last_text = f"{last_time.astimezone(KST).strftime('%H:%M:%S')} ({age}초 전)"
    else: last_text = "기록 없음"

    lines = [
        "LIGHT LOGGG 상태",
        f"프로세스: {'✅ 실행 중' if running else '❌ 중지됨'}",
        f"차량: {last.get('vehicle_name') or '-'}",
        f"Tesla 상태: {last.get('status') or '-'}",
        f"최근 폴링: {last_text}",
    ]
    
    # 충전 정보 표시
    c_state = last.get("charging_state")
    if c_state and c_state != "Disconnected":
        pwr = as_float(last.get("charger_power"))
        added = as_float(last.get("charge_energy_added"))
        lines.append(f"⚡ 충전 상태: {c_state}")
        if pwr is not None: lines.append(f"🔌 충전 속도: {pwr:.1f} kW")
        if added is not None: lines.append(f"🔋 충전량: {added:.1f} kWh 추가됨")

    battery = as_float(last.get("battery_level"))
    if battery is not None: lines.append(f"배터리: {battery:.0f}%")
    
    lines.append(f"최근 로그:\n{tail_log()}")
    return "\n".join(lines)

def update_and_restart_polling(telegram_bot: Any, chat_id: str) -> None:
    repo_path = Path(__file__).parent
    try:
        telegram_bot.send(chat_id, "🔄 코드 업데이트 중 (curl)...")
        url = "https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_telegram_bot.py"
        subprocess.run(["curl", "-L", "-o", "light_loggg_telegram_bot.py", url], cwd=repo_path )
        
        # 오프셋 저장 (무한 루프 방지)
        st = load_json(telegram_bot.state_file, {})
        st["last_offset"] = telegram_bot.offset + 1
        save_json(telegram_bot.state_file, st)

        # 재시작
        telegram_bot.send(chat_id, "✅ 업데이트 완료! 재시작합니다...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e: telegram_bot.send(chat_id, f"❌ 오류: {e}")

class TelegramBot:
    def __init__(self, state_file: Path):
        load_dotenv(Path.home() / ".light_loggg.env")
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.state_file = state_file
        self.offset = load_json(self.state_file, {}).get("last_offset", 0)

    def send(self, chat_id: str, text: str):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=REQUEST_TIMEOUT )

    def handle(self, chat_id: str, text: str):
        cmd = (text or "").strip().lower()
        if "/status" in cmd: self.send(chat_id, format_status(self.state_file))
        elif "/update" in cmd: update_and_restart_polling(self, chat_id)

    def run_forever(self):
        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                res = requests.get(url, params={"offset": self.offset + 1, "timeout": 25}, timeout=35 ).json()
                for up in res.get("result", []):
                    self.offset = int(up.get("update_id", 0))
                    st = load_json(self.state_file, {}); st["last_offset"] = self.offset; save_json(self.state_file, st)
                    msg = up.get("message") or {}; self.handle(str(msg.get("chat", {}).get("id")), msg.get("text"))
            except: time.sleep(5)

if __name__ == "__main__":
    state_file = DEFAULT_STATE_FILE.expanduser()
    TelegramBot(state_file).run_forever()
