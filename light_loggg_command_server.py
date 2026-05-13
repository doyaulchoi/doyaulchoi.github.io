#!/usr/bin/env python3
"""
LIGHT LOGGG command server.

세컨폰 Tasker가 Tailscale 경유로 호출하면 command.json을 생성한다.

예:
  GET /health
  GET /driving_start?token=...
  GET /driving_stop?token=...
  GET /poll_now?token=...

비공개 설정:
  ~/.light_loggg.env

필수:
  LIGHT_LOGGG_COMMAND_TOKEN

선택:
  LIGHT_LOGGG_COMMAND_HOST=0.0.0.0
  LIGHT_LOGGG_COMMAND_PORT=8787
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse


KST = timezone(timedelta(hours=9))

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"
COMMAND_FILE = APP_DIR / "command.json"
COMMAND_SERVER_PID_FILE = APP_DIR / "command_server.pid"
COMMAND_SERVER_LOG_FILE = LOG_DIR / "command_server.log"


def now_kst() -> datetime:
    return datetime.now(KST)


def load_dotenv(path: Path) -> None:
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


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    line = f"[{now_kst().isoformat()}] {message}"

    with COMMAND_SERVER_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(line + "\n")

    print(line, flush=True)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def normalize_command(path: str) -> str:
    name = path.strip("/").strip().lower()

    aliases = {
        "driving_start": "driving_start",
        "drive_start": "driving_start",
        "start": "driving_start",
        "start_driving": "driving_start",

        "driving_stop": "driving_stop",
        "drive_stop": "driving_stop",
        "stop": "driving_stop",
        "stop_driving": "driving_stop",

        "poll_now": "poll_now",
        "refresh": "poll_now",
        "wake_poll": "poll_now",

        "health": "health",
    }

    return aliases.get(name, "")


class CommandHandler(BaseHTTPRequestHandler):
    server_version = "LightLogggCommandServer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.client_address[0]} {fmt % args}")

    def send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def token_ok(self, query: Dict[str, Any]) -> bool:
        expected = os.getenv("LIGHT_LOGGG_COMMAND_TOKEN", "").strip()

        if not expected:
            return False

        values = query.get("token")
        got = str(values[0]).strip() if values else ""

        if not got:
            got = self.headers.get("X-Light-Loggg-Token", "").strip()

        return got == expected

    def handle_request(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        command = normalize_command(parsed.path)

        if command == "health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "light_loggg_command_server",
                    "time": now_kst().isoformat(),
                },
            )
            return

        if command not in {"driving_start", "driving_stop", "poll_now"}:
            self.send_json(
                404,
                {
                    "ok": False,
                    "error": "unknown command",
                    "path": parsed.path,
                },
            )
            return

        if not self.token_ok(query):
            log(f"unauthorized path={parsed.path} from={self.client_address[0]}")
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return

        source = query.get("source", ["second_phone_tasker"])[0]
        seconds = query.get("seconds", [None])[0]

        payload: Dict[str, Any] = {
            "command": command,
            "source": source,
            "received_at": now_kst().isoformat(),
            "client_ip": self.client_address[0],
        }

        if seconds is not None:
            try:
                payload["seconds"] = int(seconds)
            except Exception:
                pass

        atomic_write_json(COMMAND_FILE, payload)

        log(f"command written: {payload}")

        self.send_json(
            200,
            {
                "ok": True,
                "command": command,
                "command_file": str(COMMAND_FILE),
                "time": now_kst().isoformat(),
            },
        )

    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()


def run_server() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    host = os.getenv("LIGHT_LOGGG_COMMAND_HOST", "0.0.0.0")
    port = int(os.getenv("LIGHT_LOGGG_COMMAND_PORT", "8787"))

    if not os.getenv("LIGHT_LOGGG_COMMAND_TOKEN", "").strip():
        raise RuntimeError("LIGHT_LOGGG_COMMAND_TOKEN 환경변수가 필요합니다.")

    COMMAND_SERVER_PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    httpd = ThreadingHTTPServer((host, port), CommandHandler)

    def stop_server(*_: Any) -> None:
        log("command server stopping")
        httpd.shutdown()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)

    log(f"command server started host={host} port={port}")
    httpd.serve_forever()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIGHT LOGGG command server")
    parser.add_argument("--daemon", action="store_true", help="run server")
    return parser


def main() -> int:
    load_dotenv(Path(".env"))
    load_dotenv(Path.home() / ".light_loggg.env")

    args = build_arg_parser().parse_args()

    if not args.daemon:
        print("Use --daemon", file=sys.stderr)
        return 2

    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
