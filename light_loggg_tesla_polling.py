#!/usr/bin/env python3
"""
LIGHT LOGGG Tesla Fleet API polling handler.

목표:
- Termux에서 가볍게 Tesla Fleet API polling
- 차량이 asleep/offline이면 vehicle_data 호출하지 않음
- 주행/충전/온라인/수면 상태별 polling 주기 분리
- Telegram 상태 알림/요약용 state 저장
- 세컨폰/Telegram 명령으로 polling 즉시 깨우기 지원

설정 구조:
- 공개 설정: ~/light_loggg_tesla/light_loggg_public_config.json
- 비공개 설정: ~/.light_loggg.env
- token 파일: ~/.light_loggg_tesla_tokens.json
- state 파일: ~/.light_loggg_state.json

우선순위:
1. 환경변수 / ~/.light_loggg.env
2. light_loggg_public_config.json
3. 코드 기본값
"""

from __future__ import annotations

import argparse
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
    "location_data",
    "vehicle_config",
    "vehicle_state",
    "vehicle_data_combo",
]

VEHICLE_DATA_ENDPOINTS = ";".join(VEHICLE_DATA_ENDPOINT_LIST)

VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION = ";".join(
    endpoint for endpoint in VEHICLE_DATA_ENDPOINT_LIST if endpoint != "location_data"
)

DEFAULT_TESLA_SCOPE = "openid offline_access user_data vehicle_device_data vehicle_location"

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


# 전역 설정값은 main()에서 env/config 로드 후 초기화된다.
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


def init_runtime_config(config_file: Path) -> None:
    global PUBLIC_CONFIG
    global POLL_ASLEEP_SECONDS, POLL_ONLINE_SECONDS, POLL_DRIVING_SECONDS
    global POLL_CHARGING_SECONDS, POLL_ERROR_SECONDS
    global WINDOW_SIZE_MINUTES, THRESHOLD_EFFICIENCY, LOW_EFFICIENCY_ALERT_COOLDOWN
    global REQUEST_TIMEOUT, EXTERNAL_DRIVE_BOOST_SECONDS
    global MORNING_ALERT_HOUR, MORNING_ALERT_MINUTE

    PUBLIC_CONFIG = load_public_config(config_file)

    POLL_ASLEEP_SECONDS = cfg_int(
        PUBLIC_CONFIG, "polling", "asleep_seconds", "LIGHT_LOGGG_POLL_ASLEEP_SECONDS", 1800
    )
    POLL_ONLINE_SECONDS = cfg_int(
        PUBLIC_CONFIG, "polling", "online_seconds", "LIGHT_LOGGG_POLL_ONLINE_SECONDS", 300
    )
    POLL_DRIVING_SECONDS = cfg_int(
        PUBLIC_CONFIG, "polling", "driving_seconds", "LIGHT_LOGGG_POLL_DRIVING_SECONDS", 10
    )
    POLL_CHARGING_SECONDS = cfg_int(
        PUBLIC_CONFIG, "polling", "charging_seconds", "LIGHT_LOGGG_POLL_CHARGING_SECONDS", 60
    )
    POLL_ERROR_SECONDS = cfg_int(
        PUBLIC_CONFIG, "polling", "error_seconds", "LIGHT_LOGGG_POLL_ERROR_SECONDS", 300
    )

    WINDOW_SIZE_MINUTES = cfg_float(
        PUBLIC_CONFIG, "alerts", "window_minutes", "LIGHT_LOGGG_WINDOW_MINUTES", 3.0
    )
    THRESHOLD_EFFICIENCY = cfg_float(
        PUBLIC_CONFIG, "alerts", "threshold_km_per_kwh", "LIGHT_LOGGG_THRESHOLD_KM_PER_KWH", 4.5
    )
    LOW_EFFICIENCY_ALERT_COOLDOWN = cfg_int(
        PUBLIC_CONFIG, "alerts", "alert_cooldown_seconds", "LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS", 60
    )

    REQUEST_TIMEOUT = cfg_int(
        PUBLIC_CONFIG, "request", "timeout_seconds", "LIGHT_LOGGG_REQUEST_TIMEOUT", 25
    )

    EXTERNAL_DRIVE_BOOST_SECONDS = cfg_int(
        PUBLIC_CONFIG, "external_commands", "drive_boost_seconds", "LIGHT_LOGGG_EXTERNAL_DRIVE_BOOST_SECONDS", 180
    )

    MORNING_ALERT_HOUR = cfg_int(
        PUBLIC_CONFIG, "morning_alert", "hour", "LIGHT_LOGGG_MORNING_ALERT_HOUR", 6
    )
    MORNING_ALERT_MINUTE = cfg_int(
        PUBLIC_CONFIG, "morning_alert", "minute", "LIGHT_LOGGG_MORNING_ALERT_MINUTE", 30
    )

    print(
        "runtime_config "
        f"asleep={POLL_ASLEEP_SECONDS} "
        f"online={POLL_ONLINE_SECONDS} "
        f"driving={POLL_DRIVING_SECONDS} "
        f"charging={POLL_CHARGING_SECONDS} "
        f"error={POLL_ERROR_SECONDS} "
        f"boost={EXTERNAL_DRIVE_BOOST_SECONDS} "
        f"timeout={REQUEST_TIMEOUT}",
        flush=True,
    )


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
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
        backup = path.with_suffix(path.suffix + f".broken.{int(time.time())}")
        try:
            path.replace(backup)
        except Exception:
            pass
        return dict(default)


def as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def speed_mph_to_kmh(speed_mph: Optional[float]) -> Optional[float]:
    if speed_mph is None:
        return None
    return float(speed_mph) * 1.609344


def miles_to_km(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return value * 1.609344


def haversine_km(
    lat1: Optional[float],
    lon1: Optional[float],
    lat2: Optional[float],
    lon2: Optional[float],
) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None

    radius = 6371.0088

    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


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
    accel_count: int = 0
    decel_count: int = 0

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
        self.accel_count = 0
        self.decel_count = 0

    def add_sample(self, sample: Sample) -> None:
        if not self.active:
            self.start(sample)

        if self.last_sample_time is not None:
            dt = max(0.0, (sample.time - self.last_sample_time).total_seconds())
        else:
            dt = 0.0

        if dt > 0:
            self.time_seconds += dt

            if sample.odometer_km is not None and self.last_odometer_km is not None:
                delta_odo = sample.odometer_km - self.last_odometer_km
                if 0 <= delta_odo < 5:
                    self.distance_km += delta_odo
            elif sample.speed_kmh is not None:
                self.distance_km += sample.speed_kmh * dt / 3600.0

            if sample.power_kw is not None and sample.power_kw < 0:
                self.energy_kwh += abs(sample.power_kw) * dt / 3600.0

        if sample.speed_kmh is not None:
            self.speeds.append(sample.speed_kmh)

            if self.last_speed_kmh is not None and dt > 0:
                delta_speed = sample.speed_kmh - self.last_speed_kmh
                accel_kmh_per_s = delta_speed / dt

                if accel_kmh_per_s >= 8:
                    self.accel_count += 1
                elif accel_kmh_per_s <= -10:
                    self.decel_count += 1

            self.last_speed_kmh = sample.speed_kmh

        if self.distance_km > 0 and self.energy_kwh > 0:
            self.efficiencies.append(self.distance_km / self.energy_kwh)

        self.last_sample_time = sample.time
        self.last_odometer_km = sample.odometer_km

    def end(self, sample: Sample) -> Dict[str, Any]:
        if self.active:
            self.add_sample(sample)

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
            "accel_count": self.accel_count,
            "decel_count": self.decel_count,
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
            "accel_count": self.accel_count,
            "decel_count": self.decel_count,
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
        drive.accel_count = int(data.get("accel_count") or 0)
        drive.decel_count = int(data.get("decel_count") or 0)
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
        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }

        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                print(
                    f"Telegram error {response.status_code}: {response.text[:300]}",
                    file=sys.stderr,
                    flush=True,
                )
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

        if isinstance(access_token, str) and access_token:
            self.access_token = access_token

        if isinstance(refresh_token, str) and refresh_token:
            self.refresh_token = refresh_token

        expires_in = int(body.get("expires_in") or 0)
        if expires_in > 0:
            self.access_token_expires_at = time.time() + max(60, expires_in - 120)

        saved_at = now_kst().isoformat()

        if not self.refresh_token:
            raise RuntimeError("Tesla token refresh 응답에 refresh_token이 없습니다.")

        token_payload = {
            "refresh_token": self.refresh_token,
            "saved_at": saved_at,
        }

        atomic_write_json(self.token_file, token_payload)

        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

        state_payload = load_json(self.state_file, {})

        if self.access_token:
            state_payload["access_token"] = self.access_token

        if self.access_token_expires_at:
            state_payload["access_token_expires_at"] = self.access_token_expires_at

        state_payload["last_token_refresh_at"] = time.time()
        state_payload["token_saved_at"] = saved_at

        atomic_write_json(self.state_file, state_payload)

        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

        self.tokens = token_payload
        self.state = state_payload

    def refresh(self) -> None:
        if not self.refresh_token:
            raise RuntimeError(f"Tesla refresh_token이 없습니다. token_file={self.token_file}")

        data = {
            "grant_type": "refresh_token",
            "scope": self.scope,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
        }

        if self.client_secret:
            data["client_secret"] = self.client_secret

        response = self.session.post(
            AUTH_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            raise RuntimeError(f"Tesla token refresh failed: HTTP {response.status_code} {response.text}")

        self.save_tokens(response.json())

    def get_vehicles(self) -> List[Dict[str, Any]]:
        if not self.access_token_valid():
            self.refresh()

        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        url = f"{self.api_base}/api/1/vehicles"

        response = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 401:
            self.refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            raise RuntimeError(f"Tesla vehicles API failed: HTTP {response.status_code} {response.text[:300]}")

        data = response.json()
        vehicles = data.get("response") or []

        if not isinstance(vehicles, list):
            return []

        return vehicles

    def fetch_once(self, vin: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
        vehicles = self.get_vehicles()

        if not vehicles:
            return "offline", None

        if vin:
            target = next((vehicle for vehicle in vehicles if vehicle.get("vin") == vin), None)
        else:
            target = vehicles[0]

        if not target:
            return "offline", None

        status = target.get("state") or "offline"

        # 핵심: asleep/offline이면 vehicle_data 호출 안 함.
        if status != "online":
            return status, None

        vehicle_id = target.get("id_s") or target.get("id")

        if not vehicle_id:
            return status, None

        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        data_url = (
            f"{self.api_base}/api/1/vehicles/{vehicle_id}/vehicle_data"
            f"?endpoints={VEHICLE_DATA_ENDPOINTS}"
        )

        response = self.session.get(data_url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 403 and "vehicle_location" in response.text:
            print(
                "Tesla token lacks vehicle_location scope; retrying vehicle_data without location_data.",
                flush=True,
            )
            data_url = (
                f"{self.api_base}/api/1/vehicles/{vehicle_id}/vehicle_data"
                f"?endpoints={VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION}"
            )
            response = self.session.get(data_url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            print(
                f"Tesla vehicle_data failed: HTTP {response.status_code} {response.text[:300]}",
                file=sys.stderr,
                flush=True,
            )
            return status, None

        vehicle_data = response.json().get("response")

        if not isinstance(vehicle_data, dict):
            return status, None

        return status, vehicle_data


# =========================
# Main poller
# =========================

class LightLogggPoller:
    def __init__(
        self,
        client: TeslaFleetClient,
        telegram: TelegramClient,
        state_file: Path,
        command_file: Path,
        vin: Optional[str] = None,
    ) -> None:
        self.client = client
        self.telegram = telegram
        self.state_file = state_file
        self.command_file = command_file
        self.vin = vin

        self.vehicle_id = ""
        self.vehicle_name = "두삼이"

        self.default_state: Dict[str, Any] = {
            "daily": {
                "date": now_kst().date().isoformat(),
                "total_distance_km": 0.0,
                "total_time_seconds": 0.0,
                "total_energy_kwh": 0.0,
                "drive_sessions": [],
                "efficiencies": [],
                "speed_samples": [],
                "accel_count": 0,
                "decel_count": 0,
                "start_soc": None,
                "end_soc": None,
            },
            "weekly": {
                "week": now_kst().strftime("%Y-W%U"),
                "total_distance_km": 0.0,
                "total_time_seconds": 0.0,
                "total_energy_kwh": 0.0,
                "drive_count": 0,
                "days": {},
            },
            "last_poll": {},
            "last_summary_date": None,
            "last_weekly_summary_iso": None,
            "last_morning_alert_date": None,
            "last_drive_end_soc": None,
            "charging_stats": {
                "total_added_soc": 0.0,
                "powers": [],
            },
            "charging_notification_stage": "idle",
            "charging_start_timestamp": None,
            "external_drive_boost_until": 0.0,
            "last_command": None,
        }

        self.state = dict(self.default_state)
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

        if not loaded:
            return

        for key, value in loaded.items():
            self.state[key] = value

    def save_state(self) -> None:
        self.state["charging_notification_stage"] = self.charging_notification_stage
        self.state["charging_start_timestamp"] = (
            self.charging_start_timestamp.isoformat() if self.charging_start_timestamp else None
        )
        self.state["external_drive_boost_until"] = self.external_drive_boost_until
        self.state["drive"] = self.drive.to_dict()

        atomic_write_json(self.state_file, self.state)

    def reset_daily_weekly_if_needed(self) -> None:
        today = now_kst().date().isoformat()
        current_week = now_kst().strftime("%Y-W%U")

        daily = self.state.get("daily") or {}
        if daily.get("date") != today:
            self.state["daily"] = {
                "date": today,
                "total_distance_km": 0.0,
                "total_time_seconds": 0.0,
                "total_energy_kwh": 0.0,
                "drive_sessions": [],
                "efficiencies": [],
                "speed_samples": [],
                "accel_count": 0,
                "decel_count": 0,
                "start_soc": None,
                "end_soc": None,
            }

        weekly = self.state.get("weekly") or {}
        if weekly.get("week") != current_week:
            self.state["weekly"] = {
                "week": current_week,
                "total_distance_km": 0.0,
                "total_time_seconds": 0.0,
                "total_energy_kwh": 0.0,
                "drive_count": 0,
                "days": {},
            }

    # -------------------------
    # Command file support
    # -------------------------

    def read_command(self) -> Optional[Dict[str, Any]]:
        if not self.command_file.exists():
            return None

        try:
            raw = self.command_file.read_text(encoding="utf-8").strip()

            if not raw:
                self.command_file.unlink(missing_ok=True)
                return None

            data = json.loads(raw)

            if isinstance(data, str):
                command = {"command": data}
            elif isinstance(data, dict):
                command = data
            else:
                command = {"command": str(data)}

            self.command_file.unlink(missing_ok=True)
            return command

        except Exception as exc:
            print(f"command file read failed: {exc}", file=sys.stderr, flush=True)
            try:
                broken = self.command_file.with_suffix(f".broken.{int(time.time())}.json")
                self.command_file.replace(broken)
            except Exception:
                pass
            return None

    def apply_command(self, command: Dict[str, Any]) -> str:
        name = str(command.get("command") or "").strip().lower()

        self.state["last_command"] = {
            "time": now_kst().isoformat(),
            "command": name,
            "raw": command,
        }

        if name in {"poll_now", "wake_poll", "refresh"}:
            self.save_state()
            return "poll_now"

        if name in {"driving_start", "drive_start", "start_driving"}:
            seconds = safe_int(command.get("seconds"), EXTERNAL_DRIVE_BOOST_SECONDS)
            if seconds <= 0:
                seconds = EXTERNAL_DRIVE_BOOST_SECONDS

            self.external_drive_boost_until = time.time() + seconds
            self.telegram.send(
                "외부 주행 시작 신호 수신\n"
                f"- source: {command.get('source') or '-'}\n"
                f"- boost: {seconds}초"
            )
            self.save_state()
            return "poll_now"

        if name in {"driving_stop", "drive_stop", "stop_driving", "clear_boost"}:
            self.external_drive_boost_until = 0.0
            self.telegram.send(
                "외부 주행 종료 신호 수신\n"
                f"- source: {command.get('source') or '-'}"
            )
            self.save_state()
            return "poll_now"

        print(f"unknown command ignored: {command}", flush=True)
        self.save_state()
        return "ignored"

    def should_boost_driving(self) -> bool:
        return self.external_drive_boost_until > time.time()

    # -------------------------
    # Vehicle parsing
    # -------------------------

    def is_driving(self, sample: Sample) -> bool:
        return (sample.speed_kmh or 0.0) > 2.0 or sample.shift_state in {"D", "R"}

    def is_charging(self, vehicle: Optional[Dict[str, Any]]) -> bool:
        if not vehicle:
            return False

        charge_state = vehicle.get("charge_state") or {}
        return charge_state.get("charging_state") == "Charging"

    def sample_from_vehicle(self, vehicle: Dict[str, Any]) -> Sample:
        drive_state = vehicle.get("drive_state") or {}
        charge_state = vehicle.get("charge_state") or {}
        vehicle_state = vehicle.get("vehicle_state") or {}

        speed_mph = as_float(drive_state.get("speed"))
        odometer_miles = as_float(vehicle_state.get("odometer"))
        charger_power = as_float(charge_state.get("charger_power"))

        return Sample(
            time=now_kst(),
            speed_kmh=speed_mph_to_kmh(speed_mph),
            power_kw=charger_power,
            odometer_km=miles_to_km(odometer_miles),
            battery_level=as_float(charge_state.get("battery_level")),
            latitude=as_float(drive_state.get("latitude")),
            longitude=as_float(drive_state.get("longitude")),
            shift_state=drive_state.get("shift_state"),
            charging_state=charge_state.get("charging_state"),
        )

    # -------------------------
    # Notifications / summaries
    # -------------------------

    def handle_morning_alert(self, vehicle: Optional[Dict[str, Any]]) -> None:
        now = now_kst()
        today_str = now.date().isoformat()

        if now.hour != MORNING_ALERT_HOUR or now.minute < MORNING_ALERT_MINUTE:
            return

        if self.state.get("last_morning_alert_date") == today_str:
            return

        if not vehicle:
            self.telegram.send("굿모닝. 차량이 offline/asleep 상태라 현재 상세 상태를 확인하지 않았습니다.")
            self.state["last_morning_alert_date"] = today_str
            return

        charge_state = vehicle.get("charge_state") or {}
        current_soc = as_float(charge_state.get("battery_level"))
        last_drive_soc = as_float(self.state.get("last_drive_end_soc"))

        message = "굿모닝. 오늘의 차량 상태입니다.\n"

        if current_soc is not None:
            message += f"- 배터리: {current_soc:.0f}%\n"

        if last_drive_soc is not None and current_soc is not None:
            added_soc = current_soc - last_drive_soc
            if added_soc > 0:
                message += f"- 마지막 주행 종료 후 충전량: +{added_soc:.0f}%p\n"

                stats = self.state.get("charging_stats") or {}
                powers = stats.get("powers") or []
                if powers:
                    avg_power = sum(powers) / len(powers)
                    message += f"- 평균 충전 속도: {avg_power:.1f} kW\n"

        self.telegram.send(message.rstrip())
        self.state["last_morning_alert_date"] = today_str
        self.state["charging_stats"] = {
            "total_added_soc": 0.0,
            "powers": [],
        }

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
            soc_text = f"{battery_level:.0f}%" if battery_level is not None else "확인 불가"
            self.telegram.send(f"충전 시작\n- 현재 배터리: {soc_text}")
            self.charging_notification_stage = "initial_notified"
            self.charging_start_timestamp = now_kst()
            return

        if self.charging_notification_stage == "initial_notified":
            if not self.charging_start_timestamp:
                self.charging_start_timestamp = now_kst()
                return

            elapsed = (now_kst() - self.charging_start_timestamp).total_seconds()

            if elapsed >= 180:
                soc_text = f"{battery_level:.0f}%" if battery_level is not None else "확인 불가"
                kw_text = f"{charger_power:.1f} kW" if charger_power is not None else "확인 불가"

                if time_to_full is not None:
                    eta_text = f"{int(time_to_full * 60)}분"
                else:
                    eta_text = "확인 불가"

                self.telegram.send(
                    "충전 중 3분 경과\n"
                    f"- 현재 배터리: {soc_text}\n"
                    f"- 충전 속도: {kw_text}\n"
                    f"- 완료 예상: {eta_text}"
                )
                self.charging_notification_stage = "detailed_notified"

    def update_daily_weekly_after_drive(self, session: Dict[str, Any]) -> None:
        self.reset_daily_weekly_if_needed()

        daily = self.state.get("daily") or {}
        weekly = self.state.get("weekly") or {}

        distance = float(session.get("distance_km") or 0.0)
        seconds = float(session.get("time_seconds") or 0.0)
        energy = float(session.get("energy_kwh") or 0.0)
        efficiency = float(session.get("avg_efficiency_km_per_kwh") or 0.0)

        daily["total_distance_km"] = float(daily.get("total_distance_km") or 0.0) + distance
        daily["total_time_seconds"] = float(daily.get("total_time_seconds") or 0.0) + seconds
        daily["total_energy_kwh"] = float(daily.get("total_energy_kwh") or 0.0) + energy
        daily["accel_count"] = int(daily.get("accel_count") or 0) + int(session.get("accel_count") or 0)
        daily["decel_count"] = int(daily.get("decel_count") or 0) + int(session.get("decel_count") or 0)
        daily["end_soc"] = session.get("end_soc")

        if daily.get("start_soc") is None:
            daily["start_soc"] = session.get("start_soc")

        if efficiency > 0:
            effs = daily.get("efficiencies") or []
            effs.append(efficiency)
            daily["efficiencies"] = effs[-500:]

        sessions = daily.get("drive_sessions") or []
        sessions.append(session)
        daily["drive_sessions"] = sessions[-100:]

        weekly["total_distance_km"] = float(weekly.get("total_distance_km") or 0.0) + distance
        weekly["total_time_seconds"] = float(weekly.get("total_time_seconds") or 0.0) + seconds
        weekly["total_energy_kwh"] = float(weekly.get("total_energy_kwh") or 0.0) + energy
        weekly["drive_count"] = int(weekly.get("drive_count") or 0) + 1

        day_key = now_kst().date().isoformat()
        days = weekly.get("days") or {}
        day = days.get(day_key) or {
            "distance_km": 0.0,
            "time_seconds": 0.0,
            "energy_kwh": 0.0,
            "drive_count": 0,
        }

        day["distance_km"] = float(day.get("distance_km") or 0.0) + distance
        day["time_seconds"] = float(day.get("time_seconds") or 0.0) + seconds
        day["energy_kwh"] = float(day.get("energy_kwh") or 0.0) + energy
        day["drive_count"] = int(day.get("drive_count") or 0) + 1

        days[day_key] = day
        weekly["days"] = days

        self.state["daily"] = daily
        self.state["weekly"] = weekly

    # -------------------------
    # Poll processing
    # -------------------------

    def update_last_poll(
        self,
        status: str,
        vehicle: Optional[Dict[str, Any]],
        interval: int,
        sample: Optional[Sample] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "time": now_kst().isoformat(),
            "status": status,
            "next_seconds": interval,
            "vehicle_id": self.vehicle_id,
            "vehicle_name": self.vehicle_name,
            "external_drive_boost": self.should_boost_driving(),
            "config": {
                "asleep_seconds": POLL_ASLEEP_SECONDS,
                "online_seconds": POLL_ONLINE_SECONDS,
                "driving_seconds": POLL_DRIVING_SECONDS,
                "charging_seconds": POLL_CHARGING_SECONDS,
                "error_seconds": POLL_ERROR_SECONDS,
            },
        }

        if vehicle:
            charge_state = vehicle.get("charge_state") or {}
            vehicle_state = vehicle.get("vehicle_state") or {}
            drive_state = vehicle.get("drive_state") or {}

            payload.update(
                {
                    "charging_state": charge_state.get("charging_state"),
                    "battery_level": charge_state.get("battery_level"),
                    "shift_state": drive_state.get("shift_state"),
                }
            )

            odometer_miles = as_float(vehicle_state.get("odometer"))
            odometer_km = miles_to_km(odometer_miles)
            if odometer_km is not None:
                payload["odometer_km"] = round(odometer_km, 1)

        if sample:
            payload.update(
                {
                    "speed_kmh": round(sample.speed_kmh, 1) if sample.speed_kmh is not None else None,
                    "latitude": sample.latitude,
                    "longitude": sample.longitude,
                }
            )

        self.state["last_poll"] = payload

    def process_vehicle(self, status: str, vehicle: Optional[Dict[str, Any]]) -> int:
        self.restore_state()
        self.reset_daily_weekly_if_needed()

        self.external_drive_boost_until = float(
            self.state.get("external_drive_boost_until") or self.external_drive_boost_until
        )

        self.handle_morning_alert(vehicle)

        charging = self.is_charging(vehicle)

        if charging and vehicle:
            self.handle_charging_notifications(vehicle)
        else:
            self.charging_notification_stage = "idle"
            self.charging_start_timestamp = None

        if not vehicle or status in {"offline", "asleep"}:
            if self.should_boost_driving():
                interval = POLL_DRIVING_SECONDS
            else:
                interval = POLL_ASLEEP_SECONDS

            self.update_last_poll(status, vehicle, interval)
            self.save_state()
            return interval

        sample = self.sample_from_vehicle(vehicle)

        was_driving = self.drive.active
        is_driving = self.is_driving(sample)

        if is_driving:
            if not was_driving:
                self.drive.start(sample)

            self.drive.add_sample(sample)
            interval = POLL_DRIVING_SECONDS

        else:
            if was_driving:
                session = self.drive.end(sample)
                self.state["last_drive_end_soc"] = sample.battery_level
                self.update_daily_weekly_after_drive(session)

                if session.get("distance_km", 0) > 0:
                    self.telegram.send(
                        "주행 종료\n"
                        f"- 거리: {session.get('distance_km', 0):.2f} km\n"
                        f"- 시간: {float(session.get('time_seconds') or 0) / 60:.0f}분\n"
                        f"- 평균속도: {session.get('avg_speed_kmh', 0):.1f} km/h\n"
                        f"- 배터리: {session.get('start_soc') or '-'}% -> {session.get('end_soc') or '-'}%"
                    )

            if charging:
                interval = POLL_CHARGING_SECONDS
            elif self.should_boost_driving():
                interval = POLL_DRIVING_SECONDS
            else:
                interval = POLL_ONLINE_SECONDS

        self.update_last_poll(status, vehicle, interval, sample)
        self.save_state()
        return interval

    def run_once(self) -> int:
        try:
            status, vehicle = self.client.fetch_once(self.vin)

            if vehicle:
                self.vehicle_id = str(vehicle.get("id_s") or vehicle.get("id") or "")
                self.vehicle_name = str(vehicle.get("display_name") or "두삼이")

            interval = self.process_vehicle(status, vehicle)

            print(
                f"{now_kst().isoformat()} status={status} "
                f"boost={self.should_boost_driving()} next={interval}s",
                flush=True,
            )

            return interval

        except Exception as exc:
            print(f"{now_kst().isoformat()} error={exc}", file=sys.stderr, flush=True)
            self.update_last_poll("error", None, POLL_ERROR_SECONDS)
            self.save_state()
            return POLL_ERROR_SECONDS

    def request_stop(self, *_: Any) -> None:
        self.stop_requested = True

    def sleep_with_command_check(self, interval: int) -> None:
        slept = 0.0

        while slept < interval and not self.stop_requested:
            command = self.read_command()

            if command:
                result = self.apply_command(command)
                print(f"command applied: {result} {command}", flush=True)

                if result == "poll_now":
                    break

            step = min(1.0, interval - slept)
            time.sleep(step)
            slept += step

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        APP_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self.telegram.send("LIGHT LOGGG Tesla polling 시작")

        while not self.stop_requested:
            interval = self.run_once()
            self.sleep_with_command_check(interval)

        self.save_state()
        self.telegram.send("LIGHT LOGGG Tesla polling 종료")


# =========================
# CLI
# =========================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIGHT LOGGG Tesla Fleet API polling handler")
    parser.add_argument("--once", action="store_true", help="fetch and process once, then exit")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE), help="Tesla token JSON path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="LIGHT LOGGG state JSON path")
    parser.add_argument("--command-file", default=str(DEFAULT_COMMAND_FILE), help="LIGHT LOGGG command JSON path")
    parser.add_argument("--config-file", default=str(DEFAULT_PUBLIC_CONFIG_FILE), help="public config JSON path")
    parser.add_argument("--api-base", default=None, help="Tesla Fleet API base URL")
    parser.add_argument("--vin", default=None, help="target VIN when multiple vehicles exist")
    parser.add_argument("--no-env", action="store_true", help="do not load .env files")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    if not args.no_env:
        load_dotenv(Path(".env"))
        load_dotenv(Path.home() / ".light_loggg.env")

    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    config_file = Path(args.config_file).expanduser()
    init_runtime_config(config_file)

    token_file = Path(args.token_file).expanduser()
    state_file = Path(args.state_file).expanduser()
    command_file = Path(args.command_file).expanduser()

    api_base = args.api_base or os.getenv("TESLA_API_BASE", DEFAULT_API_BASE)
    target_vin = args.vin or os.getenv("TESLA_VIN")

    client = TeslaFleetClient(token_file, state_file, api_base)
    telegram = TelegramClient()

    poller = LightLogggPoller(
        client=client,
        telegram=telegram,
        state_file=state_file,
        command_file=command_file,
        vin=target_vin,
    )

    if args.once:
        poller.run_once()
    else:
        poller.run_forever()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
