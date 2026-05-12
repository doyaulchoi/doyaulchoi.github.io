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

def save_pid():
    DEFAULT_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_PID_FILE.write_text(str(os.getpid()))

# ... (기존 클래스 및 함수들 유지하되 아래 update_last_poll 부분만 수정됨) ...

    def update_last_poll(self, status, vehicle, interval):
        payload = {"time": now_kst().isoformat(), "status": status, "next_seconds": interval}
        if vehicle:
            cs = vehicle.get("charge_state", {})
            payload.update({
                "charging_state": cs.get("charging_state"),
                "battery_level": cs.get("battery_level"),
                "charger_power": cs.get("charger_power"), # 충전 속도 추가
                "charge_energy_added": cs.get("charge_energy_added"), # 충전량 추가
                "vehicle_name": vehicle.get("display_name", "두삼이")
            })
        
        # 상태 파일 저장
        state = {}
        if DEFAULT_STATE_FILE.exists():
            try: state = json.loads(DEFAULT_STATE_FILE.read_text())
            except: pass
        state["last_poll"] = payload
        DEFAULT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def run_forever(self):
        save_pid() # 시작할 때 PID 저장
        self.telegram.send("LIGHT LOGGG Tesla 폴링 시작")
        while True:
            # ... (기존 폴링 로직) ...
            time.sleep(60)

if __name__ == "__main__":
    # ... (기존 실행 로직) ...
    pass
