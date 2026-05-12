# LIGHT LOGGG Tesla 폴링 방식 적용 안내

## 개요

기존 구성은 Tesla Fleet Telemetry 서버를 외부에 공개하고 Tesla 개발자 포털에서 telemetry 서버를 등록하는 방식이었다. 이번 구성은 그 경로를 사용하지 않고, **TeslaMate에서 검증된 Owner/Fleet API 폴링 구조**를 경량 Python 스크립트로 단순화한 방식이다. 따라서 미패드 Termux에서 단일 프로세스를 실행하면 차량 목록 조회, 토큰 갱신, 차량 상태 확인, 주행 중 전비 계산, 텔레그램 알림까지 처리한다.

## 핵심 동작

| 구분 | 동작 |
| --- | --- |
| asleep 또는 offline | `vehicle_data`를 호출하지 않고 5분 후 재확인한다. |
| online 대기 | 60초 간격으로 상태를 확인한다. |
| 주행 중 | 10초 간격으로 상세 데이터를 가져와 최근 3분 전비를 계산한다. |
| 충전 중 | 60초 간격으로 상태를 확인한다. |
| 토큰 만료 | 401 응답 또는 시작 시 필요할 때 refresh token으로 새 access token을 발급한다. |
| 알림 | 최근 전비가 기준값보다 낮으면 텔레그램으로 경고하고, 21시 이후 일일 요약을 보낸다. |

## 미패드 Termux 설치

아래 명령을 Termux에서 실행한다.

```bash
cd ~
wget -O setup_light_loggg_tesla_polling.sh https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/setup_light_loggg_tesla_polling.sh
bash setup_light_loggg_tesla_polling.sh
```

설치 후 `~/.light_loggg.env`를 열어 텔레그램 봇 토큰과 채팅 ID를 입력한다. Tesla refresh token은 `~/.light_loggg_tesla_tokens.json`의 `refresh_token` 값에 넣는다. 이 두 파일은 GitHub에 올리지 않는다.

## 실행

1회 테스트는 다음 명령으로 수행한다.

```bash
python ~/light_loggg_tesla/light_loggg_tesla_polling.py --once
```

상시 실행은 다음 명령으로 시작한다.

```bash
~/light_loggg_tesla/run.sh
```

## 설정값

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 없음 | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 없음 | 알림 받을 채팅 ID |
| `TESLA_VIN` | 없음 | 차량이 여러 대일 때 대상 VIN |
| `TESLA_API_BASE` | `https://fleet-api.prd.na.vn.cloud.tesla.com` | Tesla Fleet API 지역 엔드포인트 |
| `LIGHT_LOGGG_THRESHOLD_KM_PER_KWH` | `4.5` | 전비 경고 기준 |
| `LIGHT_LOGGG_WINDOW_MINUTES` | `3` | 최근 전비 계산 창 |
| `HOME_LAT`, `HOME_LON` | 없음 | 18시 이후 집 도착 요약에 사용할 좌표 |

## 확인된 상태

현재 저장된 Tesla refresh token으로 `products` 조회는 성공했으며, 차량 `두삼이`가 `online` 상태로 확인되었다. 새 스크립트의 `--once` 실행도 정상적으로 완료되었다.
