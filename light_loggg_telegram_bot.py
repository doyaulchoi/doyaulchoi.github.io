#!/usr/bin/env python3
"""Telegram command listener for LIGHT LOGGG Tesla polling.

This bot listens for Telegram commands such as /status, /daily, /weekly,
and /update.

Operational policy:
- GitHub is the source of truth.
- Termux updates are performed by downloading raw files from GitHub.
- Public config is stored in ~/light_loggg_tesla/light_loggg_public_config.json.
- Sensitive values must stay in ~/.light_loggg.env or local token files.
- /update downloads files, validates them, restarts polling, runs check_system.py,
  and sends the diagnostic result back to Telegram.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests


KST = timezone(timedelta(hours=9))

REQUEST_TIMEOUT = int(os.getenv("LIGHT_LOGGG_REQUEST_TIMEOUT", "25"))

RAW_BASE = os.getenv(
    "LIGHT_LOGGG_RAW_BASE",
    "https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main",
)

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"

DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
DEFAULT_PID_FILE = APP_DIR / "polling.pid"
BOT_PID_FILE = APP_DIR / "telegram_bot.pid"

PUBLIC_CONFIG_FILE = APP_DIR / "light_loggg_public_config.json"

DEFAULT_LOG_FILE = LOG_DIR / "polling.log"
BOT_LOG_FILE = LOG_DIR / "telegram_bot.log"
UPDATE_LOG_FILE = LOG_DIR / "update.log"

POLLING_SCRIPT_PATH = APP_DIR / "light_loggg_tesla_polling.py"
BOT_SCRIPT_PATH = APP_DIR / "light_loggg_telegram_bot.py"
CHECK_SCRIPT_PATH = APP_DIR / "check_system.py"

BOOT_SOURCE_FILE = APP_DIR / "start-light-loggg.sh"
BOOT_TARGET_DIR = Path.home() / ".termux" / "boot"
BOOT_TARGET_FILE = BOOT_TARGET_DIR / "start-light-loggg.sh"

COMMAND_FILE = APP_DIR / "command.json"

UPDATE_FILES = [
    "light_loggg_public_config.json",
    "light_loggg_tesla_polling.py",
    "light_loggg_telegram_bot.py",
    "light_loggg_tesla_oauth.py",
    "check_system.py",
    "start-light-loggg.sh",
    "setup_light_loggg_tesla_polling.sh",
    "setup_tesla_telemetry.sh",
    "setup_tesla_telemetry_python.sh",
    "telemetry_server.py",
    "tesla_telemetry_handler.py",
]


def load_dotenv(path: Path = Path(".env")) -> None:
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


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


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


def append_update_log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with UPDATE_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(f"[{now_kst().isoformat()}] {text}\n")


def read_pid(pid_file: Path) -> Optional[int]:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def process_alive(pid_file: Path = DEFAULT_PID_FILE) -> Optional[bool]:
    pid = read_pid(pid_file)

    if pid is None:
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


def tail_log(log_file: Path = DEFAULT_LOG_FILE, lines: int = 5) -> str:
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
        return text[-1600:] if text else "최근 로그 없음"
    except Exception:
        return "로그 확인 실패"


def split_telegram_text(text: str, limit: int = 3500) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < 500:
            cut = limit

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def format_config_status(state: Dict[str, Any]) -> list[str]:
    last = state.get("last_poll") or {}
    config = last.get("config") or {}

    if not isinstance(config, dict) or not config:
        public_config = load_json(PUBLIC_CONFIG_FILE, {})
        polling = public_config.get("polling") or {}

        if isinstance(polling, dict) and polling:
            return [
                "설정:",
                "- source: public_config 추정",
                f"- asleep: {polling.get('asleep_seconds', '-')}",
                f"- online: {polling.get('online_seconds', '-')}",
                f"- driving: {polling.get('driving_seconds', '-')}",
                f"- charging: {polling.get('charging_seconds', '-')}",
                f"- error: {polling.get('error_seconds', '-')}",
            ]

        return [
            "설정:",
            "- polling config 기록 없음",
        ]

    return [
        "설정:",
        "- source: last_poll",
        f"- asleep: {config.get('asleep_seconds', '-')}",
        f"- online: {config.get('online_seconds', '-')}",
        f"- driving: {config.get('driving_seconds', '-')}",
        f"- charging: {config.get('charging_seconds', '-')}",
        f"- error: {config.get('error_seconds', '-')}",
    ]


def format_daily_summary(state: Dict[str, Any]) -> str:
    daily = state.get("daily") or {}

    distance = float(daily.get("total_distance_km") or 0)
    seconds = float(daily.get("total_time_seconds") or 0)
    energy = float(daily.get("total_energy_kwh") or 0)

    avg_speed = distance / (seconds / 3600) if seconds > 0 else 0

    effs = daily.get("efficiencies") or []
    avg_eff_from_list = sum(effs) / len(effs) if effs else 0
    avg_eff_from_energy = distance / energy if energy > 0 else 0
    avg_eff = avg_eff_from_energy or avg_eff_from_list

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
        f"급가속 {int(daily.get('accel_count') or 0)}회, "
        f"급감속 {int(daily.get('decel_count') or 0)}회"
    )


def format_weekly_summary(state: Dict[str, Any]) -> str:
    weekly = state.get("weekly") or {}

    distance = float(weekly.get("total_distance_km") or 0)
    seconds = float(weekly.get("total_time_seconds") or 0)
    energy = float(weekly.get("total_energy_kwh") or 0)

    avg_speed = distance / (seconds / 3600) if seconds > 0 else 0
    avg_eff = distance / energy if energy > 0 else 0

    return (
        f"주간 주행 요약 {weekly.get('week') or '-'}\n"
        f"누적거리 {distance:.2f} km\n"
        f"누적시간 {seconds / 60:.0f}분\n"
        f"평균속도 {avg_speed:.1f} km/h\n"
        f"평균전비 {avg_eff:.2f} km/kWh\n"
        f"주행횟수 {int(weekly.get('drive_count') or 0)}회"
    )


def format_status(state_file: Path) -> str:
    state = load_json(state_file, {})
    last = state.get("last_poll") or {}

    running = process_alive(DEFAULT_PID_FILE)
    if running is True:
        running_text = "확인됨"
    elif running is False:
        running_text = "중지됨"
    else:
        running_text = "PID 파일 없음"

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
    external_drive_boost = last.get("external_drive_boost")

    polling_pid = read_pid(DEFAULT_PID_FILE)
    bot_pid = read_pid(BOT_PID_FILE)

    public_config_exists = PUBLIC_CONFIG_FILE.exists()

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

    if external_drive_boost is not None:
        lines.append(f"외부 주행 boost: {'ON' if external_drive_boost else 'OFF'}")

    if polling_pid is not None:
        lines.append(f"polling PID: {polling_pid}")

    if bot_pid is not None:
        lines.append(f"bot PID: {bot_pid}")

    lines.append(f"공개 설정 파일: {'있음' if public_config_exists else '없음'}")

    lines.extend(format_config_status(state))

    lines.append("최근 로그:")
    lines.append(tail_log())

    return "\n".join(lines)


def run_command(
    command: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    append_update_log("RUN: " + " ".join(command))

    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if stdout:
        append_update_log("STDOUT: " + stdout[-3000:])

    if stderr:
        append_update_log("STDERR: " + stderr[-3000:])

    append_update_log(f"RETURN_CODE: {result.returncode}")

    return result


def download_file(filename: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    url = f"{RAW_BASE.rstrip('/')}/{filename}"
    target = APP_DIR / filename
    temp = APP_DIR / f".{filename}.tmp"

    result = run_command(
        [
            "curl",
            "-fL",
            "--connect-timeout",
            "15",
            "--max-time",
            "60",
            "-o",
            str(temp),
            url,
        ],
        timeout=90,
    )

    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"{filename} 다운로드 실패: {result.stderr or result.stdout}")

    if not temp.exists() or temp.stat().st_size == 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"{filename} 다운로드 결과가 비어 있음")

    temp.replace(target)

    if filename.endswith(".py") or filename.endswith(".sh"):
        try:
            target.chmod(0o755)
        except OSError:
            pass


def py_compile_file(path: Path) -> None:
    result = run_command(
        [sys.executable, "-m", "py_compile", str(path)],
        cwd=APP_DIR,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(f"{path.name} 문법 검사 실패: {result.stderr or result.stdout}")


def validate_public_config() -> str:
    if not PUBLIC_CONFIG_FILE.exists():
        return "public config 없음"

    try:
        data = json.loads(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"public config JSON 오류: {exc}")

    if not isinstance(data, dict):
        raise RuntimeError("public config 최상위가 object가 아님")

    polling = data.get("polling") or {}

    if not isinstance(polling, dict):
        raise RuntimeError("public config polling 항목이 object가 아님")

    return "public config 검증 완료"


def install_boot_script() -> str:
    if not BOOT_SOURCE_FILE.exists():
        return "boot script 원본 없음. 설치 생략."

    BOOT_TARGET_DIR.mkdir(parents=True, exist_ok=True)

    temp = BOOT_TARGET_FILE.with_suffix(".tmp")
    temp.write_text(BOOT_SOURCE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    temp.chmod(0o755)
    temp.replace(BOOT_TARGET_FILE)

    return f"boot script 설치 완료: {BOOT_TARGET_FILE}"


def stop_polling_process() -> str:
    pid = read_pid(DEFAULT_PID_FILE)

    if pid is None:
        run_command(["pkill", "-f", "light_loggg_tesla_polling.py"], timeout=10)
        DEFAULT_PID_FILE.unlink(missing_ok=True)
        return "PID 파일 없음. pkill로 polling 프로세스 정리 시도함."

    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(3)

        if process_alive(DEFAULT_PID_FILE):
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)

        DEFAULT_PID_FILE.unlink(missing_ok=True)
        return f"polling PID {pid} 종료 완료"

    except ProcessLookupError:
        DEFAULT_PID_FILE.unlink(missing_ok=True)
        return f"polling PID {pid}는 이미 종료됨"

    except Exception as exc:
        run_command(["pkill", "-f", "light_loggg_tesla_polling.py"], timeout=10)
        DEFAULT_PID_FILE.unlink(missing_ok=True)
        return f"polling PID 종료 중 오류. pkill fallback 실행: {exc}"


def start_polling_process() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    python_executable = shutil.which("python3") or shutil.which("python") or sys.executable

    if not python_executable:
        raise RuntimeError("python 실행 파일을 찾지 못함")

    log_file = DEFAULT_LOG_FILE.open("a", encoding="utf-8")

    process = subprocess.Popen(
        [python_executable, str(POLLING_SCRIPT_PATH)],
        cwd=str(APP_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    DEFAULT_PID_FILE.write_text(str(process.pid) + "\n", encoding="utf-8")

    return process.pid


def run_system_check() -> tuple[int, str]:
    if not CHECK_SCRIPT_PATH.exists():
        return 1, f"check_system.py 없음: {CHECK_SCRIPT_PATH}"

    result = run_command(
        [sys.executable, str(CHECK_SCRIPT_PATH)],
        cwd=APP_DIR,
        timeout=120,
    )

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    output = stdout.strip()

    if stderr.strip():
        output = output + "\n\n[stderr]\n" + stderr.strip()

    if not output:
        output = "(check_system 출력 없음)"

    return result.returncode, output


def summarize_check_result(exit_code: int, output: str) -> str:
    fail_count = output.count("[FAIL]")
    warn_count = output.count("[WARN]")
    ok_count = output.count("[OK]")

    if exit_code == 0 and fail_count == 0:
        headline = "check_system 완료: 치명 오류 없음"
    else:
        headline = "check_system 완료: 확인 필요"

    return (
        f"{headline}\n"
        f"- exit_code: {exit_code}\n"
        f"- OK: {ok_count}\n"
        f"- WARN: {warn_count}\n"
        f"- FAIL: {fail_count}"
    )


def write_command(command: Dict[str, Any]) -> None:
    atomic_write_json(COMMAND_FILE, command)


def update_and_restart_polling(telegram_bot: Any, chat_id: str) -> None:
    started_at = now_kst()

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        append_update_log("==== UPDATE START ====")

        telegram_bot.send(chat_id, "코드 업데이트를 시작합니다.")

        for filename in UPDATE_FILES:
            download_file(filename)

        telegram_bot.send(chat_id, "GitHub raw 파일 다운로드 완료. 검사를 시작합니다.")

        config_msg = validate_public_config()

        py_compile_file(POLLING_SCRIPT_PATH)
        py_compile_file(BOT_SCRIPT_PATH)

        optional_python_files = [
            APP_DIR / "light_loggg_tesla_oauth.py",
            APP_DIR / "check_system.py",
            APP_DIR / "telemetry_server.py",
            APP_DIR / "tesla_telemetry_handler.py",
        ]

        for path in optional_python_files:
            if path.exists():
                py_compile_file(path)

        boot_msg = install_boot_script()

        stop_msg = stop_polling_process()
        new_pid = start_polling_process()

        # Give polling a little time to write last_poll/logs after restart.
        time.sleep(5)

        check_exit_code, check_output = run_system_check()
        check_summary = summarize_check_result(check_exit_code, check_output)

        elapsed = int((now_kst() - started_at).total_seconds())

        telegram_bot.send(
            chat_id,
            "업데이트 완료\n"
            f"- {config_msg}\n"
            f"- {boot_msg}\n"
            f"- {stop_msg}\n"
            f"- 새 polling PID: {new_pid}\n"
            f"- 소요 시간: {elapsed}초\n"
            f"\n{check_summary}\n"
            "\n참고: Telegram bot 자신은 현재 프로세스 그대로 유지됩니다. "
            "bot 코드 변경은 다음 부팅 또는 수동 재시작 후 완전히 반영됩니다.",
        )

        telegram_bot.send(
            chat_id,
            "check_system 상세 결과\n"
            + check_output,
        )

        append_update_log("==== UPDATE END OK ====")

    except Exception as exc:
        append_update_log(f"UPDATE FAILED: {exc}")
        telegram_bot.send(
            chat_id,
            "업데이트 실패\n"
            f"- 오류: {exc}\n"
            f"- 로그: {UPDATE_LOG_FILE}",
        )

        try:
            check_exit_code, check_output = run_system_check()
            check_summary = summarize_check_result(check_exit_code, check_output)
            telegram_bot.send(
                chat_id,
                "업데이트 실패 후 check_system 결과\n"
                f"{check_summary}\n\n"
                f"{check_output}",
            )
        except Exception as check_exc:
            telegram_bot.send(
                chat_id,
                f"업데이트 실패 후 check_system 실행도 실패: {check_exc}",
            )


class TelegramBot:
    def __init__(self, state_file: Path) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.state_file = state_file
        self.offset = 0

        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_TOKEN 환경변수가 필요합니다.")

    def send(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        for chunk in split_telegram_text(text, limit=3500):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
            }

            try:
                response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)

                if response.status_code >= 400:
                    print(
                        f"Telegram sendMessage HTTP {response.status_code}: {response.text[:500]}",
                        file=sys.stderr,
                        flush=True,
                    )

            except requests.RequestException as exc:
                print(f"Telegram sendMessage failed: {exc}", file=sys.stderr, flush=True)

            time.sleep(0.2)

    def allowed(self, chat_id: str) -> bool:
        if self.chat_id:
            return str(chat_id) == str(self.chat_id)

        self.chat_id = str(chat_id)
        return True

    def handle(self, chat_id: str, text: str) -> None:
        command = (text or "").strip().split()[0].lower()
        state = load_json(self.state_file, {})

        if command in {"/start", "start"}:
            self.send(
                chat_id,
                "LIGHT LOGGG 명령\n"
                "/status\n"
                "/daily\n"
                "/weekly\n"
                "/update\n"
                "/check\n"
                "/poll_now\n"
                "/driving_start\n"
                "/driving_stop",
            )

        elif command in {"/status", "status"}:
            self.send(chat_id, format_status(self.state_file))

        elif command in {"/daily", "daily"}:
            self.send(chat_id, format_daily_summary(state))

        elif command in {"/weekly", "weekly"}:
            self.send(chat_id, format_weekly_summary(state))

        elif command in {"/update", "update"}:
            update_and_restart_polling(self, chat_id)

        elif command in {"/check", "check", "/check_system", "check_system"}:
            exit_code, output = run_system_check()
            summary = summarize_check_result(exit_code, output)
            self.send(
                chat_id,
                summary + "\n\n" + output,
            )

        elif command in {"/poll_now", "poll_now"}:
            write_command(
                {
                    "command": "poll_now",
                    "source": "telegram",
                    "time": now_kst().isoformat(),
                }
            )
            self.send(chat_id, "poll_now 명령 파일 생성 완료")

        elif command in {"/driving_start", "driving_start"}:
            write_command(
                {
                    "command": "driving_start",
                    "source": "telegram",
                    "seconds": 180,
                    "time": now_kst().isoformat(),
                }
            )
            self.send(chat_id, "driving_start 명령 파일 생성 완료. polling boost 요청됨.")

        elif command in {"/driving_stop", "driving_stop"}:
            write_command(
                {
                    "command": "driving_stop",
                    "source": "telegram",
                    "time": now_kst().isoformat(),
                }
            )
            self.send(chat_id, "driving_stop 명령 파일 생성 완료. polling boost 해제 요청됨.")

        else:
            self.send(
                chat_id,
                "알 수 없는 명령어입니다.\n"
                "사용 가능: /status, /daily, /weekly, /update, /check, /poll_now, /driving_start, /driving_stop",
            )

    def run_forever(self) -> None:
        print("LIGHT LOGGG Telegram command bot started", flush=True)

        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"

                response = requests.get(
                    url,
                    params={
                        "offset": self.offset + 1,
                        "timeout": 25,
                    },
                    timeout=35,
                )

                data = response.json()

                if not data.get("ok"):
                    print(f"Telegram getUpdates error: {data}", file=sys.stderr, flush=True)
                    time.sleep(5)
                    continue

                for update in data.get("result", []):
                    self.offset = max(self.offset, int(update.get("update_id", 0)))

                    message = update.get("message") or {}
                    text = message.get("text") or ""
                    chat_id = str((message.get("chat") or {}).get("id") or "")

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

    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        BOT_PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"Failed to write bot PID file: {exc}", file=sys.stderr, flush=True)

    state_file = Path(os.getenv("LIGHT_LOGGG_STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser()

    TelegramBot(state_file).run_forever()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
