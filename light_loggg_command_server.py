#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))

APP_DIR = Path.home() / "light_loggg_tesla"
LOG_DIR = APP_DIR / "logs"
COMMAND_FILE = APP_DIR / "command.json"
PID_FILE = APP_DIR / "command_server.pid"

DEFAULT_HOST = os.getenv("LIGHT_LOGGG_COMMAND_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("LIGHT_LOGGG_COMMAND_PORT", "8787"))
DEFAULT_DRIVE_BOOST_SECONDS = int(os.getenv("LIGHT_LOGGG_EXTERNAL_DRIVE_BOOST_SECONDS", "180"))
COMMAND_SECRET = os.getenv("LIGHT_LOGGG_COMMAND_SECRET", "").strip()


def now_kst() -> datetime:
    return datetime.now(KST)


def log(text: str) -> None:
    """
    로그는 stdout으로만 출력한다.
    start-light-loggg.sh 또는 nohup 실행부에서 stdout/stderr를 command_server.log로 리다이렉트한다.
    여기서 파일에 직접 쓰면 로그가 중복된다.
    """
    line = f"[{now_kst().isoformat()}] {text}"
    print(line, flush=True)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def safe_int(value: Any, default: int) -> int:
    try:
        result = int(value)
        if result <= 0:
            return default
        return result
    except Exception:
        return default


def normalize_command(command: str) -> str:
    command = (command or "").strip().lower().lstrip("/")

    aliases = {
        "wake_poll": "poll_now",
        "refresh": "poll_now",
        "drive_start": "driving_start",
        "start_driving": "driving_start",
        "drive_stop": "driving_stop",
        "stop_driving": "driving_stop",
        "clear_boost": "driving_stop",
    }

    return aliases.get(command, command)


def build_command(command: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    params = params or {}
    command = normalize_command(command)

    if command not in {"poll_now", "driving_start", "driving_stop"}:
        raise ValueError(f"unsupported command: {command}")

    payload: Dict[str, Any] = {
        "command": command,
        "source": "http",
        "time": now_kst().isoformat(),
    }

    if command == "driving_start":
        payload["seconds"] = safe_int(params.get("seconds"), DEFAULT_DRIVE_BOOST_SECONDS)

    return payload


class CommandHandler(BaseHTTPRequestHandler):
    server_version = "LightLogggCommandServer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.client_address[0]} {fmt % args}")

    def send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def check_secret(
        self,
        query: Dict[str, list[str]],
        body: Dict[str, Any] | None = None,
    ) -> bool:
        if not COMMAND_SECRET:
            return True

        header_secret = self.headers.get("X-Light-Loggg-Secret", "").strip()
        query_secret = (query.get("secret") or [""])[0].strip()
        body_secret = ""

        if isinstance(body, dict):
            body_secret = str(body.get("secret") or "").strip()

        return COMMAND_SECRET in {header_secret, query_secret, body_secret}

    def handle_command(self, command: str, params: Dict[str, Any] | None = None) -> None:
        try:
            payload = build_command(command, params=params)
            atomic_write_json(COMMAND_FILE, payload)

            log(f"command written: {payload}")

            self.send_json(
                200,
                {
                    "ok": True,
                    "command_file": str(COMMAND_FILE),
                    "command": payload,
                },
            )

        except Exception as exc:
            log(f"command failed: {exc}")
            self.send_json(
                400,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        query = parse_qs(parsed.query)

        if not self.check_secret(query):
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path in {"", "health"}:
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "light_loggg_command_server",
                    "time": now_kst().isoformat(),
                    "command_file": str(COMMAND_FILE),
                    "supported": [
                        "/health",
                        "/poll_now",
                        "/driving_start",
                        "/driving_stop",
                        "/command?name=driving_start",
                    ],
                },
            )
            return

        if path == "command":
            command = (query.get("name") or query.get("command") or [""])[0]

            params: Dict[str, Any] = {}
            if query.get("seconds"):
                params["seconds"] = query["seconds"][0]

            self.handle_command(command, params=params)
            return

        if path in {
            "poll_now",
            "driving_start",
            "driving_stop",
            "wake_poll",
            "refresh",
            "drive_start",
            "start_driving",
            "drive_stop",
            "stop_driving",
            "clear_boost",
        }:
            params = {}

            if query.get("seconds"):
                params["seconds"] = query["seconds"][0]

            self.handle_command(path, params=params)
            return

        self.send_json(
            404,
            {
                "ok": False,
                "error": "not found",
                "path": parsed.path,
            },
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        query = parse_qs(parsed.query)

        length = safe_int(self.headers.get("Content-Length"), 0)
        raw_body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        if not self.check_secret(query, body):
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "command":
            command = str(body.get("command") or body.get("name") or "")
            self.handle_command(command, params=body)
            return

        if path in {"poll_now", "driving_start", "driving_stop"}:
            self.handle_command(path, params=body)
            return

        self.send_json(
            404,
            {
                "ok": False,
                "error": "not found",
                "path": parsed.path,
            },
        )


def run_server(host: str, port: int) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    server = ThreadingHTTPServer((host, port), CommandHandler)

    log(f"command server started host={host} port={port} pid={os.getpid()}")

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        log("command server stopped")
        server.server_close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIGHT LOGGG HTTP command server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--daemon", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
