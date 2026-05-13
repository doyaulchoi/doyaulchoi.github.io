#!/usr/bin/env python3
"""
LIGHT LOGGG Tesla Fleet API polling handler.

목표:
- Termux에서 가볍게 Tesla Fleet API polling
- 차량이 asleep/offline이면 vehicle_data 호출하지 않음
- 주행/충전/온라인/수면 상태별 polling 주기 분리
- Telegram 상태 알림/요약용 state 저장
- 세컨폰/Telegram 명령으로 polling 즉시 깨우기 지원
- 충전 시작 시 즉시 알림 및 3분 후 상세 알림
- 매일 아침 6시 30분 모닝 알림 (배터리 상태, 충전량, 평균 충전 속도)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests


# =========================
# Paths / defaults
# =========================

KST = timezone(timedelta(hours=9))

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"
DATA_DIR = APP_DIR / "data"
TRIPS_CSV_FILE = DATA_DIR / "trips.csv"

DEFAULT_PUBLIC_CONFIG_FILE = APP_DIR / "light_loggg_public_config.json"
DEFAULT_TOKEN_FILE = Path.home() / ".light_loggg_tesla_tokens.json"
DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
DEFAULT_COMMAND_FILE = APP_DIR / "command.json"

DEFAULT_API_BASE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
DEFAULT_CLIENT_ID = "d1351a7e-42fd-4318-b6a2-c9d702af75c1"

AUTH_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"

VEHICLE_DATA_ENDPOINT_LIST = [
    "charge_state",
    "climate_state",
    "closures_state",
    "drive_state",
    "gui_settings",
    "vehicle_config",
    "vehicle_state",
    "vehicle_data_combo",
]

VEHICLE_DATA_ENDPOINTS = ";".join(VEHICLE_DATA_ENDPOINT_LIST)

VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION = ";".join(
    endpoint for endpoint in VEHICLE_DATA_ENDPOINT_LIST if endpoint != "location_data"
)

DEFAULT_TESLA_SCOPE = "openid offline_access user_data vehicle_device_data"

DEFAULT_PUBLIC_CONFIG: Dict[str, Any] = {
    "polling": {
        "asleep_seconds": 1800,
        "online_seconds": 300,
        "driving_seconds": 10,
        "charging_seconds": 60,
        "error_seconds": 300,
    },
    "alerts": {
        "threshold_km_per_kwh": 4.5,
        "window_minutes": 3,
        "alert_cooldown_seconds": 60,
    },
    "external_commands": {
        "drive_boost_seconds": 180,
    },
    "request": {
        "timeout_seconds": 25,
    },
    "morning_alert": {
        "hour": 6,
        "minute": 30,
    },
    "daily_report": {
        "enabled": True,
        "hour": 19,
        "minute": 0,
        "weekly_enabled": True,
        "weekly_day": 4,
    },
}


# =========================
# Config helpers
# =========================

def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small env file without python-dotenv."""
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


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def load_public_config(path: Path) -> Dict[str, Any]:
    config = dict(DEFAULT_PUBLIC_CONFIG)

    if not path.exists():
        return config

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(loaded, dict):
            print(f"public config ignored: top-level is not object: {path}", file=sys.stderr, flush=True)
            return config

        return deep_merge(config, loaded)

    except Exception as exc:
        print(f"public config load failed: {exc}", file=sys.stderr, flush=True)
        return config


def cfg_int(config: Dict[str, Any], section: str, key: str, env_key: str, default: int) -> int:
    if env_key in os.environ:
        try:
            return int(os.environ[env_key])
        except Exception:
            print(f"invalid env int {env_key}={os.environ.get(env_key)}; using config/default", file=sys.stderr, flush=True)

    try:
        return int((config.get(section) or {}).get(key, default))
    except Exception:
        return default


def cfg_float(config: Dict[str, Any], section: str, key: str, env_key: str, default: float) -> float:
    if env_key in os.environ:
        try:
            return float(os.environ[env_key])
        except Exception:
            print(f"invalid env float {env_key}={os.environ.get(env_key)}; using config/default", file=sys.stderr, flush=True)

    try:
        return float((config.get(section) or {}).get(key, default))
    except Exception:
        return default


def cfg_bool(config: Dict[str, Any], section: str, key: str, env_key: str, default: bool) -> bool:
    if env_key in os.environ:
        value = os.environ.get(env_key, "").strip().lower()
        return value in {"1", "true", "yes", "y", "on"}

    try:
        value = (config.get(section) or {}).get(key, default)

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}

        return bool(value)

    except Exception:
        return default


# 전역 설정값
PUBLIC_CONFIG: Dict[str, Any] = dict(DEFAULT_PUBLIC_CONFIG)

POLL_ASLEEP_SECONDS = 1800
POLL_ONLINE_SECONDS = 300
POLL_DRIVING_SECONDS = 10
POLL_CHARGING_SECONDS = 60
POLL_ERROR_SECONDS = 300

WINDOW_SIZE_MINUTES = 3.0
THRESHOLD_EFFICIENCY = 4.5
LOW_EFFICIENCY_ALERT_COOLDOWN = 60

REQUEST_TIMEOUT = 25
EXTERNAL_DRIVE_BOOST_SECONDS = 180

MORNING_ALERT_HOUR = 6
MORNING_ALERT_MINUTE = 30
DAILY_REPORT_ENABLED = True
DAILY_REPORT_HOUR = 19
DAILY_REPORT_MINUTE = 0
WEEKLY_REPORT_ENABLED = True
WEEKLY_REPORT_HOUR = 4
WEEKLY_REPORT_DAY = 4


def init_runtime_config(config_file: Path) -> None:
    global PUBLIC_CONFIG
    global POLL_ASLEEP_SECONDS, POLL_ONLINE_SECONDS, POLL_DRIVING_SECONDS
    global POLL_CHARGING_SECONDS, POLL_ERROR_SECONDS
    global WINDOW_SIZE_MINUTES, THRESHOLD_EFFICIENCY, LOW_EFFICIENCY_ALERT_COOLDOWN
    global REQUEST_TIMEOUT, EXTERNAL_DRIVE_BOOST_SECONDS
    global MORNING_ALERT_HOUR, MORNING_ALERT_MINUTE
    global DAILY_REPORT_ENABLED, DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE
    global WEEKLY_REPORT_ENABLED, WEEKLY_REPORT_HOUR, WEEKLY_REPORT_DAY

    PUBLIC_CONFIG = load_public_config(config_file)

    POLL_ASLEEP_SECONDS = cfg_int(PUBLIC_CONFIG, "polling", "asleep_seconds", "LIGHT_LOGGG_POLL_ASLEEP_SECONDS", 1800)
    POLL_ONLINE_SECONDS = cfg_int(PUBLIC_CONFIG, "polling", "online_seconds", "LIGHT_LOGGG_POLL_ONLINE_SECONDS", 300)
    POLL_DRIVING_SECONDS = cfg_int(PUBLIC_CONFIG, "polling", "driving_seconds", "LIGHT_LOGGG_POLL_DRIVING_SECONDS", 10)
    POLL_CHARGING_SECONDS = cfg_int(PUBLIC_CONFIG, "polling", "charging_seconds", "LIGHT_LOGGG_POLL_CHARGING_SECONDS", 60)
    POLL_ERROR_SECONDS = cfg_int(PUBLIC_CONFIG, "polling", "error_seconds", "LIGHT_LOGGG_POLL_ERROR_SECONDS", 300)

    WINDOW_SIZE_MINUTES = cfg_float(PUBLIC_CONFIG, "alerts", "window_minutes", "LIGHT_LOGGG_WINDOW_MINUTES", 3.0)
    THRESHOLD_EFFICIENCY = cfg_float(PUBLIC_CONFIG, "alerts", "threshold_km_per_kwh", "LIGHT_LOGGG_THRESHOLD_KM_PER_KWH", 4.5)
    LOW_EFFICIENCY_ALERT_COOLDOWN = cfg_int(PUBLIC_CONFIG, "alerts", "alert_cooldown_seconds", "LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS", 60)

    REQUEST_TIMEOUT = cfg_int(PUBLIC_CONFIG, "request", "timeout_seconds", "LIGHT_LOGGG_REQUEST_TIMEOUT", 25)
    EXTERNAL_DRIVE_BOOST_SECONDS = cfg_int(PUBLIC_CONFIG, "external_commands", "drive_boost_seconds", "LIGHT_LOGGG_EXTERNAL_DRIVE_BOOST_SECONDS", 180)

    MORNING_ALERT_HOUR = cfg_int(PUBLIC_CONFIG, "morning_alert", "hour", "LIGHT_LOGGG_MORNING_ALERT_HOUR", 6)
    MORNING_ALERT_MINUTE = cfg_int(PUBLIC_CONFIG, "morning_alert", "minute", "LIGHT_LOGGG_MORNING_ALERT_MINUTE", 30)

    DAILY_REPORT_ENABLED = cfg_bool(PUBLIC_CONFIG, "daily_report", "enabled", "LIGHT_LOGGG_DAILY_REPORT_ENABLED", True)
    DAILY_REPORT_HOUR = cfg_int(PUBLIC_CONFIG, "daily_report", "hour", "LIGHT_LOGGG_DAILY_REPORT_HOUR", 19)
    DAILY_REPORT_MINUTE = cfg_int(PUBLIC_CONFIG, "daily_report", "minute", "LIGHT_LOGGG_DAILY_REPORT_MINUTE", 0)
    WEEKLY_REPORT_ENABLED = cfg_bool(PUBLIC_CONFIG, "daily_report", "weekly_enabled", "LIGHT_LOGGG_WEEKLY_REPORT_ENABLED", True)
    WEEKLY_REPORT_DAY = cfg_int(PUBLIC_CONFIG, "daily_report", "weekly_day", "LIGHT_LOGGG_WEEKLY_REPORT_DAY", 4)


# =========================
# Utility functions
# =========================

def now_kst() -> datetime:
    return datetime.now(KST)


def parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return dict(default)
    except Exception:
        return dict(default)


def haversine_km(lat1: Optional[float], lon1: Optional[float], lat2: Optional[float], lon2: Optional[float]) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0088
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_telegram_text(text):
        payload = {"chat_id": chat_id, "text": chunk}
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                print(f"Telegram sendMessage HTTP {response.status_code}: {response.text[:500]}", file=sys.stderr, flush=True)
        except requests.RequestException as exc:
            print(f"Telegram sendMessage failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(0.2)


def split_telegram_text(text: str, limit: int = 3500) -> List[str]:
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


# =========================
# Tesla API helpers
# =========================

@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: str


@dataclass
class TeslaClient:
    client_id: str
    api_base: str
    auth_token_url: str
    scope: str
    token_file: Path
    tokens: Optional[AuthTokens] = None

    def __post_init__(self) -> None:
        self.load_tokens()

    def load_tokens(self) -> None:
        if not self.token_file.exists():
            return
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            self.tokens = AuthTokens(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=datetime.fromtimestamp(data["expires_at"], tz=timezone.utc),
                token_type=data["token_type"],
            )
        except Exception as exc:
            print(f"Failed to load tokens: {exc}", file=sys.stderr, flush=True)
            self.tokens = None

    def save_tokens(self) -> None:
        if not self.tokens: return
        payload = {
            "access_token": self.tokens.access_token,
            "refresh_token": self.tokens.refresh_token,
            "expires_at": int(self.tokens.expires_at.timestamp()),
            "token_type": self.tokens.token_type,
        }
        atomic_write_json(self.token_file, payload)

    def refresh_tokens(self) -> None:
        if not self.tokens or not self.tokens.refresh_token:
            raise RuntimeError("Refresh token not available.")

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": self.tokens.refresh_token,
            "scope": self.scope,
        }
        try:
            response = requests.post(self.auth_token_url, headers=headers, data=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            self.tokens = AuthTokens(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"]),
                token_type=data["token_type"],
            )
            self.save_tokens()
        except Exception as exc:
            print(f"Failed to refresh tokens: {exc}", file=sys.stderr, flush=True)
            raise

    def ensure_tokens_valid(self) -> None:
        if not self.tokens or self.tokens.expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
            self.refresh_tokens()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.ensure_tokens_valid()
        headers = {"Authorization": f"{self.tokens.token_type} {self.tokens.access_token}"}
        url = f"{self.api_base}{path}"
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()


# =========================
# State management
# =========================

@dataclass
class Sample:
    time: datetime
    odometer: Optional[float] = None
    battery_level: Optional[int] = None
    charging_state: Optional[str] = None
    charge_energy_added: Optional[float] = None
    charger_power: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[int] = None
    shift_state: Optional[str] = None


@dataclass
class State:
    tesla_client: TeslaClient
    telegram_token: str
    telegram_chat_id: str
    public_config: Dict[str, Any]
    state_file: Path
    last_poll: Optional[Sample] = None
    last_charge_alert: Optional[datetime] = None
    last_morning_alert: Optional[datetime] = None
    last_daily_report: Optional[datetime] = None
    last_weekly_report: Optional[datetime] = None
    last_driving_end: Optional[datetime] = None
    charging_start_time: Optional[datetime] = None
    charging_start_battery: Optional[int] = None
    charging_start_energy_added: Optional[float] = None
    charging_samples: Deque[Tuple[datetime, float, int]] = field(default_factory=lambda: deque(maxlen=10))
    external_drive_boost_until: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.load_state()

    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.last_poll = Sample(time=parse_dt(data["last_poll"]["time"]), **{k: v for k, v in data["last_poll"].items() if k != "time"}) if data.get("last_poll") else None
            self.last_charge_alert = parse_dt(data.get("last_charge_alert"))
            self.last_morning_alert = parse_dt(data.get("last_morning_alert"))
            self.last_daily_report = parse_dt(data.get("last_daily_report"))
            self.last_weekly_report = parse_dt(data.get("last_weekly_report"))
            self.last_driving_end = parse_dt(data.get("last_driving_end"))
            self.charging_start_time = parse_dt(data.get("charging_start_time"))
            self.charging_start_battery = data.get("charging_start_battery")
            self.charging_start_energy_added = data.get("charging_start_energy_added")
            self.external_drive_boost_until = parse_dt(data.get("external_drive_boost_until"))
        except Exception as exc:
            print(f"Failed to load state: {exc}", file=sys.stderr, flush=True)

    def save_state(self) -> None:
        payload = {
            "last_poll": self.last_poll.__dict__ if self.last_poll else None,
            "last_charge_alert": self.last_charge_alert.isoformat() if self.last_charge_alert else None,
            "last_morning_alert": self.last_morning_alert.isoformat() if self.last_morning_alert else None,
            "last_daily_report": self.last_daily_report.isoformat() if self.last_daily_report else None,
            "last_weekly_report": self.last_weekly_report.isoformat() if self.last_weekly_report else None,
            "last_driving_end": self.last_driving_end.isoformat() if self.last_driving_end else None,
            "charging_start_time": self.charging_start_time.isoformat() if self.charging_start_time else None,
            "charging_start_battery": self.charging_start_battery,
            "charging_start_energy_added": self.charging_start_energy_added,
            "external_drive_boost_until": self.external_drive_boost_until.isoformat() if self.external_drive_boost_until else None,
        }
        atomic_write_json(self.state_file, payload)

    def send_telegram(self, text: str) -> None:
        send_telegram_message(self.telegram_token, self.telegram_chat_id, text)


# =========================
# Polling logic
# =========================

def get_vehicle_data(tesla_client: TeslaClient, vehicle_id: str) -> Dict[str, Any]:
    return tesla_client.get(f"/api/1/vehicles/{vehicle_id}/vehicle_data", params={
        "endpoints": VEHICLE_DATA_ENDPOINTS
    })


def get_vehicle_data_without_location(tesla_client: TeslaClient, vehicle_id: str) -> Dict[str, Any]:
    return tesla_client.get(f"/api/1/vehicles/{vehicle_id}/vehicle_data", params={
        "endpoints": VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION
    })


def handle_command(state: State) -> None:
    if not DEFAULT_COMMAND_FILE.exists():
        return

    try:
        command = load_json(DEFAULT_COMMAND_FILE, {})
        cmd_type = command.get("command")
        source = command.get("source", "unknown")

        if cmd_type == "poll_now":
            state.send_telegram(f"[{source}] 즉시 폴링 명령 수신. 차량 상태 확인 중...")
            # Force online polling for a short period
            state.external_drive_boost_until = now_kst() + timedelta(seconds=POLL_ONLINE_SECONDS * 2)
            state.save_state()

        elif cmd_type == "driving_start":
            seconds = command.get("seconds", EXTERNAL_DRIVE_BOOST_SECONDS)
            state.external_drive_boost_until = now_kst() + timedelta(seconds=seconds)
            state.send_telegram(f"[{source}] 주행 시작 boost 명령 수신. {seconds}초간 폴링 부스트.")
            state.save_state()

        elif cmd_type == "driving_stop":
            state.external_drive_boost_until = None
            state.send_telegram(f"[{source}] 주행 boost 해제 명령 수신.")
            state.save_state()

    except Exception as exc:
        print(f"Failed to handle command: {exc}", file=sys.stderr, flush=True)
    finally:
        DEFAULT_COMMAND_FILE.unlink(missing_ok=True)


def handle_charging_alert(state: State, current_sample: Sample) -> None:
    if current_sample.charging_state == "Charging" and state.charging_start_time is None:
        state.charging_start_time = current_sample.time
        state.charging_start_battery = current_sample.battery_level
        state.charging_start_energy_added = current_sample.charge_energy_added
        state.send_telegram(f"⚡️ 충전 시작! 현재 배터리: {current_sample.battery_level}%")
        state.save_state()

    elif current_sample.charging_state != "Charging" and state.charging_start_time is not None:
        charge_duration = now_kst() - state.charging_start_time
        charge_amount = (current_sample.battery_level or 0) - (state.charging_start_battery or 0)
        energy_added = (current_sample.charge_energy_added or 0) - (state.charging_start_energy_added or 0)
        
        message = f"🔌 충전 종료!\n"
        message += f"- 충전 시간: {charge_duration.total_seconds() / 60:.1f}분\n"
        message += f"- 충전량: {charge_amount}% ({state.charging_start_battery}% -> {current_sample.battery_level}%)\n"
        if energy_added > 0:
            message += f"- 추가된 에너지: {energy_added:.2f} kWh\n"
        state.send_telegram(message)
        state.charging_start_time = None
        state.charging_start_battery = None
        state.charging_start_energy_added = None
        state.save_state()

    if current_sample.charging_state == "Charging":
        state.charging_samples.append((current_sample.time, current_sample.charge_energy_added or 0, current_sample.charger_power or 0))

        if len(state.charging_samples) >= 2 and (now_kst() - state.charging_samples[0][0]).total_seconds() >= WINDOW_SIZE_MINUTES * 60:
            total_energy_added = state.charging_samples[-1][1] - state.charging_samples[0][1]
            avg_power = sum(s[2] for s in state.charging_samples) / len(state.charging_samples)
            
            if total_energy_added > 0 and state.last_charge_alert is None or (now_kst() - state.last_charge_alert).total_seconds() > LOW_EFFICIENCY_ALERT_COOLDOWN:
                message = f"⚡️ 충전 상세 정보 (지난 {WINDOW_SIZE_MINUTES:.0f}분)\n"
                message += f"- 평균 충전 전력: {avg_power:.0f} W\n"
                message += f"- 추가된 에너지: {total_energy_added:.2f} kWh\n"
                state.send_telegram(message)
                state.last_charge_alert = now_kst()
                state.save_state()

def handle_morning_alert(state: State) -> None:
    now = now_kst()
    if state.last_morning_alert and state.last_morning_alert.date() == now.date():
        return # Already sent today

    if now.hour == MORNING_ALERT_HOUR and now.minute >= MORNING_ALERT_MINUTE:
        if state.last_poll is None:
            state.send_telegram("☀️ 모닝 알림: 차량 상태를 확인할 수 없습니다. 폴링 스크립트가 정상 작동 중인지 확인해 주세요.")
            state.last_morning_alert = now
            state.save_state()
            return

        message = "☀️ 모닝 알림\n"
        
        # 전날 주행 종료 시점 배터리 레벨과 비교
        if state.last_driving_end and state.last_driving_end.date() == (now - timedelta(days=1)).date():
            # 전날 마지막 주행 종료 시점의 배터리 레벨을 가져와야 함. 현재 state에는 없음.
            # 이 부분은 추후 구현 필요 (state에 daily_summary_data 같은 필드 추가)
            # 임시로 last_poll의 배터리 레벨만 사용
            pass

        if state.last_poll.charging_state == "Charging":
            message += f"- 현재 충전 중: {state.last_poll.battery_level}%\n"
            # 충전 시작 정보가 있다면 추가
            if state.charging_start_time and state.charging_start_battery:
                charge_duration = now - state.charging_start_time
                charge_amount = (state.last_poll.battery_level or 0) - (state.charging_start_battery or 0)
                message += f"  (시작: {state.charging_start_battery}% @ {state.charging_start_time.strftime("%H:%M")}, 충전량: {charge_amount}%)\n"
        elif state.last_poll.battery_level is not None:
            message += f"- 현재 배터리: {state.last_poll.battery_level}%\n"

        # 전날 주행 완료 시점과 비교하여 배터리 레벨이 늘었으면 충전량 및 평균 충전 속도
        # 이 로직은 last_driving_end 시점의 배터리 레벨이 state에 저장되어 있어야 정확히 구현 가능
        # 현재는 last_poll의 배터리 레벨만으로 판단
        if state.last_poll.charging_state == "Charging" and state.charging_start_time:
            # 현재 충전 중이므로 충전량과 평균 충전 속도 계산
            if len(state.charging_samples) >= 2:
                total_energy_added = state.charging_samples[-1][1] - state.charging_samples[0][1]
                avg_power = sum(s[2] for s in state.charging_samples) / len(state.charging_samples)
                message += f"- 현재 충전 중 (평균 전력: {avg_power:.0f} W, 추가 에너지: {total_energy_added:.2f} kWh)\n"

        state.send_telegram(message)
        state.last_morning_alert = now
        state.save_state()

def handle_daily_report(state: State) -> None:
    now = now_kst()
    if not DAILY_REPORT_ENABLED or (state.last_daily_report and state.last_daily_report.date() == now.date()):
        return

    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE:
        # TODO: Implement actual daily report logic based on stored trip data
        state.send_telegram(f"🗓️ 일일 보고서 (구현 예정): {now.strftime("%Y-%m-%d")}")
        state.last_daily_report = now
        state.save_state()

def handle_weekly_report(state: State) -> None:
    now = now_kst()
    if not WEEKLY_REPORT_ENABLED or (state.last_weekly_report and state.last_weekly_report.isocalendar()[:2] == now.isocalendar()[:2]):
        return

    if now.isoweekday() == WEEKLY_REPORT_DAY and now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE:
        # TODO: Implement actual weekly report logic based on stored trip data
        state.send_telegram(f"🗓️ 주간 보고서 (구현 예정): {now.strftime("%Y-%m-%d")}")
        state.last_weekly_report = now
        state.save_state()


def poll_vehicle(state: State, vehicle_id: str) -> None:
    try:
        # Determine if location data is needed based on shift_state
        if state.last_poll and state.last_poll.shift_state == "D": # Driving
            vehicle_data = get_vehicle_data(state.tesla_client, vehicle_id)
        else:
            vehicle_data = get_vehicle_data_without_location(state.tesla_client, vehicle_id)

        charge_state = vehicle_data["charge_state"]
        drive_state = vehicle_data["drive_state"]
        vehicle_state = vehicle_data["vehicle_state"]

        current_sample = Sample(
            time=now_kst(),
            odometer=vehicle_state.get("odometer"),
            battery_level=charge_state.get("battery_level"),
            charging_state=charge_state.get("charging_state"),
            charge_energy_added=charge_state.get("charge_energy_added"),
            charger_power=charge_state.get("charger_power"),
            latitude=drive_state.get("latitude"),
            longitude=drive_state.get("longitude"),
            speed=drive_state.get("speed"),
            shift_state=drive_state.get("shift_state"),
        )

        # Update last_driving_end if vehicle was driving and now is not
        if state.last_poll and state.last_poll.shift_state == "D" and current_sample.shift_state != "D":
            state.last_driving_end = state.last_poll.time

        state.last_poll = current_sample
        state.save_state()

        handle_charging_alert(state, current_sample)

    except requests.exceptions.HTTPError as http_err:
        if http_err.response.status_code == 408: # Request Timeout from Tesla API
            print(f"Tesla API timeout (408). Vehicle likely asleep.", file=sys.stderr, flush=True)
        else:
            print(f"HTTP error occurred: {http_err}", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"Error polling vehicle: {exc}", file=sys.stderr, flush=True)


def main() -> int:
    load_dotenv(Path(".env"))
    load_dotenv(Path.home() / ".light_loggg.env")

    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    init_runtime_config(DEFAULT_PUBLIC_CONFIG_FILE)

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    tesla_vin = os.getenv("TESLA_VIN")
    tesla_client_id = os.getenv("TESLA_CLIENT_ID", DEFAULT_CLIENT_ID)
    tesla_api_base = os.getenv("TESLA_API_BASE", DEFAULT_API_BASE)
    tesla_scope = os.getenv("TESLA_SCOPE", DEFAULT_TESLA_SCOPE)

    if not all([telegram_token, telegram_chat_id, tesla_vin]):
        print("필수 환경 변수 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TESLA_VIN)가 설정되지 않았습니다.", file=sys.stderr, flush=True)
        return 1

    tesla_client = TeslaClient(
        client_id=tesla_client_id,
        api_base=tesla_api_base,
        auth_token_url=AUTH_TOKEN_URL,
        scope=tesla_scope,
        token_file=DEFAULT_TOKEN_FILE,
    )

    state = State(
        tesla_client=tesla_client,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        public_config=PUBLIC_CONFIG,
        state_file=DEFAULT_STATE_FILE,
    )

    print("LIGHT LOGGG Tesla polling started", flush=True)

    while True:
        handle_command(state)
        handle_morning_alert(state)
        handle_daily_report(state)
        handle_weekly_report(state)

        poll_vehicle(state, tesla_vin)

        current_status = state.last_poll.status if state.last_poll else "unknown"
        current_charging_state = state.last_poll.charging_state if state.last_poll else "Unknown"
        current_shift_state = state.last_poll.shift_state if state.last_poll else "P"

        sleep_seconds = POLL_ASLEEP_SECONDS

        if state.external_drive_boost_until and now_kst() < state.external_drive_boost_until:
            sleep_seconds = POLL_DRIVING_SECONDS # Force driving boost
        elif current_status == "online":
            if current_shift_state == "D":
                sleep_seconds = POLL_DRIVING_SECONDS
            elif current_charging_state == "Charging":
                sleep_seconds = POLL_CHARGING_SECONDS
            else:
                sleep_seconds = POLL_ONLINE_SECONDS
        elif current_status == "asleep":
            sleep_seconds = POLL_ASLEEP_SECONDS
        elif current_status == "offline":
            sleep_seconds = POLL_ASLEEP_SECONDS
        elif current_status == "error":
            sleep_seconds = POLL_ERROR_SECONDS

        time.sleep(sleep_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
