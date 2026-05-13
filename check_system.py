#!/usr/bin/env python3
"""
LIGHT LOGGG system checker for Termux.

현재 운영 기준:
- 앱 폴더: ~/light_loggg_tesla
- 공개 설정: ~/light_loggg_tesla/light_loggg_public_config.json
- 비공개 env: ~/.light_loggg.env
- Tesla token 파일: ~/.light_loggg_tesla_tokens.json
- state 파일: ~/.light_loggg_state.json
- command 파일: ~/light_loggg_tesla/command.json
- polling PID: ~/light_loggg_tesla/polling.pid
- Telegram bot PID: ~/light_loggg_tesla/telegram_bot.pid
- logs: ~/light_loggg_tesla/logs/

주의:
- 이 스크립트는 진단용이다.
- 토큰/secret 값을 출력하지 않는다.
- 자동 복구는 하지 않는다.
- 실제 업데이트는 Telegram /update 또는 GitHub raw curl 방식으로 수행한다.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


# =========================
# Paths
# =========================

HOME = Path.home()

APP_DIR = HOME / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"

PUBLIC_CONFIG_FILE = APP_DIR / "light_loggg_public_config.json"

ENV_FILE = HOME / ".light_loggg.env"
TOKEN_FILE = HOME / ".light_loggg_tesla_tokens.json"
STATE_FILE = HOME / ".light_loggg_state.json"

COMMAND_FILE = APP_DIR / "command.json"

POLLING_SCRIPT = APP_DIR / "light_loggg_tesla_polling.py"
BOT_SCRIPT = APP_DIR / "light_loggg_telegram_bot.py"
OAUTH_SCRIPT = APP_DIR / "light_loggg_tesla_oauth.py"
CHECK_SCRIPT = APP_DIR / "check_system.py"
BOOT_SOURCE_SCRIPT = APP_DIR / "start-light-loggg.sh"

TELEMETRY_SERVER_SCRIPT = APP_DIR / "telemetry_server.py"
TELEMETRY_HANDLER_SCRIPT = APP_DIR / "tesla_telemetry_handler.py"

SETUP_SCRIPT = APP_DIR / "setup_light_loggg_tesla_polling.sh"
SETUP_TELEMETRY_GO_SCRIPT = APP_DIR / "setup_tesla_telemetry.sh"
SETUP_TELEMETRY_PYTHON_SCRIPT = APP_DIR / "setup_tesla_telemetry_python.sh"

POLLING_PID = APP_DIR / "polling.pid"
BOT_PID = APP_DIR / "telegram_bot.pid"

POLLING_LOG = LOG_DIR / "polling.log"
BOT_LOG = LOG_DIR / "telegram_bot.log"
BOOT_LOG = LOG_DIR / "boot.log"
BOOT_ERROR_LOG = LOG_DIR / "boot-error.log"
UPDATE_LOG = LOG_DIR / "update.log"

BOOT_TARGET_SCRIPT = HOME / ".termux" / "boot" / "start-light-loggg.sh"

RAW_BASE_DEFAULT = "https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main"

DNS_HOSTS = [
    "api.telegram.org",
    "google.com",
    "fleet-api.prd.na.vn.cloud.tesla.com",
]

REQUIRED_PRIVATE_ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TESLA_CLIENT_ID",
    "TESLA_CLIENT_SECRET",
    "TESLA_API_BASE",
    "TESLA_SCOPE",
]

OPTIONAL_PRIVATE_ENV_KEYS = [
    "TESLA_VIN",
    "TESLA_REDIRECT_URI",
    "LIGHT_LOGGG_STATE_FILE",
    "LIGHT_LOGGG_RAW_BASE",
]

# env에 있어도 되는 override 값. 공개 config보다 우선한다.
OPTIONAL_OVERRIDE_ENV_KEYS = [
    "LIGHT_LOGGG_POLL_ASLEEP_SECONDS",
    "LIGHT_LOGGG_POLL_ONLINE_SECONDS",
    "LIGHT_LOGGG_POLL_DRIVING_SECONDS",
    "LIGHT_LOGGG_POLL_CHARGING_SECONDS",
    "LIGHT_LOGGG_POLL_ERROR_SECONDS",
    "LIGHT_LOGGG_THRESHOLD_KM_PER_KWH",
    "LIGHT_LOGGG_WINDOW_MINUTES",
    "LIGHT_LOGGG_ALERT_COOLDOWN_SECONDS",
    "LIGHT_LOGGG_EXTERNAL_DRIVE_BOOST_SECONDS",
    "LIGHT_LOGGG_REQUEST_TIMEOUT",
    "LIGHT_LOGGG_MORNING_ALERT_HOUR",
    "LIGHT_LOGGG_MORNING_ALERT_MINUTE",
]

EXPECTED_UPDATE_FILES = [
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


# =========================
# Printing helpers
# =========================

def ok(message: str) -> None:
    print(f"[OK] {message}")


def warn(message: str) -> None:
    print(f"[WARN] {message}")


def fail(message: str) -> None:
    print(f"[FAIL] {message}")


def info(message: str) -> None:
    print(f"[INFO] {message}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def mask_value(value: str, keep: int = 4) -> str:
    if not value:
        return ""

    if len(value) <= keep * 2:
        return "*" * len(value)

    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


# =========================
# File/env/json helpers
# =========================

def load_env_file(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}

    if not path.exists():
        return env

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        fail(f"env 파일 읽기 실패: {path} / {exc}")
        return env

    for line_no, raw in enumerate(lines, start=1):
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            warn(f"{path} {line_no}행: '=' 없는 줄 무시: {line}")
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            warn(f"{path} {line_no}행: key 비어 있음")
            continue

        if (
            " " in value
            and not (
                (value.startswith('"') and value.endswith('"'))
                or (value.startswith("'") and value.endswith("'"))
            )
        ):
            warn(f"{path} {line_no}행: 공백 포함 값은 따옴표 권장: {key}")

        value = value.strip('"').strip("'")
        env[key] = value

    return env


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(data, dict):
            return data

        fail(f"{path} JSON 최상위가 object가 아님")
        return None

    except json.JSONDecodeError as exc:
        fail(f"{path} JSON 파싱 실패: {exc}")
        return None

    except Exception as exc:
        fail(f"{path} 읽기 실패: {exc}")
        return None


def check_file(path: Path, label: str, required: bool = True) -> bool:
    if path.exists():
        if path.is_file():
            ok(f"{label} 파일 있음: {path}")
            return True

        fail(f"{label} 경로가 파일이 아님: {path}")
        return False

    if required:
        fail(f"{label} 파일 없음: {path}")
    else:
        warn(f"{label} 파일 없음: {path}")

    return False


def check_dir(path: Path, label: str, required: bool = True) -> bool:
    if path.exists():
        if path.is_dir():
            ok(f"{label} 폴더 있음: {path}")
            return True

        fail(f"{label} 경로가 폴더가 아님: {path}")
        return False

    if required:
        fail(f"{label} 폴더 없음: {path}")
    else:
        warn(f"{label} 폴더 없음: {path}")

    return False


def run_command(command: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        return result.returncode, result.stdout.strip(), result.stderr.strip()

    except FileNotFoundError:
        return 127, "", f"command not found: {command[0]}"

    except subprocess.TimeoutExpired:
        return 124, "", "timeout"

    except Exception as exc:
        return 1, "", str(exc)


# =========================
# Config checks
# =========================

def check_public_config() -> Optional[Dict[str, Any]]:
    if not check_file(PUBLIC_CONFIG_FILE, "공개 설정", required=True):
        return None

    config = read_json(PUBLIC_CONFIG_FILE)

    if config is None:
        return None

    ok("공개 설정 JSON 문법 OK")

    polling = config.get("polling")
    alerts = config.get("alerts")
    external_commands = config.get("external_commands")
    request_cfg = config.get("request")
    morning_alert = config.get("morning_alert")

    if isinstance(polling, dict):
        ok("public_config.polling 있음")

        expected_polling_keys = [
            "asleep_seconds",
            "online_seconds",
            "driving_seconds",
            "charging_seconds",
            "error_seconds",
        ]

        for key in expected_polling_keys:
            value = polling.get(key)

            if isinstance(value, int):
                ok(f"polling.{key}={value}")
            else:
                warn(f"polling.{key} 값 없음 또는 int 아님: {value}")
    else:
        fail("public_config.polling 없음 또는 object 아님")

    if isinstance(alerts, dict):
        ok("public_config.alerts 있음")
        info(f"alerts.threshold_km_per_kwh={alerts.get('threshold_km_per_kwh', '-')}")
        info(f"alerts.window_minutes={alerts.get('window_minutes', '-')}")
        info(f"alerts.alert_cooldown_seconds={alerts.get('alert_cooldown_seconds', '-')}")
    else:
        warn("public_config.alerts 없음 또는 object 아님")

    if isinstance(external_commands, dict):
        ok("public_config.external_commands 있음")
        info(f"external_commands.drive_boost_seconds={external_commands.get('drive_boost_seconds', '-')}")
    else:
        warn("public_config.external_commands 없음 또는 object 아님")

    if isinstance(request_cfg, dict):
        ok("public_config.request 있음")
        info(f"request.timeout_seconds={request_cfg.get('timeout_seconds', '-')}")
    else:
        warn("public_config.request 없음 또는 object 아님")

    if isinstance(morning_alert, dict):
        ok("public_config.morning_alert 있음")
        info(f"morning_alert.hour={morning_alert.get('hour', '-')}")
        info(f"morning_alert.minute={morning_alert.get('minute', '-')}")
    else:
        warn("public_config.morning_alert 없음. 코드 기본값 사용 가능")

    return config


def check_env_file() -> Dict[str, str]:
    if not check_file(ENV_FILE, "비공개 env", required=True):
        return {}

    env = load_env_file(ENV_FILE)

    for key in REQUIRED_PRIVATE_ENV_KEYS:
        value = env.get(key)

        if value:
            if "TOKEN" in key or "SECRET" in key:
                ok(f"{key} 있음: length={len(value)} masked={mask_value(value)}")
            elif key == "TESLA_SCOPE":
                ok(f"{key}={value}")
            else:
                ok(f"{key}={value}")
        else:
            fail(f"{key} 없음")

    for key in OPTIONAL_PRIVATE_ENV_KEYS:
        value = env.get(key)

        if value:
            if key == "TESLA_VIN":
                ok(f"{key} 있음: {mask_value(value, keep=5)}")
            else:
                ok(f"{key}={value}")
        else:
            warn(f"{key} 없음. 필요 시 생략 가능")

    override_found = False

    for key in OPTIONAL_OVERRIDE_ENV_KEYS:
        value = env.get(key)

        if value:
            override_found = True
            warn(f"env override 있음: {key}={value}")

    if not override_found:
        ok("polling/alert override env 없음. 공개 설정 기준으로 동작 예상")

    return env


def compare_env_and_public_config(env: Dict[str, str], config: Optional[Dict[str, Any]]) -> None:
    if config is None:
        warn("공개 설정을 읽지 못해 env/config 비교 생략")
        return

    polling = config.get("polling") or {}

    mapping = [
        ("LIGHT_LOGGG_POLL_ASLEEP_SECONDS", "asleep_seconds"),
        ("LIGHT_LOGGG_POLL_ONLINE_SECONDS", "online_seconds"),
        ("LIGHT_LOGGG_POLL_DRIVING_SECONDS", "driving_seconds"),
        ("LIGHT_LOGGG_POLL_CHARGING_SECONDS", "charging_seconds"),
        ("LIGHT_LOGGG_POLL_ERROR_SECONDS", "error_seconds"),
    ]

    for env_key, config_key in mapping:
        env_value = env.get(env_key)
        config_value = polling.get(config_key)

        if env_value:
            warn(f"{env_key}가 env에 있어 공개 설정 polling.{config_key}={config_value}보다 우선 적용됨")
        else:
            ok(f"{env_key} env override 없음. public_config polling.{config_key}={config_value} 적용 예상")


# =========================
# Python/process/network checks
# =========================

def check_python_compile(path: Path) -> bool:
    if not path.exists():
        fail(f"문법 검사 불가, 파일 없음: {path}")
        return False

    code, stdout, stderr = run_command([sys.executable, "-m", "py_compile", str(path)], timeout=30)

    if code == 0:
        ok(f"Python 문법 OK: {path.name}")
        return True

    fail(f"Python 문법 오류: {path.name}")

    if stdout:
        print(stdout)

    if stderr:
        print(stderr)

    return False


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_alive(pid: int) -> Optional[bool]:
    try:
        os.kill(pid, 0)
        return True

    except ProcessLookupError:
        return False

    except PermissionError:
        return True

    except Exception:
        return None


def check_pid_file(path: Path, label: str) -> bool:
    if not path.exists():
        warn(f"{label} PID 파일 없음: {path}")
        return False

    pid = read_pid(path)

    if pid is None:
        fail(f"{label} PID 파일 읽기 실패 또는 숫자 아님: {path}")
        return False

    alive = pid_alive(pid)

    if alive is True:
        ok(f"{label} 프로세스 실행 중: PID {pid}")
        return True

    if alive is False:
        fail(f"{label} PID 파일은 있으나 프로세스 없음: PID {pid}")
        return False

    warn(f"{label} PID 확인 불명확: PID {pid}")
    return False


def check_process_by_pgrep(script_name: str) -> bool:
    code, stdout, stderr = run_command(["pgrep", "-f", script_name], timeout=10)

    if code == 0 and stdout:
        pids = [line.strip() for line in stdout.splitlines() if line.strip().isdigit()]

        if pids:
            ok(f"{script_name} pgrep 확인됨: {', '.join(pids)}")
            return True

    warn(f"{script_name} pgrep 확인 안 됨")

    if stderr:
        info(stderr)

    return False


def check_dns() -> bool:
    all_ok = True

    for host in DNS_HOSTS:
        try:
            ip = socket.gethostbyname(host)
            ok(f"DNS OK: {host} -> {ip}")
        except Exception as exc:
            fail(f"DNS 실패: {host} / {exc}")
            all_ok = False

    return all_ok


def check_telegram(env: Dict[str, str]) -> bool:
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_TOKEN")

    if not token:
        fail("TELEGRAM_BOT_TOKEN 없음")
        return False

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )

    except requests.RequestException as exc:
        fail(f"Telegram getMe 요청 실패: {exc}")
        return False

    try:
        data = response.json()

    except ValueError:
        fail(f"Telegram 응답 JSON 아님: HTTP {response.status_code}")
        return False

    if response.status_code == 200 and data.get("ok"):
        username = (data.get("result") or {}).get("username") or "-"
        ok(f"Telegram bot 연결 OK: @{username}")
        return True

    fail(f"Telegram bot 연결 실패: HTTP {response.status_code} {data}")
    return False


def check_tesla_token_file() -> bool:
    data = read_json(TOKEN_FILE)

    if data is None:
        fail(f"Tesla token 파일 없음 또는 오류: {TOKEN_FILE}")
        return False

    refresh_token = data.get("refresh_token")

    if isinstance(refresh_token, str) and len(refresh_token) > 20:
        ok(f"Tesla refresh_token 있음: length={len(refresh_token)} masked={mask_value(refresh_token)}")
        return True

    fail("Tesla token 파일에 정상 refresh_token 없음")
    return False


def check_state_file() -> bool:
    data = read_json(STATE_FILE)

    if data is None:
        warn(f"state 파일 없음 또는 오류: {STATE_FILE}")
        return False

    last_poll = data.get("last_poll") or {}

    if last_poll:
        ok("state 파일에 last_poll 있음")
        info(f"최근 상태: {last_poll.get('status') or '-'}")
        info(f"최근 차량명: {last_poll.get('vehicle_name') or '-'}")
        info(f"최근 배터리: {last_poll.get('battery_level') or '-'}")
        info(f"다음 주기: {last_poll.get('next_seconds') or '-'}")

        config = last_poll.get("config") or {}

        if isinstance(config, dict) and config:
            ok("last_poll.config 있음")
            info(
                "적용 기록 설정: "
                f"asleep={config.get('asleep_seconds', '-')} "
                f"online={config.get('online_seconds', '-')} "
                f"driving={config.get('driving_seconds', '-')} "
                f"charging={config.get('charging_seconds', '-')} "
                f"error={config.get('error_seconds', '-')}"
            )
        else:
            warn("last_poll.config 없음. 새 polling 코드 적용 전이거나 아직 폴링 전일 수 있음")
    else:
        warn("state 파일은 있으나 last_poll 없음")

    access_token = data.get("access_token")
    expires_at = data.get("access_token_expires_at")

    if isinstance(access_token, str) and access_token:
        ok(f"state access_token 있음: length={len(access_token)} masked={mask_value(access_token)}")
    else:
        warn("state access_token 없음. refresh_token으로 갱신 가능하면 문제 아닐 수 있음")

    if isinstance(expires_at, (int, float)):
        remain = int(expires_at - time.time())

        if remain > 0:
            ok(f"access_token 만료까지 약 {remain}초")
        else:
            warn(f"access_token 만료됨: {abs(remain)}초 지남")

    return True


def check_raw_url_access(env: Dict[str, str]) -> None:
    raw_base = env.get("LIGHT_LOGGG_RAW_BASE") or RAW_BASE_DEFAULT
    test_url = raw_base.rstrip("/") + "/light_loggg_public_config.json"

    try:
        response = requests.get(test_url, timeout=10)

        if response.status_code == 200:
            ok(f"GitHub raw 접근 OK: {test_url}")
        else:
            warn(f"GitHub raw 접근 비정상: HTTP {response.status_code} {test_url}")

    except requests.RequestException as exc:
        warn(f"GitHub raw 접근 실패: {exc}")


def check_update_files() -> None:
    for filename in EXPECTED_UPDATE_FILES:
        path = APP_DIR / filename

        if path.exists():
            ok(f"업데이트 대상 파일 있음: {filename}")
        else:
            warn(f"업데이트 대상 파일 없음: {filename}")


def tail_file(path: Path, lines: int = 10) -> None:
    if not path.exists():
        warn(f"로그 파일 없음: {path}")
        return

    code, stdout, stderr = run_command(["tail", "-n", str(lines), str(path)], timeout=10)

    print(f"\n--- tail {path} ---")

    if stdout:
        print(stdout)
    elif stderr:
        print(stderr)
    else:
        print("(empty)")


# =========================
# Output command guide
# =========================

def print_update_commands() -> None:
    print("\n--- 수동 업데이트 명령 참고 ---")
    print("cd ~/light_loggg_tesla")
    print("curl -L -o light_loggg_public_config.json \\")
    print("https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_public_config.json")
    print("curl -L -o light_loggg_tesla_polling.py \\")
    print("https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_tesla_polling.py")
    print("curl -L -o light_loggg_telegram_bot.py \\")
    print("https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/light_loggg_telegram_bot.py")
    print("python -m py_compile light_loggg_tesla_polling.py")
    print("python -m py_compile light_loggg_telegram_bot.py")
    print("python -m json.tool light_loggg_public_config.json >/dev/null && echo CONFIG_OK")
    print("~/.termux/boot/start-light-loggg.sh")


def print_private_env_example() -> None:
    print("\n--- ~/.light_loggg.env 예시 ---")
    print('TELEGRAM_BOT_TOKEN="새_텔레그램_봇_토큰"')
    print('TELEGRAM_CHAT_ID="8792879646"')
    print('TESLA_CLIENT_ID="네_CLIENT_ID"')
    print('TESLA_CLIENT_SECRET="네_CLIENT_SECRET"')
    print('TESLA_VIN=5YJ3E1ECXMF912228')
    print("TESLA_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com")
    print('TESLA_SCOPE="openid offline_access user_data vehicle_device_data vehicle_location"')


# =========================
# Main
# =========================

def main() -> int:
    print("\n=== LIGHT LOGGG 시스템 진단 시작 ===\n")

    section("1. 경로 확인")
    check_dir(APP_DIR, "앱")
    check_dir(LOG_DIR, "로그", required=False)

    check_file(PUBLIC_CONFIG_FILE, "공개 설정", required=True)

    check_file(POLLING_SCRIPT, "polling script")
    check_file(BOT_SCRIPT, "telegram bot script")
    check_file(OAUTH_SCRIPT, "oauth script", required=False)
    check_file(CHECK_SCRIPT, "check script", required=False)
    check_file(BOOT_SOURCE_SCRIPT, "GitHub boot script 원본", required=False)
    check_file(BOOT_TARGET_SCRIPT, "Termux:Boot script 설치본", required=False)

    check_file(TELEMETRY_SERVER_SCRIPT, "telemetry server", required=False)
    check_file(TELEMETRY_HANDLER_SCRIPT, "telemetry handler", required=False)
    check_file(SETUP_SCRIPT, "setup script", required=False)
    check_file(SETUP_TELEMETRY_GO_SCRIPT, "telemetry Go setup script", required=False)
    check_file(SETUP_TELEMETRY_PYTHON_SCRIPT, "telemetry Python setup script", required=False)

    section("2. 공개 설정 확인")
    public_config = check_public_config()

    section("3. 비공개 env 확인")
    env = check_env_file()
    compare_env_and_public_config(env, public_config)

    section("4. token/state 확인")
    check_tesla_token_file()
    check_state_file()

    section("5. Python 문법 검사")
    check_python_compile(POLLING_SCRIPT)
    check_python_compile(BOT_SCRIPT)

    if OAUTH_SCRIPT.exists():
        check_python_compile(OAUTH_SCRIPT)

    if CHECK_SCRIPT.exists():
        check_python_compile(CHECK_SCRIPT)

    if TELEMETRY_SERVER_SCRIPT.exists():
        check_python_compile(TELEMETRY_SERVER_SCRIPT)

    if TELEMETRY_HANDLER_SCRIPT.exists():
        check_python_compile(TELEMETRY_HANDLER_SCRIPT)

    section("6. JSON 문법 검사")
    if PUBLIC_CONFIG_FILE.exists():
        code, stdout, stderr = run_command(
            [sys.executable, "-m", "json.tool", str(PUBLIC_CONFIG_FILE)],
            timeout=10,
        )

        if code == 0:
            ok("public config JSON 문법 OK")
        else:
            fail("public config JSON 문법 오류")
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)

    if STATE_FILE.exists():
        code, stdout, stderr = run_command(
            [sys.executable, "-m", "json.tool", str(STATE_FILE)],
            timeout=10,
        )

        if code == 0:
            ok("state JSON 문법 OK")
        else:
            warn("state JSON 문법 오류")
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)

    if TOKEN_FILE.exists():
        code, stdout, stderr = run_command(
            [sys.executable, "-m", "json.tool", str(TOKEN_FILE)],
            timeout=10,
        )

        if code == 0:
            ok("token JSON 문법 OK")
        else:
            fail("token JSON 문법 오류")
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)

    section("7. 프로세스/PID 확인")
    check_pid_file(POLLING_PID, "polling")
    check_pid_file(BOT_PID, "telegram bot")
    check_process_by_pgrep("light_loggg_tesla_polling.py")
    check_process_by_pgrep("light_loggg_telegram_bot.py")

    section("8. 네트워크/DNS 확인")
    check_dns()

    section("9. Telegram API 확인")
    if env:
        check_telegram(env)
    else:
        fail("env를 읽지 못해서 Telegram 확인 불가")

    section("10. GitHub raw 접근 확인")
    check_raw_url_access(env)

    section("11. /update 대상 파일 확인")
    check_update_files()

    section("12. command 파일 확인")
    if COMMAND_FILE.exists():
        warn(f"command 파일이 남아 있음: {COMMAND_FILE}")
        try:
            print(COMMAND_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            warn(f"command 파일 읽기 실패: {exc}")
    else:
        ok("command 파일 없음. 대기 명령 없음")

    section("13. 최근 로그")
    tail_file(POLLING_LOG, lines=10)
    tail_file(BOT_LOG, lines=10)
    tail_file(BOOT_LOG, lines=10)
    tail_file(BOOT_ERROR_LOG, lines=10)
    tail_file(UPDATE_LOG, lines=10)

    print_update_commands()
    print_private_env_example()

    print("\n=== LIGHT LOGGG 시스템 진단 종료 ===\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
