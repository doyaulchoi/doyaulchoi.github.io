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
LONG_DRIVE_ALERT_SECONDS = 3600  # 1 hour
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


def parse_dt(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


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
    shift_state: Optional[str] = None


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

    def start(self, sample: Sample) -> None:
        self.active = True
        self.start_time = sample.time
        self.start_odometer_km = sample.odometer_km
        self.start_soc = sample.battery_level

    def add_sample(self, sample: Sample) -> None:
        if sample.speed_kmh is not None:
            self.speeds.append(sample.speed_kmh)
        self.last_speed_kmh = sample.speed_kmh

    def end(self, sample: Sample) -> None:
        self.active = False

    def summary(self) -> str:
        return "주행 종료"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "start_odometer_km": self.start_odometer_km,
            "start_soc": self.start_soc,
            "accel_count": self.accel_count,
            "decel_count": self.decel_count,
        }


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
            raise RuntimeError(f"Tesla token refresh failed: HTTP {res.status_code} {res.text}")
        self.save_tokens(res.json())

    def fetch_once(self, vin: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
        if not self.access_token_valid():
            self.refresh()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        url = f"{self.api_base}/api/1/vehicles"
        res = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code == 401:
            self.refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            res = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            raise RuntimeError(f"Tesla vehicles API failed: HTTP {res.status_code}")
        
        vehicles = res.json().get("response") or []
        if not vehicles:
            return "offline", None
        
        target = None
        if vin:
            target = next((v for v in vehicles if v.get("vin") == vin), None)
        else:
            target = vehicles[0]
        
        if not target:
            return "offline", None
        
        status = target.get("state") or "offline"
        if status != "online":
            return status, None
        
        v_id = target.get("id_s") or target.get("id")
        data_url = f"{self.api_base}/api/1/vehicles/{v_id}/vehicle_data?endpoints={VEHICLE_DATA_ENDPOINTS}"
        res = self.session.get(data_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code == 403 and "vehicle_location" in res.text:
            print("Tesla token lacks vehicle_location scope; retrying vehicle_data without location_data.")
            data_url = f"{self.api_base}/api/1/vehicles/{v_id}/vehicle_data?endpoints={VEHICLE_DATA_ENDPOINTS_WITHOUT_LOCATION}"
            res = self.session.get(data_url, headers=headers, timeout=REQUEST_TIMEOUT)
            
        if res.status_code != 200:
            return status, None
        
        return status, res.json().get("response")


class LightLogggPoller:
    def __init__(self, client: TeslaFleetClient, telegram: TelegramClient, state_file: Path, vin: Optional[str] = None) -> None:
        self.client = client
        self.telegram = telegram
        self.state_file = state_file
        self.vin = vin
        self.vehicle_id: str = ""
        self.vehicle_name: str = "두삼이"
        self.state: Dict[str, Any] = {
            "daily": {"date": date.today().isoformat(), "total_distance_km": 0.0, "total_time_seconds": 0.0, "drive_sessions": [], "efficiencies": [], "speed_samples": [], "accel_count": 0, "decel_count": 0},
            "weekly": {"week": now_kst().strftime("%Y-W%U"), "total_distance_km": 0.0, "total_time_seconds": 0.0, "total_energy_kwh": 0.0, "drive_count": 0, "days": {}},
            "last_summary_date": None,
            "last_weekly_summary_iso": None,
            "last_morning_alert_date": None,
            "last_drive_end_soc": None,
            "charging_stats": {"total_added_soc": 0.0, "powers": []}
        }
        self.restore_state()
        self.drive = DriveSession(active=self.state.get("drive", {}).get("active", False))
        self.last_alert_at = 0.0
        self.stop_requested = False
        self.window: deque[Sample] = deque()
        self.last_summary_date = self.state.get("last_summary_date")
        self.last_weekly_summary_iso = self.state.get("last_weekly_summary_iso")
        # Charging notification states
        self.charging_notification_stage: str = self.state.get("charging_notification_stage", "idle")
        self.charging_start_timestamp: Optional[datetime] = parse_dt(self.state.get("charging_start_timestamp"))

    def restore_state(self) -> None:
        if self.state_file.exists():
            try:
                loaded_state = json.loads(self.state_file.read_text(encoding="utf-8"))
                for key, value in loaded_state.items():
                    if key == "tokens" and isinstance(self.state.get(key), dict) and isinstance(value, dict):
                        self.state[key].update(value)
                    else:
                        self.state[key] = value
            except Exception as exc:
                print(f"상태 파일 로드 실패: {exc}", file=sys.stderr)
        self.charging_notification_stage = self.state.get("charging_notification_stage", "idle")
        self.charging_start_timestamp = parse_dt(self.state.get("charging_start_timestamp"))

    def save_state(self) -> None:
        try:
            self.state["charging_notification_stage"] = self.charging_notification_stage
            self.state["charging_start_timestamp"] = self.charging_start_timestamp.isoformat() if self.charging_start_timestamp else None
            self.state["last_summary_date"] = self.last_summary_date
            self.state["last_weekly_summary_iso"] = self.last_weekly_summary_iso
            self.state["drive"] = self.drive.to_dict()
            atomic_write_json(self.state_file, self.state)
        except Exception as exc:
            print(f"상태 파일 저장 실패: {exc}", file=sys.stderr)

    def is_driving(self, sample: Sample) -> bool:
        return (sample.speed_kmh or 0) > 2.0 or sample.shift_state in {"D", "R"}

    def is_charging(self, vehicle: Optional[Dict[str, Any]]) -> bool:
        if not vehicle: return False
        cs = vehicle.get("charge_state") or {}
        return cs.get("charging_state") == "Charging"

    def sample_from_vehicle(self, vehicle: Dict[str, Any]) -> Sample:
        ds = vehicle.get("drive_state") or {}
        cs = vehicle.get("charge_state") or {}
        vs = vehicle.get("vehicle_state") or {}
        return Sample(
            time=now_kst(),
            speed_kmh=speed_mph_to_kmh(as_float(ds.get("speed"))),
            power_kw=as_float(cs.get("charger_power")),
            odometer_km=as_float(vs.get("odometer")) * 1.609344 if vs.get("odometer") else None,
            battery_level=as_float(cs.get("battery_level")),
            latitude=as_float(ds.get("latitude")),
            longitude=as_float(ds.get("longitude")),
            shift_state=ds.get("shift_state")
        )

    def handle_morning_alert(self, vehicle: Optional[Dict[str, Any]]) -> None:
        now = now_kst()
        today_str = now.date().isoformat()
        
        # Check if it's 6:30 AM KST
        if now.hour == 6 and now.minute >= 30 and self.state.get("last_morning_alert_date") != today_str:
            if not vehicle:
                # If vehicle is offline, we can't get current SOC, but we can still try to send something if we have last poll
                self.telegram.send("☀️ 굿모닝! 차량이 오프라인이라 현재 상태를 확인할 수 없습니다.")
                self.state["last_morning_alert_date"] = today_str
                return

            cs = vehicle.get("charge_state") or {}
            current_soc = as_float(cs.get("battery_level"))
            last_drive_soc = self.state.get("last_drive_end_soc")
            
            msg = "☀️ 굿모닝! 오늘의 차량 상태입니다.\n"
            
            if last_drive_soc is not None and current_soc is not None:
                if current_soc > last_drive_soc:
                    # Charged
                    added_soc = current_soc - last_drive_soc
                    stats = self.state.get("charging_stats") or {}
                    powers = stats.get("powers") or []
                    avg_power = sum(powers) / len(powers) if powers else 0
                    msg += (
                        f"🔋 배터리: {current_soc:.0f}% (충전됨)\n"
                        f"📈 충전량: +{added_soc:.0f}%p\n"
                    )
                    if avg_power > 0:
                        msg += f"⚡️ 평균 충전 속도: {avg_power:.1f} kW"
                else:
                    # Not charged or decreased
                    msg += f"🔋 배터리: {current_soc:.0f}%"
            elif current_soc is not None:
                msg += f"🔋 배터리: {current_soc:.0f}%"
            
            self.telegram.send(msg)
            self.state["last_morning_alert_date"] = today_str
            # Reset charging stats for the next day
            self.state["charging_stats"] = {"total_added_soc": 0.0, "powers": []}

    def handle_charging_notifications(self, vehicle: Dict[str, Any]) -> None:
        cs = vehicle.get("charge_state") or {}
        battery_level = as_float(cs.get("battery_level"))
        charger_power = as_float(cs.get("charger_power"))
        time_to_full = as_float(cs.get("time_to_full_charge"))

        # Collect stats for morning alert
        if battery_level is not None and charger_power is not None:
            stats = self.state.get("charging_stats", {"total_added_soc": 0.0, "powers": []})
            if charger_power > 0:
                stats["powers"].append(charger_power)
            self.state["charging_stats"] = stats

        if self.charging_notification_stage == "idle":
            msg = f"⚡️ 충전 시작! 현재 배터리: {battery_level:.0f}%"
            self.telegram.send(msg)
            self.charging_notification_stage = "initial_notified"
            self.charging_start_timestamp = now_kst()
            print(f"[Charging] Initial notification sent. Battery: {battery_level:.0f}%")
        elif self.charging_notification_stage == "initial_notified":
            if self.charging_start_timestamp and (now_kst() - self.charging_start_timestamp).total_seconds() >= 180:
                kw = charger_power if charger_power is not None else 0
                eta = f"{int(time_to_full * 60)}분" if time_to_full is not None else "알 수 없음"
                msg = (
                    f"⚡️ 충전 중... (3분 경과)\n"
                    f"현재 배터리: {battery_level:.0f}%\n"
                    f"충전 속도: {kw:.1f} kW\n"
                    f"완료 예상 시간: {eta}"
                )
                self.telegram.send(msg)
                self.charging_notification_stage = "detailed_notified"
                print(f"[Charging] Detailed notification sent. Speed: {kw:.1f} kW")

    def update_last_poll(self, status: str, vehicle: Optional[Dict[str, Any]], interval: int, sample: Optional[Sample] = None) -> None:
        payload = {"time": now_kst().isoformat(), "status": status, "next_seconds": interval, "vehicle_id": self.vehicle_id, "vehicle_name": self.vehicle_name}
        if vehicle:
            cs, vs, ds = vehicle.get("charge_state", {}), vehicle.get("vehicle_state", {}), vehicle.get("drive_state", {})
            payload.update({"charging_state": cs.get("charging_state"), "battery_level": cs.get("battery_level"), "shift_state": ds.get("shift_state")})
            om = as_float(vs.get("odometer"))
            if om: payload["odometer_km"] = round(om * 1.609344, 1)
        if sample:
            payload.update({"speed_kmh": round(sample.speed_kmh, 1) if sample.speed_kmh else None, "latitude": sample.latitude, "longitude": sample.longitude})
        self.state["last_poll"] = payload

    def process_vehicle(self, status: str, vehicle: Optional[Dict[str, Any]]) -> int:
        self.restore_state()
        self.handle_morning_alert(vehicle)
        
        charging = self.is_charging(vehicle)
        if charging:
            self.handle_charging_notifications(vehicle)
        else:
            self.charging_notification_stage = "idle"
            self.charging_start_timestamp = None

        if not vehicle or status in {"offline", "asleep"}:
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
                self.drive.end(sample)
                # Save SOC at the end of drive for morning comparison
                self.state["last_drive_end_soc"] = sample.battery_level
            
            if charging:
                interval = POLL_CHARGING_SECONDS
            else:
                interval = POLL_ONLINE_SECONDS
            
        self.update_last_poll(status, vehicle, interval, sample)
        self.save_state()
        return interval

    def run_once(self) -> int:
        try:
            status, vehicle = self.client.fetch_once(self.vin)
            if vehicle:
                self.vehicle_id = vehicle.get("id_s") or str(vehicle.get("id"))
                self.vehicle_name = vehicle.get("display_name") or "두삼이"
                print(f"Using vehicle id={self.vehicle_id} name={self.vehicle_name}")
            interval = self.process_vehicle(status, vehicle)
            print(f"{now_kst().isoformat()} status={status} next={interval}s", flush=True)
            return interval
        except Exception as exc:
            print(f"{now_kst().isoformat()} error={exc}", file=sys.stderr, flush=True)
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
