#!/usr/bin/env python3
"""Issue a Tesla Fleet API user refresh token for LIGHT LOGGG.

This script is intended for Termux or any terminal-only environment. It prints a
Tesla authorization URL, asks the user to paste the redirected URL or the code
parameter, exchanges the authorization code for tokens, validates the token by
calling the Fleet API products endpoint, and writes only the refresh token needed
by light_loggg_tesla_polling.py.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

CLIENT_ID = os.environ.get("TESLA_CLIENT_ID", "d1351a7e-42fd-4318-b6a2-c9d702af75c1")
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("TESLA_REDIRECT_URI", "https://doyaulchoi.github.io/index.html")
AUDIENCE = os.environ.get("TESLA_AUDIENCE", "https://fleet-api.prd.na.vn.cloud.tesla.com")
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
DEFAULT_SCOPE = "openid offline_access user_data vehicle_device_data vehicle_location"
DEFAULT_TOKEN_FILE = Path.home() / ".light_loggg_tesla_tokens.json"
DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"


def fail(message: str, code: int = 1) -> None:
    print(f"error={message}", file=sys.stderr)
    sys.exit(code)


def extract_code(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        fail("Tesla redirect URL 또는 code가 비어 있습니다.")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("error"):
            fail("Tesla 로그인 오류: " + query["error"][0])
        codes = query.get("code")
        if not codes or not codes[0].strip():
            fail("붙여넣은 URL에서 code 파라미터를 찾지 못했습니다.")
        return codes[0].strip()
    return raw


def build_authorize_url(scope: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": CLIENT_ID,
        "locale": "ko-KR",
        "prompt": "login",
        "prompt_missing_scopes": "true",
        "require_requested_scopes": "true",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote), state


def post_token(payload: dict[str, str]) -> dict[str, Any]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(TOKEN_URL, data=payload, headers=headers, timeout=30)
    try:
        data = response.json()
    except ValueError:
        fail(f"Tesla token 응답이 JSON이 아닙니다. HTTP {response.status_code}: {response.text[:300]}")
    if response.status_code >= 400:
        fail(f"Tesla token exchange failed: HTTP {response.status_code} {json.dumps(data, ensure_ascii=False)}")
    return data


def exchange_code(code: str, scope: str) -> dict[str, Any]:
    if not CLIENT_SECRET:
        fail("TESLA_CLIENT_SECRET 환경변수가 비어 있습니다. 이 스크립트는 authorization_code 교환에 client_secret이 필요합니다.")
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "audience": AUDIENCE,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
    }
    return post_token(payload)


def refresh_once(refresh_token: str) -> dict[str, Any]:
    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    return post_token(payload)


def validate_access(access_token: str) -> dict[str, Any]:
    url = AUDIENCE.rstrip("/") + "/api/1/products"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers, timeout=30)
    try:
        data = response.json()
    except ValueError:
        fail(f"Tesla products 응답이 JSON이 아닙니다. HTTP {response.status_code}: {response.text[:300]}")
    if response.status_code >= 400:
        fail(f"Tesla products validation failed: HTTP {response.status_code} {json.dumps(data, ensure_ascii=False)}")
    return data


def save_files(token_data: dict[str, Any], token_file: Path, state_file: Path) -> None:
    refresh_token = token_data.get("refresh_token")
    access_token = token_data.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token.startswith(("NA_", "EU_", "CN_")):
        fail("Tesla 응답에 정상 refresh_token이 없습니다.")
    if not isinstance(access_token, str) or len(access_token) < 20:
        fail("Tesla 응답에 정상 access_token이 없습니다.")

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({"refresh_token": refresh_token}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)

    # Prime the LIGHT LOGGG state with the fresh access token so the next run can
    # validate immediately. refresh_once() is not called here because Tesla
    # refresh tokens are single-use and the polling script should own rotation.
    expires_in = int(token_data.get("expires_in") or 0)
    state = {
        "access_token": access_token,
        "access_token_expires_at": time.time() + max(60, expires_in - 120),
        "last_token_refresh_at": time.time(),
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(state_file, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tesla OAuth refresh token generator for LIGHT LOGGG")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE), help="refresh token JSON output path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="LIGHT LOGGG state JSON output path")
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="OAuth scope string")
    parser.add_argument("--code", default="", help="authorization code or full redirect URL")
    args = parser.parse_args()

    token_file = Path(args.token_file).expanduser()
    state_file = Path(args.state_file).expanduser()

    auth_url, expected_state = build_authorize_url(args.scope)
    print("\n아래 Tesla 로그인 주소를 브라우저에 붙여넣고 로그인/승인하세요.\n")
    print(auth_url)
    print("\n로그인 후 doyaulchoi.github.io 페이지로 이동하면, 브라우저 주소창의 전체 URL을 복사해서 아래에 붙여넣으세요.")
    print("주소에 code=... 가 포함되어 있어야 합니다. state 값은 내부 검증 참고값입니다:", expected_state)

    raw = args.code.strip() if args.code else input("\nredirect URL 또는 code 입력: ").strip()
    code = extract_code(raw)

    token_data = exchange_code(code, args.scope)
    products = validate_access(token_data["access_token"])
    save_files(token_data, token_file, state_file)

    count = len(products.get("response", [])) if isinstance(products.get("response"), list) else "unknown"
    print(f"ok=Tesla OAuth token saved token_file={token_file} state_file={state_file} products={count}")
    print("다음 명령으로 LIGHT LOGGG를 테스트하세요:")
    print("python ~/light_loggg_tesla/light_loggg_tesla_polling.py --once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
