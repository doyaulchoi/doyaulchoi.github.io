#!/usr/bin/env python3
"""
LIGHT LOGGG Tesla Fleet API polling handler.

목표:
- Termux에서 가볍게 Tesla Fleet API polling
- 차량이 asleep/offline이면 vehicle_data 호출하지 않음
- 주행/충전/온라인/수면 상태별 polling 주기 분리
- Telegram 상태 알림/요약용 state 저장
- 세컨폰/Telegram 명령으로 polling 즉시 깨우기 지원
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
        value = value.strip().strip('"').strip("'")

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
WEEKLY_REPORT_DAY = 4


def init_runtime_config(config_file: Path) -> None:
    global PUBLIC_CONFIG
    global POLL_ASLEEP_SECONDS, POLL_ONLINE_SECONDS, POLL_DRIVING_SECONDS
    global POLL_CHARGING_SECONDS, POLL_ERROR_SECONDS
    global WINDOW_SIZE_MINUTES, THRESHOLD_EFFICIENCY, LOW_EFFICIENCY_ALERT_COOLDOWN
    global REQUEST_TIMEOUT, EXTERNAL_DRIVE_BOOST_SECONDS
    global MORNING_ALERT_HOUR, MORNING_ALERT_MINUTE
    global DAILY_REPORT_ENABLED, DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE
    global WEEKLY_REPORT_ENABLED, WEEKLY_REPORT_DAY

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
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def speed_mph_to_kmh(speed_mph: Optional[float]) -> Optional[float]:
    if speed_mph is None:
        return None
    return float(speed_mph) * 1.609344


def miles_to_km(miles: Optional[float]) -> Optional[float]:
    if miles is None:
        return None
    return float(miles) * 1.609344


def format_seconds_hm(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}시간 {m}분"
    return f"{m}분"


def format_duration_hours_minutes(hours: Optional[float]) -> str:
    if hours is None:
        return "확인 불가"
    h = int(hours)
    m = int((hours - h) * 60)
    if h > 0:
        return f"{h}시간 {m}분"
    return f"{m}분"


def format_eta_clock(hours: Optional[float]) -> str:
    if hours is None:
        return "확인 불가"
    eta = now_kst() + timedelta(hours=hours)
    return eta.strftime("%H:%M")


TRIP_CSV_FIELDS = [
    "date", "source", "start_time", "end_time", "distance_km", "duration_min",
    "avg_speed_kmh", "start_soc", "end_soc", "soc_used_pct", "start_odometer_km", "end_odometer_km"
]


def append_trip_to_csv(csv_file: Path, session: Dict[str, Any], source: str = "polling") -> None:
    start_time = parse_dt(session.get("start_time"))
    end_time = parse_dt(session.get("end_time"))
    if end_time:
        end_kst = end_time.astimezone(KST)
        date_text = end_kst.date().isoformat()
        end_time_text = end_kst.isoformat()
    else:
        date_text = now_kst().date().isoformat()
        end_time_text = ""
    if start_time:
        start_time_text = start_time.astimezone(KST).isoformat()
    else:
        start_time_text = ""
    distance_km = as_float(session.get("distance_km"))
    time_seconds = as_float(session.get("time_seconds"))
    avg_speed_kmh = as_float(session.get("avg_speed_kmh"))
    start_soc = as_float(session.get("start_soc"))
    end_soc = as_float(session.get("end_soc"))
    soc_used_pct = start_soc - end_soc if start_soc is not None and end_soc is not None else None
    start_odometer_km = as_float(session.get("start_odometer_km"))
    end_odometer_km = as_float(session.get("end_odometer_km"))
    row = {
        "date": date_text, "source": source, "start_time": start_time_text, "end_time": end_time_text,
        "distance_km": round(distance_km, 3) if distance_km is not None else "",
        "duration_min": round(time_seconds / 60, 1) if time_seconds is not None else "",
        "avg_speed_kmh": round(avg_speed_kmh, 1) if avg_speed_kmh is not None else "",
        "start_soc": round(start_soc, 1) if start_soc is not None else "",
        "end_soc": round(end_soc, 1) if end_soc is not None else "",
        "soc_used_pct": round(soc_used_pct, 1) if soc_used_pct is not None else "",
        "start_odometer_km": round(start_odometer_km, 3) if start_odometer_km is not None else "",
        "end_odometer_km": round(end_odometer_km, 3) if end_odometer_km is not None else "",
    }
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_file.exists() or csv_file.stat().st_size == 0
    with csv_file.open("a", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=TRIP_CSV_FIELDS)
        if write_header: writer.writeheader()
        writer.writerow(row)


# =========================
# Data classes
# =========================

@dataclass
class Sample:
    time: datetime
    speed_kmh: Optional[float]
    power_kw: Optional[float]
    odometer_km: Optional[float]
    battery_level: Optional[float]
    latitude: Optional[float]
    longitude: Optional[float]
    shift_state: Optional[str] = None
    charging_state: Optional[str] = None


@dataclass
class DriveSession:
    active: bool = False
    start_time: Optional[datetime] = None
    start_odometer_km: Optional[float] = None
    start_soc: Optional[float] = None
    last_sample_time: Optional[datetime] = None
    last_odometer_km: Optional[float] = None
    last_speed_kmh: Optional[float] = None
    distance_km: float = 0.0
    time_seconds: float = 0.0
    energy_kwh: float = 0.0
    speeds: List[float] = field(default_factory=list)
    efficiencies: List[float] = field(default_factory=list)

    def start(self, sample: Sample) -> None:
        self.active = True
        self.start_time = sample.time
        self.start_odometer_km = sample.odometer_km
        self.start_soc = sample.battery_level
        self.last_sample_time = sample.time
        self.last_odometer_km = sample.odometer_km
        self.last_speed_kmh = sample.speed_kmh
        self.distance_km = 0.0
        self.time_seconds = 0.0
        self.energy_kwh = 0.0
        self.speeds = []
        self.efficiencies = []

    def add_sample(self, sample: Sample) -> None:
        if not self.active: self.start(sample)
        if self.last_sample_time is not None:
            dt = max(0.0, (sample.time - self.last_sample_time).total_seconds())
        else:
            dt = 0.0
        if dt > 0:
            self.time_seconds += dt
            if sample.odometer_km is not None and self.last_odometer_km is not None:
                delta_odo = sample.odometer_km - self.last_odometer_km
                if 0 <= delta_odo < 5: self.distance_km += delta_odo
            elif sample.speed_kmh is not None:
                self.distance_km += sample.speed_kmh * dt / 3600.0
            if sample.power_kw is not None and sample.power_kw > 0:
                self.energy_kwh += sample.power_kw * dt / 3600.0
        if sample.speed_kmh is not None:
            self.speeds.append(sample.speed_kmh)
            self.last_speed_kmh = sample.speed_kmh
        if self.distance_km > 0 and self.energy_kwh > 0:
            self.efficiencies.append(self.distance_km / self.energy_kwh)
        self.last_sample_time = sample.time
        self.last_odometer_km = sample.odometer_km

    def end(self, sample: Sample) -> Dict[str, Any]:
        if self.active: self.add_sample(sample)
        self.active = False
        avg_speed = self.distance_km / (self.time_seconds / 3600.0) if self.time_seconds > 0 else 0.0
        avg_eff = self.distance_km / self.energy_kwh if self.energy_kwh > 0 else 0.0
        return {
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": sample.time.isoformat(),
            "start_odometer_km": self.start_odometer_km,
            "end_odometer_km": sample.odometer_km,
            "distance_km": round(self.distance_km, 3),
            "time_seconds": round(self.time_seconds, 1),
            "avg_speed_kmh": round(avg_speed, 1),
            "energy_kwh": round(self.energy_kwh, 3),
            "avg_efficiency_km_per_kwh": round(avg_eff, 2),
            "start_soc": self.start_soc,
            "end_soc": sample.battery_level,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "start_odometer_km": self.start_odometer_km,
            "start_soc": self.start_soc,
            "last_sample_time": self.last_sample_time.isoformat() if self.last_sample_time else None,
            "last_odometer_km": self.last_odometer_km,
            "last_speed_kmh": self.last_speed_kmh,
            "distance_km": self.distance_km,
            "time_seconds": self.time_seconds,
            "energy_kwh": self.energy_kwh,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DriveSession":
        drive = cls()
        drive.active = bool(data.get("active"))
        drive.start_time = parse_dt(data.get("start_time"))
        drive.start_odometer_km = as_float(data.get("start_odometer_km"))
        drive.start_soc = as_float(data.get("start_soc"))
        drive.last_sample_time = parse_dt(data.get("last_sample_time"))
        drive.last_odometer_km = as_float(data.get("last_odometer_km"))
        drive.last_speed_kmh = as_float(data.get("last_speed_kmh"))
        drive.distance_km = float(data.get("distance_km") or 0.0)
        drive.time_seconds = float(data.get("time_seconds") or 0.0)
        drive.energy_kwh = float(data.get("energy_kwh") or 0.0)
        return drive


# =========================
# Telegram
# =========================

class TelegramClient:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            print(f"[telegram disabled] {text}", flush=True)
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                print(f"Telegram error {response.status_code}: {response.text[:300]}", file=sys.stderr, flush=True)
                return False
            return True
        except requests.RequestException as exc:
            print(f"Telegram request failed: {exc}", file=sys.stderr, flush=True)
            return False


# =========================
# Tesla Fleet API
# =========================

class TeslaFleetClient:
    def __init__(self, token_file: Path, state_file: Path, api_base: str = DEFAULT_API_BASE) -> None:
        self.token_file = token_file
        self.state_file = state_file
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self.tokens = load_json(token_file, {})
        self.state = load_json(state_file, {})
        self.access_token = self.tokens.get("access_token") or self.tokens.get("token")
        self.access_token_expires_at = float(self.tokens.get("access_token_expires_at") or 0)
        state_access_token = self.state.get("access_token")
        state_expires_at = float(self.state.get("access_token_expires_at") or 0)
        if state_access_token and state_expires_at > self.access_token_expires_at:
            self.access_token = state_access_token
            self.access_token_expires_at = state_expires_at
        self.refresh_token = self.tokens.get("refresh_token")
        self.client_id = os.getenv("TESLA_CLIENT_ID") or os.getenv("TESLA_AUTH_CLIENT_ID") or DEFAULT_CLIENT_ID
        self.client_secret = os.getenv("TESLA_CLIENT_SECRET", "")
        self.scope = os.getenv("TESLA_SCOPE", DEFAULT_TESLA_SCOPE)

    def access_token_valid(self) -> bool:
        return bool(self.access_token and self.access_token_expires_at > time.time() + 120)

    def save_tokens(self, body: Dict[str, Any]) -> None:
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        if isinstance(access_token, str) and access_token: self.access_token = access_token
        if isinstance(refresh_token, str) and refresh_token: self.refresh_token = refresh_token
        expires_in = int(body.get("expires_in") or 0)
        if expires_in > 0: self.access_token_expires_at = time.time() + max(60, expires_in - 120)
        saved_at = now_kst().isoformat()
        if not self.refresh_token: raise RuntimeError("Tesla token refresh 응답에 refresh_token이 없습니다.")
        token_payload = {"refresh_token": self.refresh_token, "saved_at": saved_at}
        atomic_write_json(self.token_file, token_payload)
        try: os.chmod(self.token_file, 0o600)
        except OSError: pass
        state_payload = load_json(self.state_file, {})
        if self.access_token: state_payload["access_token"] = self.access_token
        if self.access_token_expires_at: state_payload["access_token_expires_at"] = self.access_token_expires_at
        state_payload["last_token_refresh_at"] = time.time()
        state_payload["token_saved_at"] = saved_at
        atomic_write_json(self.state_file, state_payload)
        try: os.chmod(self.state_file, 0o600)
        except OSError: pass
        self.tokens = token_payload
        self.state = state_payload

    def refresh(self) -> None:
        if not self.refresh_token: raise RuntimeError(f"Tesla refresh_token이 없습니다. {self.token_file} 파일을 확인해야 합니다.")
        data = {"grant_type": "refresh_token", "scope": self.scope, "client_id": self.client_id, "refresh_token": self.refresh_token}
        if self.client_secret: data["client_secret"] = self.client_secret
        res = self.session.post(AUTH_TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200: raise RuntimeError(f"Tesla token refresh failed: HTTP {res.status_code} {res.text}")
        self.save_tokens(res.json())

    def fetch_once(self, vin: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
        if not self.access_token_valid(): self.refresh()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        url = f"{self.api_base}/api/1/vehicles"
        res = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code == 401:
            self.refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            res = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200: raise RuntimeError(f"Tesla vehicles API failed: HTTP {res.status_code}")
        vehicles = res.json().get("response") or []
        if not vehicles: return "offline", None
        target = next((v for v in vehicles if v.get("vin") == vin), vehicles[0]) if vin else vehicles[0]
        if not target: return "offline", None
        status = target.get("state") or "offline"
        if status != "online": return status, None
        v_id = target.get("id_s") or target.get("id")
        data_url = f"{self.api_base}/api/1/vehicles/{v_id}/vehicle_data?endpoints={VEHICLE_DATA_ENDPOINTS}"
        res = self.session.get(data_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code == 403 and "vehicle_location" in res.text:
            data_url = f"{self.api_base}/api/1/vehicles/{v_id}/vehicle_data?endpoints={VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION}"
            res = self.session.get(data_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200: return status, None
        return status, res.json().get("response")


# =========================
# Main Poller
# =========================

class LightLogggPoller:
    def __init__(self, client: TeslaFleetClient, telegram: TelegramClient, state_file: Path, command_file: Path, vin: Optional[str] = None) -> None:
        self.client = client
        self.telegram = telegram
        self.state_file = state_file
        self.command_file = command_file
        self.vin = vin
        self.vehicle_id: str = ""
        self.vehicle_name: str = "두삼이"
        self.state: Dict[str, Any] = {
            "daily": {"date": date.today().isoformat(), "total_distance_km": 0.0, "total_time_seconds": 0.0, "total_energy_kwh": 0.0, "drive_sessions": [], "efficiencies": [], "speed_samples": [], "start_soc": None, "end_soc": None},
            "weekly": {"week": now_kst().strftime("%Y-W%U"), "total_distance_km": 0.0, "total_time_seconds": 0.0, "total_energy_kwh": 0.0, "drive_count": 0, "days": {}},
            "last_summary_date": None,
            "last_weekly_summary_iso": None,
            "last_morning_alert_date": None,
            "last_drive_end_soc": None,
            "charging_stats": {"total_added_soc": 0.0, "powers": []}
        }
        self.restore_state()
        self.drive = DriveSession.from_dict(self.state.get("drive") or {})
        self.last_alert_at = 0.0
        self.stop_requested = False
        self.window: Deque[Sample] = deque()
        self.charging_notification_stage = str(self.state.get("charging_notification_stage") or "idle")
        self.charging_start_timestamp = parse_dt(self.state.get("charging_start_timestamp"))
        self.external_drive_boost_until = float(self.state.get("external_drive_boost_until") or 0.0)

    def restore_state(self) -> None:
        loaded = load_json(self.state_file, {})
        if not loaded: return
        for key, value in loaded.items(): self.state[key] = value

    def save_state(self) -> None:
        self.state["charging_notification_stage"] = self.charging_notification_stage
        self.state["charging_start_timestamp"] = self.charging_start_timestamp.isoformat() if self.charging_start_timestamp else None
        self.state["external_drive_boost_until"] = self.external_drive_boost_until
        self.state["drive"] = self.drive.to_dict()
        atomic_write_json(self.state_file, self.state)

    def reset_daily_weekly_if_needed(self) -> None:
        today = now_kst().date().isoformat()
        current_week = now_kst().strftime("%Y-W%U")
        daily = self.state.get("daily") or {}
        if daily.get("date") != today:
            self.state["daily"] = {"date": today, "total_distance_km": 0.0, "total_time_seconds": 0.0, "total_energy_kwh": 0.0, "drive_sessions": [], "efficiencies": [], "speed_samples": [], "start_soc": None, "end_soc": None}
        weekly = self.state.get("weekly") or {}
        if weekly.get("week") != current_week:
            self.state["weekly"] = {"week": current_week, "total_distance_km": 0.0, "total_time_seconds": 0.0, "total_energy_kwh": 0.0, "drive_count": 0, "days": {}}

    def read_command(self) -> Optional[Dict[str, Any]]:
        if not self.command_file.exists(): return None
        try:
            raw = self.command_file.read_text(encoding="utf-8").strip()
            if not raw:
                self.command_file.unlink(missing_ok=True)
                return None
            data = json.loads(raw)
            command = {"command": data} if isinstance(data, str) else data if isinstance(data, dict) else {"command": str(data)}
            self.command_file.unlink(missing_ok=True)
            return command
        except Exception as exc:
            print(f"command file read failed: {exc}", file=sys.stderr, flush=True)
            try:
                broken = self.command_file.with_suffix(f".broken.{int(time.time())}.json")
                self.command_file.replace(broken)
            except Exception: pass
            return None

    def apply_command(self, command: Dict[str, Any]) -> str:
        name = str(command.get("command") or "").strip().lower()
        self.state["last_command"] = {"time": now_kst().isoformat(), "command": name, "raw": command}
        if name in {"poll_now", "wake_poll", "refresh"}:
            self.save_state()
            return "poll_now"
        if name in {"driving_start", "drive_start", "start_driving"}:
            seconds = safe_int(command.get("seconds"), EXTERNAL_DRIVE_BOOST_SECONDS)
            if seconds <= 0: seconds = EXTERNAL_DRIVE_BOOST_SECONDS
            self.external_drive_boost_until = time.time() + seconds
            self.state["external_drive_pending_start"] = True
            self.state["external_drive_pending_stop"] = False
            self.save_state()
            return "poll_now"
        if name in {"driving_stop", "drive_stop", "stop_driving", "clear_boost"}:
            self.external_drive_boost_until = 0.0
            self.state["external_drive_pending_stop"] = True
            self.save_state()
            return "poll_now"
        self.save_state()
        return "ignored"

    def should_boost_driving(self) -> bool:
        return self.external_drive_boost_until > time.time()

    def handle_drive_end_summary(self, session: Dict[str, Any]) -> None:
        distance_km = float(session.get("distance_km") or 0.0)
        if distance_km < 1.0: return
        time_seconds = float(session.get("time_seconds") or 0.0)
        avg_speed = float(session.get("avg_speed_kmh") or 0.0)
        start_soc = as_float(session.get("start_soc"))
        end_soc = as_float(session.get("end_soc"))
        battery_text = f"{start_soc:.0f}% → {end_soc:.0f}% ({start_soc - end_soc:.0f}%p 사용)" if start_soc is not None and end_soc is not None else "확인 불가"
        duration_text = format_seconds_hm(time_seconds)
        self.telegram.send(f"두삼이 주행 종료\n- 주행거리: {distance_km:.2f} km\n- 주행시간: {duration_text}\n- 배터리: {battery_text}\n- 평균속도: {avg_speed:.1f} km/h")

    def handle_scheduled_reports(self) -> None:
        now = now_kst()
        today_str = now.date().isoformat()
        current_week = now.strftime("%Y-W%U")
        if DAILY_REPORT_ENABLED and now.hour == DAILY_REPORT_HOUR and now.minute == DAILY_REPORT_MINUTE and self.state.get("last_summary_date") != today_str:
            self.telegram.send(self.build_daily_report_text())
            self.state["last_summary_date"] = today_str
        if WEEKLY_REPORT_ENABLED and now.weekday() == WEEKLY_REPORT_DAY and now.hour == DAILY_REPORT_HOUR and now.minute == DAILY_REPORT_MINUTE and self.state.get("last_weekly_summary_iso") != current_week:
            self.telegram.send(self.build_weekly_report_text())
            self.state["last_weekly_summary_iso"] = current_week

    def build_daily_report_text(self) -> str:
        daily = self.state.get("daily") or {}
        date_text = daily.get("date") or now_kst().date().isoformat()
        distance = float(daily.get("total_distance_km") or 0.0)
        seconds = float(daily.get("total_time_seconds") or 0.0)
        sessions = daily.get("drive_sessions") or []
        start_soc = as_float(daily.get("start_soc"))
        end_soc = as_float(daily.get("end_soc"))
        avg_speed = distance / (seconds / 3600.0) if seconds > 0 else 0.0
        battery_text = f"{start_soc:.0f}% → {end_soc:.0f}% ({start_soc - end_soc:.0f}%p 사용)" if start_soc is not None and end_soc is not None else "확인 불가"
        return f"두삼이 일일 리포트\n- 날짜: {date_text}\n- 주행 횟수: {len(sessions)}회\n- 총 주행거리: {distance:.2f} km\n- 총 주행시간: {format_seconds_hm(seconds)}\n- 평균속도: {avg_speed:.1f} km/h\n- 배터리: {battery_text}"

    def build_weekly_report_text(self) -> str:
        weekly = self.state.get("weekly") or {}
        week_text = weekly.get("week") or now_kst().strftime("%Y-W%U")
        distance = float(weekly.get("total_distance_km") or 0.0)
        seconds = float(weekly.get("total_time_seconds") or 0.0)
        drive_count = int(weekly.get("drive_count") or 0)
        avg_speed = distance / (seconds / 3600.0) if seconds > 0 else 0.0
        return f"두삼이 주간 리포트\n- 주차: {week_text}\n- 주행 횟수: {drive_count}회\n- 총 주행거리: {distance:.2f} km\n- 총 주행시간: {format_seconds_hm(seconds)}\n- 평균속도: {avg_speed:.1f} km/h"

    def is_driving(self, sample: Sample) -> bool:
        return (sample.speed_kmh or 0.0) > 2.0 or sample.shift_state in {"D", "R"}

    def is_charging(self, vehicle: Optional[Dict[str, Any]]) -> bool:
        if not vehicle: return False
        charge_state = vehicle.get("charge_state") or {}
        return charge_state.get("charging_state") == "Charging"

    def sample_from_vehicle(self, vehicle: Dict[str, Any]) -> Sample:
        drive_state = vehicle.get("drive_state") or {}
        charge_state = vehicle.get("charge_state") or {}
        vehicle_state = vehicle.get("vehicle_state") or {}
        speed_mph = as_float(drive_state.get("speed"))
        odometer_miles = as_float(vehicle_state.get("odometer"))
        drive_power = as_float(drive_state.get("power"))
        charger_power = as_float(charge_state.get("charger_power"))
        power_kw = drive_power if drive_power is not None else charger_power
        return Sample(time=now_kst(), speed_kmh=speed_mph_to_kmh(speed_mph), power_kw=power_kw, odometer_km=miles_to_km(odometer_miles), battery_level=as_float(charge_state.get("battery_level")), latitude=None, longitude=None, shift_state=drive_state.get("shift_state"), charging_state=charge_state.get("charging_state"))

    def handle_morning_alert(self, vehicle: Optional[Dict[str, Any]]) -> None:
        now = now_kst()
        today_str = now.date().isoformat()
        target_time = now.replace(hour=MORNING_ALERT_HOUR, minute=MORNING_ALERT_MINUTE, second=0, microsecond=0)
        if now < target_time or (now - target_time).total_seconds() > 2 * 3600 or self.state.get("last_morning_alert_date") == today_str: return
        
        last_poll = self.state.get("last_poll") or {}
        charge_state = vehicle.get("charge_state") or {} if vehicle else {}
        battery_level = as_float(charge_state.get("battery_level")) or as_float(last_poll.get("battery_level"))
        last_drive_end_soc = self.state.get("last_drive_end_soc")
        
        lines = ["좋은 아침 ☀️", "두삼이 아침 현황입니다."]
        if battery_level is not None:
            lines.append(f"- 현재 배터리: {battery_level:.0f}%")
            if last_drive_end_soc is not None:
                if battery_level > last_drive_end_soc:
                    added = battery_level - last_drive_end_soc
                    stats = self.state.get("charging_stats") or {}
                    powers = stats.get("powers") or []
                    avg_power = sum(powers) / len(powers) if powers else 0
                    lines.append(f"- 야간 충전됨: +{added:.0f}%p")
                    if avg_power > 0: lines.append(f"- 평균 충전 속도: {avg_power:.1f} kW")
                elif battery_level < last_drive_end_soc:
                    lines.append(f"- 배터리 소모: {last_drive_end_soc - battery_level:.0f}%p (주행 후)")
        else:
            lines.append("- 배터리: 확인 불가")
            
        est_range_km = miles_to_km(as_float(charge_state.get("est_battery_range"))) or as_float(last_poll.get("est_battery_range_km"))
        if est_range_km is not None: lines.append(f"- 주행가능거리: {est_range_km:.0f} km 예상")
        
        self.telegram.send("\n".join(lines))
        self.state["last_morning_alert_date"] = today_str
        self.state["charging_stats"] = {"total_added_soc": 0.0, "powers": []}

    def handle_charging_notifications(self, vehicle: Dict[str, Any]) -> None:
        charge_state = vehicle.get("charge_state") or {}
        battery_level = as_float(charge_state.get("battery_level"))
        charger_power = as_float(charge_state.get("charger_power"))
        time_to_full = as_float(charge_state.get("time_to_full_charge"))
        if charger_power is not None and charger_power > 0:
            stats = self.state.get("charging_stats") or {"total_added_soc": 0.0, "powers": []}
            powers = stats.get("powers") or []
            powers.append(charger_power)
            stats["powers"] = powers[-300:]
            self.state["charging_stats"] = stats
        if self.charging_notification_stage == "idle":
            self.telegram.send(f"충전 시작\n- 현재 배터리: {battery_level:.0f}%" if battery_level is not None else "충전 시작\n- 배터리 확인 불가")
            self.charging_notification_stage = "initial_notified"
            self.charging_start_timestamp = now_kst()
        elif self.charging_notification_stage == "initial_notified" and self.charging_start_timestamp:
            if (now_kst() - self.charging_start_timestamp).total_seconds() >= 180:
                soc_text = f"{battery_level:.0f}%" if battery_level is not None else "확인 불가"
                kw_text = f"{charger_power:.1f} kW" if charger_power is not None else "확인 불가"
                self.telegram.send(f"충전 중 3분 경과\n- 현재 배터리: {soc_text}\n- 충전 속도: {kw_text}\n- 완료 예상: {format_duration_hours_minutes(time_to_full)}\n- 예상 시각: {format_eta_clock(time_to_full)}")
                self.charging_notification_stage = "detailed_notified"

    def update_daily_weekly_after_drive(self, session: Dict[str, Any]) -> None:
        self.reset_daily_weekly_if_needed()
        daily, weekly = self.state.get("daily") or {}, self.state.get("weekly") or {}
        dist, sec, nrg, eff = float(session.get("distance_km") or 0.0), float(session.get("time_seconds") or 0.0), float(session.get("energy_kwh") or 0.0), float(session.get("avg_efficiency_km_per_kwh") or 0.0)
        daily["total_distance_km"] = float(daily.get("total_distance_km") or 0.0) + dist
        daily["total_time_seconds"] = float(daily.get("total_time_seconds") or 0.0) + sec
        daily["total_energy_kwh"] = float(daily.get("total_energy_kwh") or 0.0) + nrg
        daily["end_soc"] = session.get("end_soc")
        if daily.get("start_soc") is None: daily["start_soc"] = session.get("start_soc")
        if eff > 0:
            effs = daily.get("efficiencies") or []
            effs.append(eff)
            daily["efficiencies"] = effs[-500:]
        sessions = daily.get("drive_sessions") or []
        sessions.append(session)
        daily["drive_sessions"] = sessions[-100:]
        weekly["total_distance_km"] = float(weekly.get("total_distance_km") or 0.0) + dist
        weekly["total_time_seconds"] = float(weekly.get("total_time_seconds") or 0.0) + sec
        weekly["total_energy_kwh"] = float(weekly.get("total_energy_kwh") or 0.0) + nrg
        weekly["drive_count"] = int(weekly.get("drive_count") or 0) + 1
        day_key = now_kst().date().isoformat()
        days = weekly.get("days") or {}
        day = days.get(day_key) or {"distance_km": 0.0, "time_seconds": 0.0, "energy_kwh": 0.0, "drive_count": 0}
        day["distance_km"] += dist
        day["time_seconds"] += sec
        day["energy_kwh"] += nrg
        day["drive_count"] += 1
        days[day_key] = day
        weekly["days"] = days
        self.state["daily"], self.state["weekly"] = daily, weekly

    def update_last_poll(self, status: str, vehicle: Optional[Dict[str, Any]], interval: int, sample: Optional[Sample] = None) -> None:
        payload = {"time": now_kst().isoformat(), "status": status, "next_seconds": interval, "vehicle_id": self.vehicle_id, "vehicle_name": self.vehicle_name, "config": {"asleep_seconds": POLL_ASLEEP_SECONDS, "online_seconds": POLL_ONLINE_SECONDS, "driving_seconds": POLL_DRIVING_SECONDS, "charging_seconds": POLL_CHARGING_SECONDS, "error_seconds": POLL_ERROR_SECONDS}}
        if vehicle:
            cs, vs, ds = vehicle.get("charge_state") or {}, vehicle.get("vehicle_state") or {}, vehicle.get("drive_state") or {}
            payload.update({"charging_state": cs.get("charging_state"), "battery_level": cs.get("battery_level"), "shift_state": ds.get("shift_state"), "charger_power_kw": cs.get("charger_power"), "time_to_full_charge": cs.get("time_to_full_charge"), "est_battery_range_km": round(miles_to_km(as_float(cs.get("est_battery_range"))), 1) if cs.get("est_battery_range") else None})
            om = miles_to_km(as_float(vs.get("odometer")))
            if om: payload["odometer_km"] = round(om, 1)
        if sample: payload["speed_kmh"] = round(sample.speed_kmh, 1) if sample.speed_kmh is not None else None
        self.state["last_poll"] = payload

    def process_vehicle(self, status: str, vehicle: Optional[Dict[str, Any]]) -> int:
        self.restore_state()
        self.reset_daily_weekly_if_needed()
        self.handle_morning_alert(vehicle)
        charging = self.is_charging(vehicle)
        if charging and vehicle:
            self.handle_charging_notifications(vehicle)
        elif vehicle:
            if self.charging_notification_stage != "idle":
                self.telegram.send(f"충전 완료\n- 현재 배터리: {as_float((vehicle.get('charge_state') or {}).get('battery_level')):.0f}%")
            self.charging_notification_stage = "idle"
            self.charging_start_timestamp = None
        
        if not vehicle or status in {"offline", "asleep"}:
            interval = POLL_DRIVING_SECONDS if self.should_boost_driving() else POLL_ASLEEP_SECONDS
            self.update_last_poll(status, vehicle, interval)
            self.handle_scheduled_reports()
            self.save_state()
            return interval
            
        sample = self.sample_from_vehicle(vehicle)
        was_driving = self.drive.active
        is_driving = self.is_driving(sample)
        if is_driving:
            if not was_driving: self.drive.start(sample)
            self.drive.add_sample(sample)
            interval = POLL_DRIVING_SECONDS
        else:
            if was_driving:
                summary = self.drive.end(sample)
                self.handle_drive_end_summary(summary)
                self.update_daily_weekly_after_drive(summary)
                append_trip_to_csv(TRIPS_CSV_FILE, summary)
                self.state["last_drive_end_soc"] = sample.battery_level
            interval = POLL_CHARGING_SECONDS if charging else POLL_ONLINE_SECONDS
        self.update_last_poll(status, vehicle, interval, sample)
        self.handle_scheduled_reports()
        self.save_state()
        return interval

    def run_once(self) -> int:
        try:
            cmd = self.read_command()
            if cmd: self.apply_command(cmd)
            status, vehicle = self.client.fetch_once(self.vin)
            if vehicle:
                self.vehicle_id = vehicle.get("id_s") or str(vehicle.get("id"))
                self.vehicle_name = vehicle.get("display_name") or "두삼이"
            interval = self.process_vehicle(status, vehicle)
            print(f"{now_kst().isoformat()} status={status} next={interval}s", flush=True)
            return interval
        except Exception as exc:
            print(f"{now_kst().isoformat()} error={exc}", file=sys.stderr, flush=True)
            return POLL_ERROR_SECONDS

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, "stop_requested", True))
        self.telegram.send("LIGHT LOGGG Tesla 폴링 시작")
        while not self.stop_requested:
            interval = self.run_once()
            slept = 0
            while slept < interval and not self.stop_requested:
                time.sleep(min(1, interval - slept))
                slept += 1
        self.save_state()
        self.telegram.send("LIGHT LOGGG Tesla 폴링 종료")

def main() -> int:
    load_dotenv(Path(".env"))
    load_dotenv(Path.home() / ".light_loggg.env")
    init_runtime_config(DEFAULT_PUBLIC_CONFIG_FILE)
    client = TeslaFleetClient(DEFAULT_TOKEN_FILE, DEFAULT_STATE_FILE)
    telegram = TelegramClient()
    poller = LightLogggPoller(client, telegram, DEFAULT_STATE_FILE, DEFAULT_COMMAND_FILE, os.getenv("TESLA_VIN"))
    if "--once" in sys.argv: poller.run_once()
    else: poller.run_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
