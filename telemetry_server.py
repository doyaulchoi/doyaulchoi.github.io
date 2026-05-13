#!/usr/bin/env python3
"""
LIGHT LOGGG Tesla Fleet Telemetry HTTP server.

역할:
- Tesla Fleet Telemetry webhook/stream 수신용 Flask 서버
- 수신 데이터를 handler.py 또는 tesla_telemetry_handler.py의 process_data(data)로 전달
- Termux + cloudflared 조합에서도 돌 수 있게 단순하게 유지

주의:
- 현재 메인 운영 구조는 light_loggg_tesla_polling.py 기반 polling 방식이다.
- 이 파일은 telemetry 실험/확장용이다.
- Telegram token 등 민감값은 ~/.light_loggg.env에서 읽는다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request


APP_NAME = "LIGHT LOGGG telemetry server"

DEFAULT_ENV_FILE = Path.home() / ".light_loggg.env"
DEFAULT_HOST = os.getenv("LIGHT_LOGGG_TELEMETRY_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("LIGHT_LOGGG_TELEMETRY_PORT", "8080"))

app = Flask(__name__)

handler_module: Optional[ModuleType] = None
handler_path_global: Optional[Path] = None

stats: Dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "received_count": 0,
    "processed_count": 0,
    "error_count": 0,
    "last_received_at": None,
    "last_vehicle_id": None,
    "last_error": None,
}


def load_dotenv(path: Path = DEFAULT_ENV_FILE) -> None:
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def load_handler(handler_path: Path) -> ModuleType:
    global handler_module, handler_path_global

    if not handler_path.exists():
        raise FileNotFoundError(f"handler file not found: {handler_path}")

    spec = importlib.util.spec_from_file_location("light_loggg_telemetry_handler", str(handler_path))

    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to create import spec for handler: {handler_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "process_data"):
        raise RuntimeError(f"handler does not define process_data(data): {handler_path}")

    handler_module = module
    handler_path_global = handler_path

    if hasattr(module, "send_message"):
        try:
            module.send_message("두삼이 telemetry server handler 로드 완료")
        except Exception as exc:
            log(f"handler send_message failed during load: {exc}")

    log(f"handler loaded: {handler_path}")

    return module


def process_payload(vehicle_id: str, payload: Dict[str, Any]) -> None:
    global handler_module

    if handler_module is None:
        raise RuntimeError("handler module is not loaded")

    payload.setdefault("_vehicle_id", vehicle_id)
    payload.setdefault("_received_at", now_iso())

    handler_module.process_data(payload)


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "name": APP_NAME,
            "status": "running",
            "health": "/health",
            "telemetry_endpoint": "/api/1/vehicles/<vehicle_id>/telemetry",
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "healthy" if handler_module is not None else "degraded",
            "handler_loaded": handler_module is not None,
            "handler_path": str(handler_path_global) if handler_path_global else None,
            "stats": stats,
        }
    ), 200


@app.route("/api/1/vehicles/<vehicle_id>/telemetry", methods=["POST"])
def receive_telemetry(vehicle_id: str):
    stats["received_count"] += 1
    stats["last_received_at"] = now_iso()
    stats["last_vehicle_id"] = vehicle_id

    try:
        payload = request.get_json(silent=True)

        if payload is None:
            raw_text = request.get_data(as_text=True) or ""
            if raw_text.strip():
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    raise ValueError("request body is not valid JSON")
            else:
                raise ValueError("empty request body")

        if not isinstance(payload, dict):
            raise ValueError("telemetry payload must be a JSON object")

        process_payload(vehicle_id, payload)

        stats["processed_count"] += 1
        return jsonify({"status": "ok"}), 200

    except Exception as exc:
        stats["error_count"] += 1
        stats["last_error"] = str(exc)

        log(f"telemetry processing error: {exc}")
        traceback.print_exc()

        return jsonify(
            {
                "status": "error",
                "error": str(exc),
            }
        ), 400


@app.route("/debug/last", methods=["GET"])
def debug_last():
    return jsonify(stats), 200


def main() -> int:
    load_dotenv(DEFAULT_ENV_FILE)

    if len(sys.argv) >= 2:
        handler_path = Path(sys.argv[1]).expanduser()
    else:
        handler_path = Path(os.getenv("LIGHT_LOGGG_TELEMETRY_HANDLER", "~/light_loggg_tesla/tesla_telemetry_handler.py")).expanduser()

    host = os.getenv("LIGHT_LOGGG_TELEMETRY_HOST", DEFAULT_HOST)
    port = int(os.getenv("LIGHT_LOGGG_TELEMETRY_PORT", str(DEFAULT_PORT)))

    log(f"{APP_NAME} starting")
    log(f"host={host} port={port}")
    log(f"handler={handler_path}")

    try:
        load_handler(handler_path)
    except Exception as exc:
        stats["last_error"] = str(exc)
        stats["error_count"] += 1
        log(f"handler load failed: {exc}")
        traceback.print_exc()
        return 1

    log(f"server listening on {host}:{port}")

    app.run(
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
