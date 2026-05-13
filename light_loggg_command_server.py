#!/usr/bin/env python3
"""
LIGHT LOGGG command server.

목표:
- 세컨폰/Tasker/Tailscale에서 HTTP 요청으로 주행 시작/종료 신호 전송
- polling 프로세스가 읽는 command.json 생성
- 별도 외부 패키지 없이 Python 표준 라이브러리만 사용

기본 동작:
- POST /drive/start  -> command.json에 driving_start 기록
- POST /drive/stop   -> command.json에 driving_stop 기록
- POST /poll_now     -> command.json에 poll_now 기록
- GET  /health       -> 상태 확인
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse


KST = timezone(timedelta(hours=9))

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"

COMMAND_FILE = APP_DIR / "command.json"
PID_FILE = APP_DIR / "command_server.pid"
LOG_FILE = LOG_DIR / "command_server.log"

DEFAULT_HOST = os.getenv("LIGHT_LOGGG_COMMAND_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("LIGHT_LOGGG_COMMAND_PORT", "8787"))

# 비워두면 인증 없이 동작.
# Tailscale 내부망에서만 쓸 거면 일단 비워둬도 된다.
# 나중에 필요하면 ~/.light_loggg.env에 LIGHT_LOGGG_COMMAND_SECRET=원하는값 추가.
COMMAND_SECRET = os.getenv("LIGHT_LOGGG_COMMAND_SECRET", "").strip()


def now_kst() -> datetime:
    return datetime.now(KST)


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    line = f"[{now_kst().isoformat()}] {message}"

    print(line, flush=True)

    try:
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except Exception:
        pass


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def parse_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length") or 0)

    if content_length <= 0:
        return {}

    raw = handler.rfile.read(content_length)

    if not raw:
        return {}

    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}

    return {}


def write_command(command_name: str, source: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "command": command_name,
        "source": source,
        "time": now_kst().isoformat(),
    }

    if extra:
        payload.update(extra)

    atomic_write_json(COMMAND_FILE, payload)
    log(f"command written: {payload}")

    return payload


class CommandHandler(BaseHTTPRequestHandler):
    server_version = "LightLogggCommandServer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.client_address[0]} {fmt % args}")

    def send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, query: Dict[str, list[str]], body: Dict[str, Any]) -> bool:
        if not COMMAND_SECRET:
            return True

        header_secret = self.headers.get("X-Light-Loggg-Secret", "").strip()
        query_secret = (query.get("secret") or [""])[0].strip()
        body_secret = str(body.get("secret") or "").strip()

        return COMMAND_SECRET in {header_secret, query_secret, body_secret}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "light_loggg_command_server",
                    "time": now_kst().isoformat(),
                    "command_file": str(COMMAND_FILE),
                    "auth_enabled": bool(COMMAND_SECRET),
                },
            )
            return

        self.send_json(
            404,
            {
                "ok": False,
                "error": "not found",
                "path": path,
            },
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = parse_json_body(self)

        if not self.authorized(query, body):
            self.send_json(
                401,
                {
                    "ok": False,
                    "error": "unauthorized",
                },
            )
            return

        source = str(body.get("source") or "http").strip() or "http"

        try:
            if path in {"/drive/start", "/driving_start"}:
                seconds = body.get("seconds")

                try:
                    seconds_int = int(seconds) if seconds is not None else 180
                except Exception:
                    seconds_int = 180

                if seconds_int <= 0:
                    seconds_int = 180

                command = write_command(
                    "driving_start",
                    source=source,
                    extra={
                        "seconds": seconds_int,
                        "client": self.client_address[0],
                    },
                )

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "message": "driving_start written",
                        "command": command,
                    },
                )
                return

            if path in {"/drive/stop", "/driving_stop"}:
                command = write_command(
                    "driving_stop",
                    source=source,
                    extra={
                        "client": self.client_address[0],
                    },
                )

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "message": "driving_stop written",
                        "command": command,
                    },
                )
                return

            if path in {"/poll_now", "/poll"}:
                command = write_command(
                    "poll_now",
                    source=source,
                    extra={
                        "client": self.client_address[0],
                    },
                )

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "message": "poll_now written",
                        "command": command,
                    },
                )
                return

            self.send_json(
                404,
                {
                    "ok": False,
                    "error": "not found",
                    "path": path,
                },
            )

        except Exception as exc:
            log(f"request failed: {exc}")
            self.send_json(
                500,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )


def write_pid() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def run_server(host: str, port: int) -> None:
    write_pid()

    server = ThreadingHTTPServer((host, port), CommandHandler)

    log(f"command server started host={host} port={port} pid={os.getpid()}")

    try:
        server.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        log("command server interrupted")
    finally:
        server.server_close()
        PID_FILE.unlink(missing_ok=True)
        log("command server stopped")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIGHT LOGGG command server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--daemon", action="store_true", help="accepted for compatibility; foreground process is started by caller")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    run_server(args.host, args.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
