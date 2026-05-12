#!/usr/bin/env python3
"""Telegram command listener for LIGHT LOGGG Tesla polling.

The Tesla polling process sends proactive alerts, while this process listens for
Telegram commands such as /status, /daily, and /weekly. It is intentionally
separate so the Tesla API polling loop remains simple and resilient on Termux.
"""

from __future__ import annotations

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
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)

def save_json(path: Path, data: Dict[str, Any]) -> None:
    """JSON 데이터를 파일에 안전하게 저장합니다."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving JSON to {path}: {e}", file=sys.stderr)


def as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def process_alive(pid_file: Path = DEFAULT_PID_FILE) -> Optional[bool]:
    if not pid_file.exists():
        return None
    try:
        pid_text = pid_file.read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except Exception:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def tail_log(log_file: Path = DEFAULT_LOG_FILE, lines: int = 3) -> str:
    if not log_file.exists():
        return "로그 파일 없음"
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_file)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = (result.stdout or result.stderr or "").strip()
        return text[-1200:] if text else "최근 로그 없음"
    except Exception:
        return "로그 확인 실패"


def format_daily_summary(state: Dict[str, Any]) -> str:
    daily = state.get("daily") or {}
    distance = float(daily.get("total_distance_km") or 0)
    seconds = float(daily.get("total_time_seconds") or 0)
    avg_speed = distance / (seconds / 3600) if seconds > 0 else 0
    effs = daily.get("efficiencies") or []
    avg_eff = sum(effs) / len(effs) if effs else 0
    start_soc = daily.get("start_soc")
    end_soc = daily.get("end_soc")
    if isinstance(start_soc, (int, float)) and isinstance(end_soc, (int, float)):
        soc_text = f"{start_soc:.0f}% -> {end_soc:.0f}% ({start_soc - end_soc:.0f}%p 사용)"
    else:
        soc_text = "확인 부족"
    return (
        f"오늘의 주행 요약 {daily.get('date') or '-'}\n"
        f"주행거리 {distance:.2f} km\n"
        f"주행시간 {seconds / 60:.0f}분\n"
        f"평균속도 {avg_speed:.1f} km/h\n"
        f"평균전비 {avg_eff:.2f} km/kWh\n"
        f"배터리 {soc_text}\n"
        f"주행횟수 {len(daily.get('drive_sessions') or [])}회\n"
        f"급가속 {int(daily.get('accel_count') or 0)}회, 급감속 {int(daily.get('decel_count') or 0)}회"
    )


def format_weekly_summary(state: Dict[str, Any]) -> str:
    weekly = state.get("weekly") or {}
    distance = float(weekly.get("total_distance_km") or 0)
    seconds = float(weekly.get("total_time_seconds") or 0)
    energy = float(weekly.get("total_energy_kwh") or 0)
    avg_eff = distance / energy if energy > 0 else 0
    return (
        f"주간 주행 요약 {weekly.get('week') or '-'}\n"
        f"누적거리 {distance:.2f} km\n"
        f"누적시간 {seconds / 60:.0f}분\n"
        f"평균전비 {avg_eff:.2f} km/kWh\n"
        f"주행횟수 {int(weekly.get('drive_count') or 0)}회"
    )


def format_status(state_file: Path) -> str:
    state = load_json(state_file, {})
    last = state.get("last_poll") or {}
    running = process_alive()
    running_text = "확인됨" if running is True else "중지됨" if running is False else "PID 파일 없음"

    last_time = parse_dt(last.get("time"))
    if last_time:
        age_seconds = max(0, int((now_kst() - last_time.astimezone(KST)).total_seconds()))
        last_text = f"{last_time.astimezone(KST).strftime('%H:%M:%S')} ({age_seconds}초 전)"
    else:
        last_text = "기록 없음"

    battery = as_float(last.get("battery_level"))
    speed = as_float(last.get("speed_kmh"))
    odometer = as_float(last.get("odometer_km"))
    next_seconds = last.get("next_seconds")
    lines = [
        "LIGHT LOGGG 상태",
        f"프로세스: {running_text}",
        f"차량: {last.get('vehicle_name') or '-'}",
        f"Tesla 상태: {last.get('status') or '-'}",
        f"최근 폴링: {last_text}",
    ]
    if isinstance(next_seconds, (int, float)):
        lines.append(f"다음 주기: {int(next_seconds)}초")
    if battery is not None:
        lines.append(f"배터리: {battery:.0f}%")
    if speed is not None:
        lines.append(f"속도: {speed:.1f} km/h")
    if odometer is not None:
        lines.append(f"누적 주행거리: {odometer:.1f} km")
    lines.append("최근 로그:")
    lines.append(tail_log())
    return "\n".join(lines)


def update_and_restart_polling(telegram_bot: Any, chat_id: str) -> None:
    repo_path = Path(__file__).parent
    try:
        # 1. Curl update (로그인 없이 파일 직접 다운로드)
        telegram_bot.send(chat_id, "🔄 코드 업데이트 중 (curl)...")
        url = "https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_telegram_bot.py"
        result = subprocess.run(["curl", "-L", "-o", "light_loggg_telegram_bot.py", url], cwd=repo_path, capture_output=True, text=True  )
        
        if result.returncode != 0:
            telegram_bot.send(chat_id, f"❌ 업데이트 실패:\n{result.stderr}")
            return

        # 2. 현재 오프셋 강제 저장 (무한 루프 방지 핵심)
        st = load_json(telegram_bot.state_file, {})
        st["last_offset"] = telegram_bot.offset + 1
        save_json(telegram_bot.state_file, st)

        # 3. 기존 폴링 프로세스 중지
        telegram_bot.send(chat_id, "🛑 기존 폴링 프로세스 중지 중...")
        if DEFAULT_PID_FILE.exists():
            try:
                pid = int(DEFAULT_PID_FILE.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
                if process_alive(DEFAULT_PID_FILE):
                    os.kill(pid, signal.SIGKILL)
                DEFAULT_PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass

        # 4. 봇 자체를 재시작 (새로 받은 코드를 적용하기 위함)
        telegram_bot.send(chat_id, "✅ 업데이트 완료! 봇을 재시작하여 새 코드를 적용합니다...")
        
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        telegram_bot.send(chat_id, f"❌ 업데이트 중 오류 발생: {e}")


class TelegramBot:
    def __init__(self, state_file: Path) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.state_file = state_file
        # 시작할 때 저장된 오프셋 불러오기 (무한 루프 방지)
        state = load_json(self.state_file, {})
        self.offset = state.get("last_offset", 0)
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_TOKEN 환경변수가 필요합니다.")

    def send(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        res = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=REQUEST_TIMEOUT )
        if res.status_code >= 400:
            print(f"Telegram sendMessage HTTP {res.status_code}: {res.text[:200]}", file=sys.stderr)

    def allowed(self, chat_id: str) -> bool:
        if self.chat_id:
            return str(chat_id) == str(self.chat_id)
        self.chat_id = str(chat_id)
        return True

    def handle(self, chat_id: str, text: str) -> None:
        command = (text or "").strip().split()[0].lower()
        state = load_json(self.state_file, {})
        
        if command in {"/start", "start"}:
            self.send(chat_id, "LIGHT LOGGG 명령: /status, /daily, /weekly, /update")
        elif command in {"/status", "status"}:
            self.send(chat_id, format_status(self.state_file))
        elif command in {"/daily", "daily"}:
            self.send(chat_id, format_daily_summary(state))
        elif command in {"/weekly", "weekly"}:
            self.send(chat_id, format_weekly_summary(state))
        elif command in {"/update", "update"}:
            update_and_restart_polling(self, chat_id)
        else:
            self.send(chat_id, "알 수 없는 명령어입니다. 사용 가능한 명령어: /status, /daily, /weekly, /update")

    def run_forever(self) -> None:
        print(f"LIGHT LOGGG Telegram command bot started with offset: {self.offset}", flush=True)
        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                res = requests.get(url, params={"offset": self.offset + 1, "timeout": 25}, timeout=35 )
                data = res.json()
                if not data.get("ok"):
                    print(f"Telegram getUpdates error: {data}", file=sys.stderr, flush=True)
                    time.sleep(5)
                    continue
                for update in data.get("result", []):
                    # 메시지 읽자마자 오프셋 업데이트 및 저장 (무한 루프 방지 핵심)
                    self.offset = int(update.get("update_id", 0))
                    st = load_json(self.state_file, {})
                    st["last_offset"] = self.offset
                    save_json(self.state_file, st)
                    
                    msg = update.get("message") or {}
                    text = msg.get("text") or ""
                    chat_id = str((msg.get("chat") or {}).get("id") or "")
                    if not chat_id:
                        continue
                    if not self.allowed(chat_id):
                        continue
                    self.handle(chat_id, text)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"Telegram bot error: {exc}", file=sys.stderr, flush=True)
                time.sleep(5)


def main() -> int:
    load_dotenv(Path(".env"))
    load_dotenv(Path.home() / ".light_loggg.env")
    state_file = Path(os.getenv("LIGHT_LOGGG_STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser()
    TelegramBot(state_file).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
