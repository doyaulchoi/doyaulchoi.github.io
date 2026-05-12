#!/usr/bin/env python3
import json
import os
import sys
import time
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

KST = timezone(timedelta(hours=9))
DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
DEFAULT_PID_FILE = Path.home() / "light_loggg_tesla" / "polling.pid"

def now_kst(): return datetime.now(KST)

def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists(): return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("\"").strip("\'")

class TelegramClient:
    def __init__(self):
        load_dotenv(Path.home() / ".light_loggg.env")
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

    def send(self, text: str):
        if not self.token or not self.chat_id: return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try: requests.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=10 )
        except: pass

class LightLogggPoller:
    def __init__(self):
        self.telegram = TelegramClient()
        self.state_file = DEFAULT_STATE_FILE
        self.charging_notification_stage = "idle"
        self.charging_start_timestamp = None
        self.last_charging_state = None

    def save_pid(self):
        DEFAULT_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_PID_FILE.write_text(str(os.getpid()))

    def handle_charging_notifications(self, cs):
        """충전 시작 시 즉시 알림 -> 3분 뒤 상세 알림"""
        c_state = cs.get("charging_state")
        battery = cs.get("battery_level")
        pwr = cs.get("charger_power")
        eta = cs.get("time_to_full_charge")

        if c_state == "Charging":
            if self.charging_notification_stage == "idle":
                self.telegram.send(f"⚡️ 충전 시작! 현재 배터리: {battery:.0f}%")
                self.charging_notification_stage = "initial_notified"
                self.charging_start_timestamp = datetime.now()
            elif self.charging_notification_stage == "initial_notified":
                elapsed = (datetime.now() - self.charging_start_timestamp).total_seconds()
                if elapsed >= 180: # 3분 경과
                    eta_text = f"{int(eta * 60)}분" if eta else "알 수 없음"
                    self.telegram.send(f"⚡️ 충전 중... (3분 경과)\n현재 배터리: {battery:.0f}%\n충전 속도: {pwr:.1f} kW\n완료 예상 시간: {eta_text}")
                    self.charging_notification_stage = "detailed_notified"
        else:
            if self.charging_notification_stage != "idle":
                self.telegram.send(f"✅ 충전 중단/완료! 최종 배터리: {battery:.0f}%")
            self.charging_notification_stage = "idle"

    def update_last_poll(self, vehicle_data):
        cs = vehicle_data.get("charge_state", {})
        self.handle_charging_notifications(cs)
        
        payload = {
            "time": now_kst().isoformat(),
            "status": vehicle_data.get("state", "online"),
            "charging_state": cs.get("charging_state"),
            "battery_level": cs.get("battery_level"),
            "charger_power": cs.get("charger_power"),
            "charge_energy_added": cs.get("charge_energy_added"),
            "vehicle_name": vehicle_data.get("display_name", "두삼이")
        }
        
        state = {}
        if self.state_file.exists():
            try: state = json.loads(self.state_file.read_text())
            except: pass
        state["last_poll"] = payload
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def run_forever(self):
        self.save_pid()
        self.telegram.send("LIGHT LOGGG Tesla 폴링 시작")
        while True:
            # (여기에 실제 Tesla API 호출 및 update_last_poll 호출 로직이 들어갑니다)
            time.sleep(60)

if __name__ == "__main__":
    LightLogggPoller().run_forever()
