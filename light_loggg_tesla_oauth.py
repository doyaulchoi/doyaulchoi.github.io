#!/usr/bin/env python3
"""
Tesla Fleet API OAuth helper for LIGHT LOGGG.

역할:
- Tesla authorization_code flow로 사용자 Fleet API token 발급
- refresh_token을 ~/.light_loggg_tesla_tokens.json 에 저장
- access_token을 ~/.light_loggg_state.json 에 저장해서 다음 polling이 바로 사용 가능하게 함
- 기존 refresh_token 갱신 테스트 지원

주의:
- client_secret, token, refresh_token은 GitHub에 올리지 않는다.
- 민감값은 ~/.light_loggg.env 또는 로컬 token/state 파일에만 둔다.
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
from typing import Any, Dict, Optional

import requests


# =========================
# Defaults
# =========================

DEFAULT_CLIENT_ID = "d1351a7e-42fd-4318-b6a2-c9d702af75c1"
DEFAULT_REDIRECT_URI = "https://doyaulchoi.github.io/index.html"
DEFAULT_API_BASE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
DEFAULT_AUTH_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
DEFAULT_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"

DEFAULT_SCOPE = "openid offline_access user_data vehicle_device_data vehicle_location"

DEFAULT_ENV_FILE = Path.home() / ".light_loggg.env"
DEFAULT_TOKEN_FILE = Path.home() / ".light_loggg_tesla_tokens.json"
DEFAULT_STATE_FILE = Path.home() / ".light_loggg_state.json"

REQUEST_TIMEOUT = int(os.getenv("LIGHT_LOGGG_REQUEST_TIMEOUT", "30"))


# =========================
# Env / IO helpers
# =========================

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


def fail(message: str, code: int = 1) -> None:
    print(f"error={message}", file=sys.stderr)
    raise SystemExit(code)


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def atomic_write_json(path: Path, payload: Dict[str, Any], chmod_600: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)

    if chmod_600:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


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


# =========================
# Tesla OAuth client
# =========================

class TeslaOAuthClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        api_base: str,
        auth_url: str,
        token_url: str,
        scope: str,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.api_base = api_base.rstrip("/")
        self.auth_url = auth_url
        self.token_url = token_url
        self.scope = scope
        self.session = requests.Session()

    def build_authorize_url(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)

        params = {
            "client_id": self.client_id,
            "locale": "ko-KR",
            "prompt": "login",
            "prompt_missing_scopes": "true",
            "require_requested_scopes": "true",
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": state,
        }

        return (
            self.auth_url
            + "?"
            + urllib.parse.urlencode(params, quote_via=urllib.parse.quote),
            state,
        )

    def post_token(self, payload: Dict[str, str]) -> Dict[str, Any]:
        response = self.session.post(
            self.token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )

        try:
            data = response.json()
        except ValueError:
            fail(f"Tesla token 응답이 JSON이 아닙니다. HTTP {response.status_code}: {response.text[:300]}")

        if response.status_code >= 400:
            fail(
                "Tesla token request failed: "
                f"HTTP {response.status_code} {json.dumps(data, ensure_ascii=False)}"
            )

        if not isinstance(data, dict):
            fail("Tesla token 응답 JSON 최상위가 object가 아닙니다.")

        return data

    def exchange_code(self, code: str) -> Dict[str, Any]:
        if not self.client_id:
            fail("TESLA_CLIENT_ID가 비어 있습니다.")

        if not self.client_secret:
            fail("TESLA_CLIENT_SECRET가 비어 있습니다. authorization_code 교환에는 client_secret이 필요합니다.")

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "audience": self.api_base,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
        }

        return self.post_token(payload)

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        if not refresh_token:
            fail("refresh_token이 비어 있습니다.")

        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
            "scope": self.scope,
        }

        if self.client_secret:
            payload["client_secret"] = self.client_secret

        return self.post_token(payload)

    def validate_products(self, access_token: str) -> Dict[str, Any]:
        url = self.api_base + "/api/1/products"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        response = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        try:
            data = response.json()
        except ValueError:
            fail(f"Tesla products 응답이 JSON이 아닙니다. HTTP {response.status_code}: {response.text[:300]}")

        if response.status_code >= 400:
            fail(
                "Tesla products validation failed: "
                f"HTTP {response.status_code} {json.dumps(data, ensure_ascii=False)}"
            )

        if not isinstance(data, dict):
            fail("Tesla products 응답 JSON 최상위가 object가 아닙니다.")

        return data


# =========================
# Token save / validation
# =========================

def validate_token_response(token_data: Dict[str, Any], require_refresh: bool = True) -> tuple[str, Optional[str], int]:
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = int(token_data.get("expires_in") or 0)

    if not isinstance(access_token, str) or len(access_token) < 20:
        fail("Tesla 응답에 정상 access_token이 없습니다.")

    if require_refresh:
        if not isinstance(refresh_token, str) or len(refresh_token) < 20:
            fail("Tesla 응답에 정상 refresh_token이 없습니다.")

    if expires_in <= 0:
        fail("Tesla 응답에 정상 expires_in이 없습니다.")

    return access_token, refresh_token if isinstance(refresh_token, str) else None, expires_in


def save_tokens(
    token_data: Dict[str, Any],
    token_file: Path,
    state_file: Path,
    existing_refresh_token: Optional[str] = None,
) -> None:
    access_token, new_refresh_token, expires_in = validate_token_response(
        token_data,
        require_refresh=existing_refresh_token is None,
    )

    refresh_token_to_save = new_refresh_token or existing_refresh_token

    if not refresh_token_to_save:
        fail("저장할 refresh_token이 없습니다.")

    saved_at = time.time()
    expires_at = saved_at + max(60, expires_in - 120)

    # token 파일에는 refresh_token만 저장한다.
    atomic_write_json(
        token_file,
        {
            "refresh_token": refresh_token_to_save,
            "saved_at": saved_at,
            "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )

    # state 파일에는 access_token 및 만료 시각을 저장한다.
    state = load_json(state_file)

    state["access_token"] = access_token
    state["access_token_expires_at"] = expires_at
    state["last_token_refresh_at"] = saved_at
    state["token_saved_at"] = saved_at
    state["token_saved_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    atomic_write_json(state_file, state)

    print(f"ok=token_saved token_file={token_file} state_file={state_file}")
    print(f"access_token_length={len(access_token)}")
    print(f"refresh_token_length={len(refresh_token_to_save)}")
    print(f"expires_in={expires_in}")
    print(f"expires_at_unix={int(expires_at)}")


def get_existing_refresh_token(token_file: Path) -> Optional[str]:
    data = load_json(token_file)
    token = data.get("refresh_token")

    if isinstance(token, str) and len(token) > 20:
        return token

    return None


# =========================
# CLI
# =========================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tesla OAuth helper for LIGHT LOGGG")

    parser.add_argument("--code", default="", help="authorization code 또는 전체 redirect URL")
    parser.add_argument("--refresh-test", action="store_true", help="기존 refresh_token으로 갱신 테스트 후 저장")
    parser.add_argument("--no-env", action="store_true", help="~/.light_loggg.env 로드하지 않음")

    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE), help="refresh token JSON path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="LIGHT LOGGG state JSON path")

    parser.add_argument("--client-id", default="", help="Tesla client id. 기본값은 env TESLA_CLIENT_ID")
    parser.add_argument("--client-secret", default="", help="Tesla client secret. 기본값은 env TESLA_CLIENT_SECRET")
    parser.add_argument("--redirect-uri", default="", help="Tesla redirect URI. 기본값은 env TESLA_REDIRECT_URI")
    parser.add_argument("--api-base", default="", help="Tesla Fleet API base URL. 기본값은 env TESLA_API_BASE")
    parser.add_argument("--scope", default="", help="OAuth scope string. 기본값은 env TESLA_SCOPE")

    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    if not args.no_env:
        load_dotenv(DEFAULT_ENV_FILE)

    token_file = Path(args.token_file).expanduser()
    state_file = Path(args.state_file).expanduser()

    client_id = args.client_id or os.getenv("TESLA_CLIENT_ID") or DEFAULT_CLIENT_ID
    client_secret = args.client_secret or os.getenv("TESLA_CLIENT_SECRET") or ""
    redirect_uri = args.redirect_uri or os.getenv("TESLA_REDIRECT_URI") or DEFAULT_REDIRECT_URI
    api_base = args.api_base or os.getenv("TESLA_API_BASE") or DEFAULT_API_BASE
    scope = args.scope or os.getenv("TESLA_SCOPE") or DEFAULT_SCOPE

    client = TeslaOAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        api_base=api_base,
        auth_url=DEFAULT_AUTH_URL,
        token_url=DEFAULT_AUTH_TOKEN_URL,
        scope=scope,
    )

    print("=== Tesla OAuth helper for LIGHT LOGGG ===")
    print(f"client_id={mask(client_id)} length={len(client_id)}")
    print(f"client_secret={'set' if bool(client_secret) else 'missing'} length={len(client_secret)}")
    print(f"redirect_uri={redirect_uri}")
    print(f"api_base={api_base}")
    print(f"scope={scope}")
    print(f"token_file={token_file}")
    print(f"state_file={state_file}")

    if args.refresh_test:
        existing_refresh = get_existing_refresh_token(token_file)

        if not existing_refresh:
            fail(f"기존 refresh_token이 없습니다: {token_file}")

        print("기존 refresh_token으로 refresh 테스트를 실행합니다.")
        token_data = client.refresh_token(existing_refresh)

        access_token, new_refresh_token, _expires_in = validate_token_response(
            token_data,
            require_refresh=False,
        )

        products = client.validate_products(access_token)

        # Tesla refresh_token이 single-use인 경우를 대비해 응답에 새 refresh_token이 있으면 반드시 저장.
        save_tokens(
            token_data,
            token_file=token_file,
            state_file=state_file,
            existing_refresh_token=existing_refresh,
        )

        count = len(products.get("response", [])) if isinstance(products.get("response"), list) else "unknown"
        print(f"ok=refresh_test products={count}")
        return 0

    auth_url, expected_state = client.build_authorize_url()

    print("\n아래 Tesla 로그인 주소를 브라우저에 붙여넣고 로그인/승인하세요.\n")
    print(auth_url)
    print("\n로그인 후 doyaulchoi.github.io 페이지로 이동하면, 브라우저 주소창의 전체 URL을 복사해서 아래에 붙여넣으세요.")
    print("주소에 code=... 가 포함되어 있어야 합니다.")
    print(f"state 참고값: {expected_state}")

    raw = args.code.strip() if args.code else input("\nredirect URL 또는 code 입력: ").strip()
    code = extract_code(raw)

    token_data = client.exchange_code(code)

    access_token, _refresh_token, _expires_in = validate_token_response(
        token_data,
        require_refresh=True,
    )

    products = client.validate_products(access_token)

    save_tokens(
        token_data,
        token_file=token_file,
        state_file=state_file,
        existing_refresh_token=None,
    )

    count = len(products.get("response", [])) if isinstance(products.get("response"), list) else "unknown"

    print(f"ok=authorization_code_flow products={count}")
    print("\n다음 명령으로 polling 1회 테스트:")
    print("python ~/light_loggg_tesla/light_loggg_tesla_polling.py --once")
    print("\n문제 있으면 진단:")
    print("python ~/light_loggg_tesla/check_system.py")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
