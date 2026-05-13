#!/usr/bin/env python3
"""
Tesla Fleet Telemetry handler for LIGHT LOGGG.

주의:
- Telegram bot token / chat_id는 절대 코드에 직접 넣지 않는다.
- ~/.light_loggg.env 또는 실행 환경변수에서 읽는다.
- 이 파일은 telemetry_server.py에서 import되어 process_data(data)를 호출받는 구조다.
"""

from __future__ import annotations

import os
import sys
import json
import time
import signal
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests


# =========================
# Runtime configuration
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

REQUEST_TIMEOUT = int(os.getenv("LIGHT_LOGGG_REQUEST_TIMEOUT", "10"))

WINDOW_SIZE_MINUTES = float(os.getenv("LIGHT_LOGGG_WINDOW_MINUTES", "3"))
THRESHOLD_EFFICIENCY = float(os.getenv("LIGHT_LOGGG_THRESHOLD_KM_PER_KWH", "4.5"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS", "60"))

WORK_DIR = os.path.expanduser("~/tesla_telemetry_work")
UPDATE_TRIGGER_FILE = os.path.join(WORK_DIR, "update_trigger")


# =========================
# Runtime state
# =========================

data_window: deque[Dict[str, Any]] = deque()
last_alert_time = 0.0

daily_stats: Dict[str, Any] = {
    "total_distance": 0.0,
    "efficiencies": [],
    "drive_sessions": [],
    "date": datetime.now().date().isoformat(),
}

command_thread_started = False


# =========================
# Helpers
# =========================

def env_ready() -> bool:
    return bool(TELEGRAM_TOKEN and ADMIN_CHAT_ID)


def send_message(text: str) -> bool:
    """Send Telegram message. Returns True if Telegram accepted the request."""
    if not env_ready():
        print(
            "[telegram disabled] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is missing. "
            f"message={text}",
            file=sys.stderr,
            flush=True,
        )
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
    }

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code >= 400:
            print(
                f"Telegram sendMessage failed: HTTP {response.status_code} "
                f"{response.text[:300]}",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True
    except requests.RequestException as exc:
        print(f"Telegram sendMessage request failed: {exc}", file=sys.stderr, flush=True)
        return False


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def calculate_efficiency(points: List[Dict[str, Any]]) -> Optional[float]:
    """
    Calculate rough km/kWh from recent telemetry points.

    Assumption:
    - speed is mph, same as Tesla API drive_state speed convention.
    - power is watts or similar telemetry field depending on telemetry config.
      If the incoming telemetry sends kW instead, this calculation must be adjusted.
    """
    if not points:
        return None

    total_distance_km = 0.0
    total_energy_kwh = 0.0

    for point in points:
        speed_mph = as_float(point.get("speed"), 0.0)
        power_raw = as_float(point.get("power"), 0.0)

        speed_kmh = speed_mph * 1.60934
        power_w = abs(power_raw)

        if speed_kmh > 0 and power_w > 0:
            # This assumes one sample per second.
            total_distance_km += speed_kmh / 3600.0
            total_energy_kwh += power_w / 1000.0 / 3600.0

    if total_distance_km <= 0 or total_energy_kwh <= 0:
        return None

    return round(total_distance_km / total_energy_kwh, 2)


def reset_daily_if_needed() -> None:
    today = datetime.now().date().isoformat()
    if daily_stats.get("date") == today:
        return

    daily_stats.clear()
    daily_stats.update(
        {
            "total_distance": 0.0,
            "efficiencies": [],
            "drive_sessions": [],
            "date": today,
        }
    )


# =========================
# Telegram command listener
# =========================

def handle_command(text: str) -> None:
    command = (text or "").strip().split()[0].lower() if text else ""

    if command == "/status":
        effs = daily_stats.get("efficiencies") or []
        avg_eff = sum(effs) / len(effs) if effs else 0.0
        send_message(
            "두삼이 telemetry handler 상태\n"
            f"- 상태: 실행 중\n"
            f"- 오늘 거리: {daily_stats.get('total_distance', 0.0):.2f} km\n"
            f"- 오늘 평균 전비: {avg_eff:.2f} km/kWh\n"
            f"- 최근 윈도우 샘플: {len(data_window)}개\n"
            f"- 기준 전비: {THRESHOLD_EFFICIENCY:.2f} km/kWh"
        )

    elif command == "/daily":
        effs = daily_stats.get("efficiencies") or []
        avg_eff = sum(effs) / len(effs) if effs else 0.0
        send_message(
            "오늘의 telemetry 주행 요약\n"
            f"- 날짜: {daily_stats.get('date')}\n"
            f"- 거리: {daily_stats.get('total_distance', 0.0):.2f} km\n"
            f"- 평균 전비: {avg_eff:.2f} km/kWh\n"
            f"- 전비 샘플: {len(effs)}개"
        )

    elif command == "/update":
        os.makedirs(WORK_DIR, exist_ok=True)
        with open(UPDATE_TRIGGER_FILE, "w", encoding="utf-8") as file:
            file.write("1\n")

        send_message("업데이트 트리거 파일 생성 완료. telemetry supervisor 재시작을 요청합니다.")

        try:
            os.kill(os.getppid(), signal.SIGTERM)
        except Exception as exc:
            send_message(f"상위 프로세스 종료 요청 실패: {exc}")

    elif command:
        send_message("알 수 없는 명령어입니다. 사용 가능: /status, /daily, /update")


def check_commands() -> None:
    """Telegram getUpdates loop."""
    if not env_ready():
        print(
            "Telegram command listener disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing.",
            file=sys.stderr,
            flush=True,
        )
        return

    last_update_id = 0
    print("Telegram listener active.", flush=True)

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            response = requests.get(
                url,
                params={
                    "offset": last_update_id + 1,
                    "timeout": 15,
                },
                timeout=25,
            )
            data = response.json()

            if not data.get("ok"):
                print(f"Telegram getUpdates returned not ok: {data}", file=sys.stderr, flush=True)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                last_update_id = int(update.get("update_id", last_update_id))

                message = update.get("message") or {}
                text = message.get("text") or ""
                chat_id = str((message.get("chat") or {}).get("id") or "")

                if chat_id != str(ADMIN_CHAT_ID):
                    continue

                handle_command(text)

        except Exception as exc:
            print(f"Telegram command listener error: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)


def start_command_thread_once() -> None:
    global command_thread_started

    if command_thread_started:
        return

    thread = threading.Thread(target=check_commands, daemon=True)
    thread.start()
    command_thread_started = True


# =========================
# Telemetry processing
# =========================

def process_data(data: Dict[str, Any]) -> None:
    """
    Called by telemetry_server.py whenever Tesla telemetry data arrives.
    """
    global last_alert_time

    reset_daily_if_needed()

    current_time = datetime.now()
    data["ts"] = current_time
    data_window.append(data)

    cutoff = current_time - timedelta(minutes=WINDOW_SIZE_MINUTES)
    while data_window and data_window[0].get("ts") < cutoff:
        data_window.popleft()

    speed_mph = as_float(data.get("speed"), 0.0)
    speed_kmh = speed_mph * 1.60934

    if speed_kmh <= 0:
        return

    efficiency = calculate_efficiency(list(data_window))

    # Distance accumulation assumes one telemetry sample per second.
    daily_stats["total_distance"] += speed_kmh / 3600.0

    if efficiency is not None:
        daily_stats["efficiencies"].append(efficiency)

        now = time.time()
        if efficiency < THRESHOLD_EFFICIENCY and now - last_alert_time > ALERT_COOLDOWN_SECONDS:
            send_message(
                "전비 경고\n"
                f"- 최근 {WINDOW_SIZE_MINUTES:.0f}분 평균: {efficiency:.2f} km/kWh\n"
                f"- 기준: {THRESHOLD_EFFICIENCY:.2f} km/kWh"
            )
            last_alert_time = now


# =========================
# Module init
# =========================

start_command_thread_once()
send_message("두삼이 telemetry handler 로드 완료")
print("Tesla telemetry handler loaded.", flush=True)
