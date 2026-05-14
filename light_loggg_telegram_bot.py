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
REPO_URL = os.getenv(
    "LIGHT_LOGGG_REPO_URL",
    "https://github.com/doyaulchoi/doyaulchoi.github.io.git",
)

REPO_BRANCH = os.getenv("LIGHT_LOGGG_REPO_BRANCH", "main")

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"

DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"
DEFAULT_PID_FILE = APP_DIR / "polling.pid"
BOT_PID_FILE = APP_DIR / "telegram_bot.pid"
BOT_OFFSET_FILE = APP_DIR / "telegram_bot_offset.json"

PUBLIC_CONFIG_FILE = APP_DIR / "light_loggg_public_config.json"

DEFAULT_LOG_FILE = LOG_DIR / "polling.log"
BOT_LOG_FILE = LOG_DIR / "telegram_bot.log"
COMMAND_SERVER_LOG_FILE = LOG_DIR / "command_server.log"
UPDATE_LOG_FILE = LOG_DIR / "update.log"
BOOT_LOG_FILE = LOG_DIR / "boot.log"
BOOT_ERROR_LOG_FILE = LOG_DIR / "boot-error.log"
TRIPS_CSV_FILE = APP_DIR / "data" / "trips.csv"

TELEGRAM_DOCUMENT_LIMIT_BYTES = 50 * 1024 * 1024

COMMAND_SERVER_PID_FILE = APP_DIR / "command_server.pid"

POLLING_SCRIPT_PATH = APP_DIR / "light_loggg_tesla_polling.py"
BOT_SCRIPT_PATH = APP_DIR / "light_loggg_telegram_bot.py"
COMMAND_SERVER_SCRIPT_PATH = APP_DIR / "light_loggg_command_server.py"
CHECK_SCRIPT_PATH = APP_DIR / "check_system.py"

BOOT_SOURCE_FILE = APP_DIR / "start-light-loggg.sh"
BOOT_TARGET_DIR = Path.home() / ".termux" / "boot"
BOOT_TARGET_FILE = BOOT_TARGET_DIR / "start-light-loggg.sh"

COMMAND_FILE = APP_DIR / "command.json"

UPDATE_FILES = [
    "light_loggg_public_config.json",
    "light_loggg_tesla_polling.py",
    "light_loggg_telegram_bot.py",
    "light_loggg_command_server.py",
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


def extract_fail_lines(text: str) -> list[str]:
    lines = []

    for line in (text or "").splitlines():
        stripped = line.strip()

        if "[FAIL]" in stripped:
            lines.append(stripped)

    return lines


def load_bot_offset() -> int:
    data = load_json(BOT_OFFSET_FILE, {})

    try:
        return int(data.get("offset") or 0)
    except Exception:
        return 0


def save_bot_offset(offset: int) -> None:
    try:
        atomic_write_json(
            BOT_OFFSET_FILE,
            {
                "offset": int(offset),
                "saved_at": now_kst().isoformat(),
            },
        )
    except Exception as exc:
        print(f"Failed to save bot offset: {exc}", file=sys.stderr, flush=True)


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


def get_log_files() -> list[Path]:
    return [
        DEFAULT_LOG_FILE,
        BOT_LOG_FILE,
        COMMAND_SERVER_LOG_FILE,
        UPDATE_LOG_FILE,
        BOOT_LOG_FILE,
        BOOT_ERROR_LOG_FILE,
    ]


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
    command_server_pid = read_pid(COMMAND_SERVER_PID_FILE)

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

    if command_server_pid is not None:
        lines.append(f"command server PID: {command_server_pid}")

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


def git_update_repo() -> str:
    if not (APP_DIR / ".git").exists():
        raise RuntimeError(
            f"{APP_DIR}는 git repo가 아님. "
            "최초 1회는 git clone 방식으로 전환해야 함."
        )

    # GitHub가 원본이다. 로컬 수정 충돌 방지용으로 pull보다 fetch+reset을 쓴다.
    remote_result = run_command(
        ["git", "remote", "set-url", "origin", REPO_URL],
        cwd=APP_DIR,
        timeout=30,
    )

    if remote_result.returncode != 0:
        raise RuntimeError(f"git remote set-url 실패: {remote_result.stderr or remote_result.stdout}")

    fetch_result = run_command(
        ["git", "fetch", "origin", REPO_BRANCH],
        cwd=APP_DIR,
        timeout=120,
    )

    if fetch_result.returncode != 0:
        raise RuntimeError(f"git fetch 실패: {fetch_result.stderr or fetch_result.stdout}")

    reset_result = run_command(
        ["git", "reset", "--hard", f"origin/{REPO_BRANCH}"],
        cwd=APP_DIR,
        timeout=60,
    )

    if reset_result.returncode != 0:
        raise RuntimeError(f"git reset 실패: {reset_result.stderr or reset_result.stdout}")

    chmod_result = run_command(
        ["sh", "-c", "chmod +x *.py *.sh 2>/dev/null || true"],
        cwd=APP_DIR,
        timeout=30,
    )

    if chmod_result.returncode != 0:
        raise RuntimeError(f"chmod 실패: {chmod_result.stderr or chmod_result.stdout}")

    head_result = run_command(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=APP_DIR,
        timeout=10,
    )

    head = (head_result.stdout or "").strip() if head_result.returncode == 0 else "unknown"

    return f"git 전체 업데이트 완료: {REPO_BRANCH}@{head}"
  

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


def stop_command_server_process() -> str:
    pid = read_pid(COMMAND_SERVER_PID_FILE)

    if pid is None:
        run_command(["pkill", "-f", "light_loggg_command_server.py"], timeout=10)
        COMMAND_SERVER_PID_FILE.unlink(missing_ok=True)
        return "command server PID 파일 없음. pkill로 정리 시도함."

    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)

        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except ProcessLookupError:
            pass

        COMMAND_SERVER_PID_FILE.unlink(missing_ok=True)
        return f"command server PID {pid} 종료 완료"

    except ProcessLookupError:
        COMMAND_SERVER_PID_FILE.unlink(missing_ok=True)
        return f"command server PID {pid}는 이미 종료됨"

    except Exception as exc:
        run_command(["pkill", "-f", "light_loggg_command_server.py"], timeout=10)
        COMMAND_SERVER_PID_FILE.unlink(missing_ok=True)
        return f"command server 종료 중 오류. pkill fallback 실행: {exc}"


def start_command_server_process() -> Optional[int]:
    if not COMMAND_SERVER_SCRIPT_PATH.exists():
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    python_executable = shutil.which("python3") or shutil.which("python") or sys.executable

    if not python_executable:
        raise RuntimeError("python 실행 파일을 찾지 못함")

    log_file = COMMAND_SERVER_LOG_FILE.open("a", encoding="utf-8")

    process = subprocess.Popen(
        [python_executable, str(COMMAND_SERVER_SCRIPT_PATH), "--daemon"],
        cwd=str(APP_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    COMMAND_SERVER_PID_FILE.write_text(str(process.pid) + "\n", encoding="utf-8")

    return process.pid


def restart_bot_process_after_reply() -> None:
    python_executable = shutil.which("python3") or shutil.which("python") or sys.executable

    if not python_executable:
        raise RuntimeError("python 실행 파일을 찾지 못함")

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = BOT_LOG_FILE.open("a", encoding="utf-8")

    subprocess.Popen(
        [python_executable, str(BOT_SCRIPT_PATH)],
        cwd=str(APP_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    time.sleep(1)
    os._exit(0)


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


def summarize_check_result(exit_code: int, output: str) -> tuple[bool, str]:
    fail_lines = extract_fail_lines(output)

    if exit_code == 0 and not fail_lines:
        return True, "OK"

    if fail_lines:
        message = "업데이트 완료, 확인 필요\n" + "\n".join(fail_lines)
        return False, message

    return False, f"업데이트 완료, check_system exit_code={exit_code}"


def write_command(command: Dict[str, Any]) -> None:
    atomic_write_json(COMMAND_FILE, command)


def update_and_restart_polling(telegram_bot: Any, chat_id: str) -> None:
    started_at = now_kst()

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        append_update_log("==== UPDATE START ====")

        telegram_bot.send(chat_id, "GitHub 전체 업데이트를 시작합니다.")

        git_msg = git_update_repo()

        telegram_bot.send(chat_id, "GitHub 전체 업데이트 완료. 검사를 시작합니다.")

        config_msg = validate_public_config()

        py_compile_file(POLLING_SCRIPT_PATH)
        py_compile_file(BOT_SCRIPT_PATH)

        optional_python_files = [
            APP_DIR / "light_loggg_tesla_oauth.py",
            APP_DIR / "check_system.py",
            COMMAND_SERVER_SCRIPT_PATH,
            APP_DIR / "telemetry_server.py",
            APP_DIR / "tesla_telemetry_handler.py",
        ]

        for path in optional_python_files:
            if path.exists():
                py_compile_file(path)

        boot_msg = install_boot_script()

        stop_msg = stop_polling_process()
        command_stop_msg = stop_command_server_process()

        new_pid = start_polling_process()
        command_server_pid = start_command_server_process()

        # Give polling a little time to write last_poll/logs after restart.
        time.sleep(5)

        check_exit_code, check_output = run_system_check()
        check_ok, check_message = summarize_check_result(check_exit_code, check_output)

        if check_ok:
            telegram_bot.send(chat_id, "OK")
        else:
            telegram_bot.send(chat_id, check_message)

        append_update_log("==== UPDATE END OK ====")

        restart_bot_process_after_reply()

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
            _check_ok, check_message = summarize_check_result(check_exit_code, check_output)
            telegram_bot.send(
                chat_id,
                "업데이트 실패 후 check_system 결과\n"
                f"{check_message}",
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

        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_TOKEN 환경변수가 필요합니다.")

        self.offset = load_bot_offset()
        if self.offset <= 0:
            self.offset = self.bootstrap_offset()

    def bootstrap_offset(self) -> int:
        """첫 실행/offset 파일 없음 상태에서 기존 pending update를 버린다.

        목적:
        - /update 처리 후 bot 재시작 시 같은 /update를 다시 처리하는 루프 방지
        - 새 bot 시작 시점 이전에 쌓여 있던 update는 처리하지 않음
        """

        url = f"https://api.telegram.org/bot{self.token}/getUpdates"

        try:
            response = requests.get(
                url,
                params={
                    "timeout": 0,
                    "limit": 100,
                },
                timeout=REQUEST_TIMEOUT,
            )

            data = response.json()

            if not data.get("ok"):
                return 0

            update_ids = [
                int(update.get("update_id", 0))
                for update in data.get("result", [])
                if int(update.get("update_id", 0)) > 0
            ]

            if not update_ids:
                return 0

            latest = max(update_ids)
            save_bot_offset(latest)
            return latest

        except Exception as exc:
            print(f"bootstrap_offset failed: {exc}", file=sys.stderr, flush=True)
            return 0

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


    def send_document(self, chat_id: str, file_path: Path, caption: str = "") -> None:
        if not file_path.exists():
            self.send(chat_id, f"파일 없음: {file_path}")
            return

        if not file_path.is_file():
            self.send(chat_id, f"파일이 아님: {file_path}")
            return

        file_size = file_path.stat().st_size

        if file_size <= 0:
            self.send(chat_id, f"빈 파일: {file_path.name}")
            return

        if file_size > TELEGRAM_DOCUMENT_LIMIT_BYTES:
            self.send(
                chat_id,
                "파일이 너무 큼\n"
                f"- 파일: {file_path.name}\n"
                f"- 크기: {file_size / 1024 / 1024:.1f} MB",
            )
            return

        url = f"https://api.telegram.org/bot{self.token}/sendDocument"

        try:
            with file_path.open("rb") as file:
                response = requests.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": caption[:1024],
                    },
                    files={
                        "document": (file_path.name, file),
                    },
                    timeout=REQUEST_TIMEOUT,
                )

            if response.status_code >= 400:
                self.send(
                    chat_id,
                    "파일 전송 실패\n"
                    f"- 파일: {file_path.name}\n"
                    f"- HTTP: {response.status_code}\n"
                    f"- 응답: {response.text[:500]}",
                )

        except requests.RequestException as exc:
            self.send(
                chat_id,
                "파일 전송 요청 실패\n"
                f"- 파일: {file_path.name}\n"
                f"- 오류: {exc}",
            )

        except Exception as exc:
            self.send(
                chat_id,
                "파일 전송 처리 실패\n"
                f"- 파일: {file_path.name}\n"
                f"- 오류: {exc}",
            )

  
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
                "🤖 LIGHT LOGGG 명령어\n\n"
                "📊 상태 확인\n"
                "  /status  - 현재 로거/차량 상태\n"
                "  /check   - 시스템 진단\n"
                "  /log     - 실행 로그 파일 전송\n"
                "  /trips   - 주행 CSV 전송\n\n"
                "📈 주행 요약\n"
                "  /daily   - 오늘 주행 요약\n"
                "  /weekly  - 주간 주행 요약\n\n"
                "🔄 업데이트\n"
                "  /update  - GitHub 최신 코드 반영\n\n"
                "⚡ 즉시 제어\n"
                "  /poll_now       - 즉시 1회 polling 요청\n"
                "  /driving_start  - 주행 시작 boost 요청\n"
                "  /driving_stop   - 주행 boost 해제\n"
            )

        elif command in {"/status", "status"}:
            self.send(chat_id, format_status(self.state_file))

        elif command in {"/daily", "daily"}:
            self.send(chat_id, format_daily_summary(state))

        elif command in {"/weekly", "weekly"}:
            self.send(chat_id, format_weekly_summary(state))
            self.send_document(
                chat_id,
                TRIPS_CSV_FILE,
                "두삼이 주행 데이터 CSV",
            )

        elif command in {"/log", "log", "/logs", "logs"}:
            self.send(chat_id, "실행 로그 파일 전송을 시작합니다.")

            for log_file in get_log_files():
                self.send_document(
                    chat_id,
                    log_file,
                    f"LIGHT LOGGG log: {log_file.name}",
                )

        elif command in {"/trips", "trips", "/drive", "drive", "/drive_data", "drive_data"}:
            self.send_document(
                chat_id,
                TRIPS_CSV_FILE,
                "두삼이 주행 데이터 CSV",
            )

        elif command in {"/update", "update"}:
            update_and_restart_polling(self, chat_id)

        elif command in {"/check", "check", "/check_system", "check_system"}:
            exit_code, output = run_system_check()
            check_ok, check_message = summarize_check_result(exit_code, output)

            if check_ok:
                self.send(chat_id, "OK")
            else:
                self.send(chat_id, check_message)

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
                "🤖 LIGHT LOGGG 명령어\n\n"
                "📊 상태 확인\n"
                "  /status  - 현재 로거/차량 상태\n"
                "  /check   - 시스템 진단\n\n"
                "📈 주행 요약\n"
                "  /daily   - 오늘 주행 요약\n"
                "  /weekly  - 주간 주행 요약\n\n"
                "🔄 업데이트\n"
                "  /update  - GitHub 최신 코드 반영\n\n"
                "⚡ 즉시 제어\n"
                "  /poll_now       - 즉시 1회 polling 요청\n"
                "  /driving_start  - 주행 시작 boost 요청\n"
                "  /driving_stop   - 주행 boost 해제\n"
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
                    update_id = int(update.get("update_id", 0))
                    self.offset = max(self.offset, update_id)
                    save_bot_offset(self.offset)

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
