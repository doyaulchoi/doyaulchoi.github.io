#!/usr/bin/env python3
"""
LIGHT LOGGG Tesla Fleet API polling handler.

This is a lightweight Termux-friendly logger inspired by TeslaMate's proven
polling pattern. It avoids repeated vehicle_data calls while the vehicle is
asleep/offline, refreshes Tesla OAuth tokens, and sends Telegram alerts for
low recent driving efficiency plus daily/weekly summaries.
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
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

KST = timezone(timedelta(hours=9))
DEFAULT_TOKEN_FILE = Path.home() / ".light_loggg_tesla_tokens.json"
DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
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
VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION = ";".join(endpoint for endpoint in VEHICLE_DATA_ENDPOINT_LIST if endpoint != "location_data")
DEFAULT_TESLA_SCOPE = "openid offline_access user_data vehicle_device_data vehicle_location"

POLL_ASLEEP_SECONDS = int(os.getenv("LIGHT_LOGGG_POLL_ASLEEP_SECONDS", "300"))
POLL_ONLINE_SECONDS = int(os.getenv("LIGHT_LOGGG_POLL_ONLINE_SECONDS", "60"))
POLL_DRIVING_SECONDS = int(os.getenv("LIGHT_LOGGG_POLL_DRIVING_SECONDS", "10"))
POLL_CHARGING_SECONDS = int(os.getenv("LIGHT_LOGGG_POLL_CHARGING_SECONDS", "60"))
POLL_ERROR_SECONDS = int(os.getenv("LIGHT_LOGGG_POLL_ERROR_SECONDS", "60"))
WINDOW_SIZE_MINUTES = float(os.getenv("LIGHT_LOGGG_WINDOW_MINUTES", "3"))
THRESHOLD_EFFICIENCY = float(os.getenv("LIGHT_LOGGG_THRESHOLD_KM_PER_KWH", "4.5"))
LOW_EFFICIENCY_ALERT_COOLDOWN = int(os.getenv("LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS", "60"))
HOME_RADIUS_KM = float(os.getenv("LIGHT_LOGGG_HOME_RADIUS_KM", "0.25"))
REQUEST_TIMEOUT = int(os.getenv("LIGHT_LOGGG_REQUEST_TIMEOUT", "25"))


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small .env file without requiring python-dotenv."""
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


def now_kst() -> datetime:
    return datetime.now(KST)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_suffix(path.suffix + f".broken.{int(time.time())}")
        path.replace(backup)
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


def ts_ms_to_dt(ts: Any) -> datetime:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc).astimezone(KST)
    return now_kst()


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


@dataclass
class Sample:
    time: datetime
    speed_kmh: Optional[float]
    power_kw: Optional[float]
    odometer_km: Optional[float]
    battery_level: Optional[float]
    latitude: Optional[float]
    longitude: Optional[float]
    shift_state: Optional[str]


@dataclass
class DriveSession:
    active: bool = False
    start_time: Optional[datetime] = None
    start_odometer_km: Optional[float] = None
    start_soc: Optional[float] = None
    speeds: List[float] = field(default_factory=list)
    efficiencies: List[float] = field(default_factory=list)
    last_speed_kmh: Optional[float] = None
    accel_count: int = 0
    decel_count: int = 0


class TelegramClient:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            print(f"[telegram disabled]\n{text}\n", flush=True)
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            res = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if res.status_code >= 400:
                print(f"Telegram error {res.status_code}: {res.text[:200]}", file=sys.stderr, flush=True)
                return False
            return True
        except requests.RequestException as exc:
            print(f"Telegram request failed: {exc}", file=sys.stderr, flush=True)
            return False


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
            raise RuntimeError("Tesla token refresh 응답에 refresh_token이 없어 토큰 회전을 저장할 수 없습니다.")
        token_payload = {"refresh_token": self.refresh_token, "saved_at": saved_at}
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
            raise RuntimeError(f"Tesla refresh_token이 없습니다. {self.token_file} 파일을 확인해야 합니다.")
        if self.refresh_token in {"여기에 Tesla refresh_token 입력", "YOUR_REFRESH_TOKEN", ""} or len(str(self.refresh_token)) < 40:
            raise RuntimeError(
                f"Tesla refresh_token이 아직 실제 값으로 입력되지 않았거나 형식이 너무 짧습니다. "
                f"{self.token_file} 파일의 refresh_token 값을 실제 Tesla 사용자 refresh token으로 교체해야 합니다."
            )
        data = {
            "grant_type": "refresh_token",
            "scope": self.scope,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        res = self.session.post(
            AUTH_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )
        if res.status_code != 200:
            extra = ""
            try:
                err = res.json()
                if err.get("error_description") == "The refresh_token is invalid":
                    extra = " 입력된 refresh_token이 만료됐거나 실제 사용자 refresh token이 아닙니다. 새로 발급해 교체해야 합니다."
            except Exception:
                pass
            raise RuntimeError(f"Tesla token refresh failed: HTTP {res.status_code} {res.text[:300]}{extra}")
        self.save_tokens(res.json())

    def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, retry: bool = True) -> Dict[str, Any]:
        if not self.access_token_valid():
            self.refresh()
        url = f"{self.api_base}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": "LIGHT-LOGGG/teslamate-style-poller"}
        res = self.session.request(method, url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if res.status_code == 401 and retry:
            self.refresh()
            return self.request(method, path, params=params, retry=False)
        if res.status_code == 421:
            try:
                body = res.json()
                msg = body.get("error") or body.get("response") or res.text
            except Exception:
                msg = res.text
            raise RuntimeError(f"Tesla region mismatch: {msg}")
        if res.status_code == 429:
            retry_after = res.headers.get("retry-after", "300")
            raise RuntimeError(f"Tesla rate limited: retry-after {retry_after}s")
        if res.status_code >= 400:
            raise RuntimeError(f"Tesla API HTTP {res.status_code}: {res.text[:500]}")
        return res.json()

    def products(self) -> List[Dict[str, Any]]:
        body = self.request("GET", "/api/1/products")
        response = body.get("response", [])
        return [item for item in response if isinstance(item, dict) and item.get("vehicle_id")]

    def vehicle_basic(self, vehicle_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/api/1/vehicles/{vehicle_id}").get("response", {})

    def vehicle_data(self, vehicle_id: str) -> Dict[str, Any]:
        try:
            return self.request(
                "GET",
                f"/api/1/vehicles/{vehicle_id}/vehicle_data",
                params={"endpoints": VEHICLE_DATA_ENDPOINTS},
            ).get("response", {})
        except RuntimeError as exc:
            message = str(exc)
            if "HTTP 403" in message and "vehicle_location" in message:
                print(
                    "Tesla token lacks vehicle_location scope; retrying vehicle_data without location_data. "
                    "Re-run light_loggg_tesla_oauth.py to grant location access.",
                    file=sys.stderr,
                    flush=True,
                )
                return self.request(
                    "GET",
                    f"/api/1/vehicles/{vehicle_id}/vehicle_data",
                    params={"endpoints": VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION},
                ).get("response", {})
            raise


class LightLogggPoller:
    def __init__(self, client: TeslaFleetClient, telegram: TelegramClient, state_file: Path, vin: Optional[str] = None) -> None:
        self.client = client
        self.telegram = telegram
        self.state_file = state_file
        self.target_vin = vin or os.getenv("TESLA_VIN")
        self.vehicle_id: Optional[str] = os.getenv("TESLA_VEHICLE_ID")
        self.vehicle_name = "Tesla"
        self.samples: deque[Sample] = deque()
        self.drive = DriveSession()
        self.last_alert_at = 0.0
        self.last_summary_date: Optional[str] = None
        self.last_weekly_summary_iso: Optional[str] = None
        self.state = load_json(state_file, self.default_state())
        self.restore_state()
        self.stop_requested = False

    @staticmethod
    def default_state() -> Dict[str, Any]:
        return {
            "daily": {},
            "weekly": {},
            "last_summary_date": None,
            "last_weekly_summary_iso": None,
        }

    def restore_state(self) -> None:
        today = now_kst().date().isoformat()
        week = now_kst().date().isocalendar()
        week_key = f"{week.year}-W{week.week:02d}"
        if self.state.get("daily", {}).get("date") != today:
            self.state["daily"] = self.new_daily(today)
        if self.state.get("weekly", {}).get("week") != week_key:
            self.state["weekly"] = self.new_weekly(week_key)
        self.last_summary_date = self.state.get("last_summary_date")
        self.last_weekly_summary_iso = self.state.get("last_weekly_summary_iso")

    @staticmethod
    def new_daily(day: str) -> Dict[str, Any]:
        return {
            "date": day,
            "total_distance_km": 0.0,
            "total_time_seconds": 0.0,
            "start_odometer_km": None,
            "end_odometer_km": None,
            "start_soc": None,
            "end_soc": None,
            "speed_samples": [],
            "efficiencies": [],
            "drive_sessions": [],
            "charging_sessions": [],
            "accel_count": 0,
            "decel_count": 0,
            "last_location": None,
            "home_arrival_summary_sent": False,
        }

    @staticmethod
    def new_weekly(week_key: str) -> Dict[str, Any]:
        return {
            "week": week_key,
            "total_distance_km": 0.0,
            "total_time_seconds": 0.0,
            "total_energy_kwh": 0.0,
            "drive_count": 0,
            "days": {},
        }

    def save_state(self) -> None:
        self.state["last_summary_date"] = self.last_summary_date
        self.state["last_weekly_summary_iso"] = self.last_weekly_summary_iso
        current = load_json(self.state_file, {})
        for key in ("access_token", "access_token_expires_at", "last_token_refresh_at", "token_saved_at"):
            if key in current:
                self.state[key] = current[key]
        atomic_write_json(self.state_file, self.state)

    def identify_vehicle(self) -> None:
        if self.vehicle_id:
            return
        vehicles = self.client.products()
        if not vehicles:
            raise RuntimeError("Tesla products 응답에서 차량을 찾지 못했습니다.")
        chosen = None
        if self.target_vin:
            chosen = next((v for v in vehicles if v.get("vin") == self.target_vin), None)
        if chosen is None:
            chosen = vehicles[0]
        self.vehicle_id = str(chosen["id"])
        self.vehicle_name = chosen.get("display_name") or chosen.get("vin") or "Tesla"
        print(f"Using vehicle id={self.vehicle_id} name={self.vehicle_name}", flush=True)

    def fetch_once(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        self.identify_vehicle()
        assert self.vehicle_id is not None
        basic = self.client.vehicle_basic(self.vehicle_id)
        vehicle_state = basic.get("state", "unknown")
        self.vehicle_name = basic.get("display_name") or self.vehicle_name
        if vehicle_state in ("offline", "asleep"):
            return vehicle_state, basic
        data = self.client.vehicle_data(self.vehicle_id)
        return data.get("state") or vehicle_state or "online", data

    def sample_from_vehicle(self, vehicle: Dict[str, Any]) -> Sample:
        drive_state = vehicle.get("drive_state") or {}
        charge_state = vehicle.get("charge_state") or {}
        vehicle_state = vehicle.get("vehicle_state") or {}
        odometer_miles = as_float(vehicle_state.get("odometer"))
        odometer_km = odometer_miles * 1.609344 if odometer_miles is not None else None
        speed_kmh = speed_mph_to_kmh(as_float(drive_state.get("speed")))
        return Sample(
            time=ts_ms_to_dt(drive_state.get("timestamp") or charge_state.get("timestamp") or vehicle_state.get("timestamp")),
            speed_kmh=speed_kmh,
            power_kw=as_float(drive_state.get("power")),
            odometer_km=odometer_km,
            battery_level=as_float(charge_state.get("battery_level")),
            latitude=as_float(drive_state.get("latitude") or drive_state.get("native_latitude")),
            longitude=as_float(drive_state.get("longitude") or drive_state.get("native_longitude")),
            shift_state=drive_state.get("shift_state"),
        )

    def is_driving(self, sample: Sample) -> bool:
        return sample.shift_state in {"D", "N", "R"} or (sample.speed_kmh is not None and sample.speed_kmh > 1.0)

    def is_charging(self, vehicle: Dict[str, Any]) -> bool:
        charging_state = ((vehicle.get("charge_state") or {}).get("charging_state") or "").lower()
        return charging_state in {"starting", "charging"}

    def update_window(self, sample: Sample) -> None:
        self.samples.append(sample)
        cutoff = sample.time - timedelta(minutes=WINDOW_SIZE_MINUTES)
        while self.samples and self.samples[0].time < cutoff:
            self.samples.popleft()

    def recent_efficiency(self) -> Optional[float]:
        if len(self.samples) < 2:
            return None
        first, last = self.samples[0], self.samples[-1]
        distance_km = None
        if first.odometer_km is not None and last.odometer_km is not None and last.odometer_km >= first.odometer_km:
            distance_km = last.odometer_km - first.odometer_km
        if not distance_km or distance_km <= 0:
            dist = 0.0
            prev = first
            for cur in list(self.samples)[1:]:
                gps_dist = haversine_km(prev.latitude, prev.longitude, cur.latitude, cur.longitude)
                if gps_dist is not None and gps_dist < 2.0:
                    dist += gps_dist
                prev = cur
            distance_km = dist
        if distance_km <= 0.05:
            return None
        energy_kwh = 0.0
        samples = list(self.samples)
        for prev, cur in zip(samples, samples[1:]):
            power = cur.power_kw if cur.power_kw is not None else prev.power_kw
            if power is None or power <= 0:
                continue
            seconds = max(0.0, (cur.time - prev.time).total_seconds())
            energy_kwh += power * seconds / 3600.0
        if energy_kwh <= 0.02:
            return None
        return distance_km / energy_kwh

    def detect_accel(self, sample: Sample) -> None:
        if sample.speed_kmh is None or self.drive.last_speed_kmh is None:
            self.drive.last_speed_kmh = sample.speed_kmh
            return
        delta = sample.speed_kmh - self.drive.last_speed_kmh
        if delta >= 12:
            self.drive.accel_count += 1
            self.state["daily"]["accel_count"] += 1
        elif delta <= -15:
            self.drive.decel_count += 1
            self.state["daily"]["decel_count"] += 1
        self.drive.last_speed_kmh = sample.speed_kmh

    def start_drive_if_needed(self, sample: Sample) -> None:
        if self.drive.active:
            return
        self.drive = DriveSession(
            active=True,
            start_time=sample.time,
            start_odometer_km=sample.odometer_km,
            start_soc=sample.battery_level,
        )
        daily = self.state["daily"]
        if daily.get("start_odometer_km") is None:
            daily["start_odometer_km"] = sample.odometer_km
        if daily.get("start_soc") is None:
            daily["start_soc"] = sample.battery_level
        self.telegram.send(f"주행 시작\n차량 {self.vehicle_name}\n시각 {sample.time.strftime('%H:%M')}")

    def finish_drive_if_needed(self, sample: Sample) -> None:
        if not self.drive.active:
            return
        duration = max(0.0, (sample.time - (self.drive.start_time or sample.time)).total_seconds())
        distance = 0.0
        if self.drive.start_odometer_km is not None and sample.odometer_km is not None:
            distance = max(0.0, sample.odometer_km - self.drive.start_odometer_km)
        soc_change = None
        if self.drive.start_soc is not None and sample.battery_level is not None:
            soc_change = self.drive.start_soc - sample.battery_level
        avg_speed = sum(self.drive.speeds) / len(self.drive.speeds) if self.drive.speeds else 0.0
        avg_eff = sum(self.drive.efficiencies) / len(self.drive.efficiencies) if self.drive.efficiencies else None
        daily = self.state["daily"]
        daily["total_distance_km"] += distance
        daily["total_time_seconds"] += duration
        daily["end_odometer_km"] = sample.odometer_km
        daily["end_soc"] = sample.battery_level
        session = {
            "start": self.drive.start_time.isoformat() if self.drive.start_time else None,
            "end": sample.time.isoformat(),
            "distance_km": round(distance, 3),
            "duration_seconds": round(duration, 1),
            "soc_change": soc_change,
            "avg_speed_kmh": round(avg_speed, 1),
            "avg_efficiency_km_per_kwh": round(avg_eff, 2) if avg_eff else None,
            "accel_count": self.drive.accel_count,
            "decel_count": self.drive.decel_count,
        }
        daily["drive_sessions"].append(session)
        self.rollup_weekly(session)
        self.drive = DriveSession()
        self.telegram.send(self.format_drive_end(session))
        self.maybe_home_arrival(sample)

    def rollup_weekly(self, session: Dict[str, Any]) -> None:
        weekly = self.state["weekly"]
        distance = float(session.get("distance_km") or 0)
        duration = float(session.get("duration_seconds") or 0)
        eff = session.get("avg_efficiency_km_per_kwh")
        energy = distance / eff if eff and eff > 0 else 0.0
        weekly["total_distance_km"] += distance
        weekly["total_time_seconds"] += duration
        weekly["total_energy_kwh"] += energy
        weekly["drive_count"] += 1
        day_key = now_kst().date().isoformat()
        day = weekly["days"].setdefault(day_key, {"distance_km": 0.0, "drive_count": 0})
        day["distance_km"] += distance
        day["drive_count"] += 1

    def handle_driving_sample(self, sample: Sample) -> None:
        self.start_drive_if_needed(sample)
        if sample.speed_kmh is not None:
            self.drive.speeds.append(sample.speed_kmh)
            self.state["daily"]["speed_samples"].append(round(sample.speed_kmh, 2))
            if len(self.state["daily"]["speed_samples"]) > 3000:
                self.state["daily"]["speed_samples"] = self.state["daily"]["speed_samples"][-3000:]
        self.detect_accel(sample)
        self.update_window(sample)
        eff = self.recent_efficiency()
        if eff:
            eff = round(eff, 2)
            self.drive.efficiencies.append(eff)
            self.state["daily"]["efficiencies"].append(eff)
            if eff < THRESHOLD_EFFICIENCY and time.time() - self.last_alert_at >= LOW_EFFICIENCY_ALERT_COOLDOWN:
                self.telegram.send(f"전비 경고\n최근 {WINDOW_SIZE_MINUTES:g}분 평균 {eff:.2f} km/kWh\n기준 {THRESHOLD_EFFICIENCY:.2f} km/kWh")
                self.last_alert_at = time.time()
        if sample.latitude is not None and sample.longitude is not None:
            self.state["daily"]["last_location"] = {"lat": sample.latitude, "lon": sample.longitude}

    def process_vehicle(self, status: str, vehicle: Optional[Dict[str, Any]]) -> int:
        self.restore_state()
        if not vehicle or status in {"offline", "asleep"}:
            if self.drive.active:
                synthetic = Sample(now_kst(), None, None, None, None, None, None, None)
                self.finish_drive_if_needed(synthetic)
            self.maybe_scheduled_summary()
            self.save_state()
            return POLL_ASLEEP_SECONDS
        sample = self.sample_from_vehicle(vehicle)
        if self.is_driving(sample):
            self.handle_driving_sample(sample)
            self.maybe_scheduled_summary()
            self.save_state()
            return POLL_DRIVING_SECONDS
        self.finish_drive_if_needed(sample)
        if self.is_charging(vehicle):
            self.maybe_scheduled_summary()
            self.save_state()
            return POLL_CHARGING_SECONDS
        self.maybe_scheduled_summary()
        self.save_state()
        return POLL_ONLINE_SECONDS

    def maybe_home_arrival(self, sample: Sample) -> None:
        daily = self.state["daily"]
        if daily.get("home_arrival_summary_sent") or now_kst().hour < 18:
            return
        home_lat = as_float(os.getenv("HOME_LAT"))
        home_lon = as_float(os.getenv("HOME_LON"))
        dist = haversine_km(sample.latitude, sample.longitude, home_lat, home_lon)
        if dist is not None and dist <= HOME_RADIUS_KM:
            daily["home_arrival_summary_sent"] = True
            self.telegram.send("집 도착 감지\n" + self.format_daily_summary())

    def maybe_scheduled_summary(self) -> None:
        current = now_kst()
        today_key = current.date().isoformat()
        if current.hour >= 21 and self.last_summary_date != today_key:
            self.telegram.send(self.format_daily_summary())
            self.last_summary_date = today_key
        if current.weekday() == 6 and current.hour >= 21 and self.last_weekly_summary_iso != today_key:
            self.telegram.send(self.format_weekly_summary())
            self.last_weekly_summary_iso = today_key

    def format_drive_end(self, session: Dict[str, Any]) -> str:
        minutes = float(session.get("duration_seconds") or 0) / 60
        eff = session.get("avg_efficiency_km_per_kwh")
        eff_text = f"{eff:.2f} km/kWh" if isinstance(eff, (int, float)) else "계산 부족"
        soc = session.get("soc_change")
        soc_text = f"{soc:.1f}%p" if isinstance(soc, (int, float)) else "확인 불가"
        return (
            "주행 종료\n"
            f"거리 {float(session.get('distance_km') or 0):.2f} km\n"
            f"시간 {minutes:.0f}분\n"
            f"평균속도 {float(session.get('avg_speed_kmh') or 0):.1f} km/h\n"
            f"평균전비 {eff_text}\n"
            f"배터리 변화 {soc_text}"
        )

    def format_daily_summary(self) -> str:
        daily = self.state["daily"]
        distance = float(daily.get("total_distance_km") or 0)
        seconds = float(daily.get("total_time_seconds") or 0)
        avg_speed = distance / (seconds / 3600) if seconds > 0 else 0
        effs = daily.get("efficiencies") or []
        avg_eff = sum(effs) / len(effs) if effs else 0
        start_soc = daily.get("start_soc")
        end_soc = daily.get("end_soc")
        if isinstance(start_soc, (int, float)) and isinstance(end_soc, (int, float)):
            soc_text = f"{start_soc:.0f}% → {end_soc:.0f}% ({start_soc - end_soc:.0f}%p 사용)"
        else:
            soc_text = "확인 부족"
        return (
            f"오늘의 주행 요약 {daily.get('date')}\n"
            f"주행거리 {distance:.2f} km\n"
            f"주행시간 {seconds / 60:.0f}분\n"
            f"평균속도 {avg_speed:.1f} km/h\n"
            f"평균전비 {avg_eff:.2f} km/kWh\n"
            f"배터리 {soc_text}\n"
            f"주행횟수 {len(daily.get('drive_sessions') or [])}회\n"
            f"급가속 {int(daily.get('accel_count') or 0)}회, 급감속 {int(daily.get('decel_count') or 0)}회"
        )

    def format_weekly_summary(self) -> str:
        weekly = self.state["weekly"]
        distance = float(weekly.get("total_distance_km") or 0)
        seconds = float(weekly.get("total_time_seconds") or 0)
        energy = float(weekly.get("total_energy_kwh") or 0)
        avg_eff = distance / energy if energy > 0 else 0
        return (
            f"주간 주행 요약 {weekly.get('week')}\n"
            f"누적거리 {distance:.2f} km\n"
            f"누적시간 {seconds / 60:.0f}분\n"
            f"평균전비 {avg_eff:.2f} km/kWh\n"
            f"주행횟수 {int(weekly.get('drive_count') or 0)}회"
        )

    def run_once(self) -> int:
        try:
            status, vehicle = self.fetch_once()
            interval = self.process_vehicle(status, vehicle)
            print(f"{now_kst().isoformat()} status={status} next={interval}s", flush=True)
            return interval
        except Exception as exc:
            print(f"{now_kst().isoformat()} error={exc}", file=sys.stderr, flush=True)
            self.save_state()
            return POLL_ERROR_SECONDS

    def request_stop(self, *_: Any) -> None:
        self.stop_requested = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.telegram.send("LIGHT LOGGG Tesla 폴링 시작")
        while not self.stop_requested:
            interval = self.run_once()
            slept = 0
            while slept < interval and not self.stop_requested:
                time.sleep(min(1, interval - slept))
                slept += 1
        self.save_state()
        self.telegram.send("LIGHT LOGGG Tesla 폴링 종료")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIGHT LOGGG Tesla Fleet API polling handler")
    parser.add_argument("--once", action="store_true", help="fetch and process once, then exit")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE), help="Tesla token JSON path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="LIGHT LOGGG state JSON path")
    parser.add_argument("--api-base", default=None, help="Tesla Fleet API base URL")
    parser.add_argument("--vin", default=None, help="target VIN when multiple vehicles exist")
    parser.add_argument("--no-env", action="store_true", help="do not load .env")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if not args.no_env:
        load_dotenv(Path(".env"))
        load_dotenv(Path.home() / ".light_loggg.env")
    api_base = args.api_base or os.getenv("TESLA_API_BASE", DEFAULT_API_BASE)
    client = TeslaFleetClient(Path(args.token_file).expanduser(), Path(args.state_file).expanduser(), api_base)
    telegram = TelegramClient()
    target_vin = args.vin or os.getenv("TESLA_VIN")
    poller = LightLogggPoller(client, telegram, Path(args.state_file).expanduser(), target_vin)
    if args.once:
        poller.run_once()
    else:
        poller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
