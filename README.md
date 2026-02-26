# Claw-Trader

현금 전용 완전 자동매매 엔진. 한국(KIS) 및 미국(IBKR) 주식 시장 지원.

---

## 특징

- **AI 신호 생성** — Anthropic Claude 기반 모멘텀 피처 분석 후 매매 신호 생성
- **이중 시장** — KR (KIS API) / US (IBKR TWS API) 동시 운영
- **현금 전용 리스크 모델** — 마진/레버리지/파생 거래 없음
- **완전 자동화** — 신호 생성 → 리스크 검증 → 주문 실행 → 상태 감시 파이프라인
- **Telegram 제어판** — 원격 일시정지/재개, 허용 사용자 PIN 인증

---

## 아키텍처

```
AI Signal Generator
       ↓
signal:queue (Redis)
       ↓
Strategy Engine (dedupe / cooldown / daily_cap)
       ↓
Risk Engine (5-rule gatekeeper)
       ↓
Executor (idempotency / audit log)
       ↓
KIS / IBKR Order API
       ↓
Order Watcher (TTL 기반 미체결 취소)
       ↓
Portfolio Engine (포지션 / PnL)
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| 상태 저장소 | Redis 7 (Docker) |
| KR 거래소 | KIS OpenAPI |
| US 거래소 | IBKR TWS API (ib_insync) |
| AI | Anthropic Claude (claude-haiku-4-5) |
| 알림 | Telegram Bot API |

---

## 실행 방법

```bash
# 1. 환경변수 설정
cp .env.example .env
# .env 파일에 실제 값 입력

# 2. 의존성 설치
python -m venv venv
source venv/bin/activate
pip install -r src/requirements.txt

# 3. Redis 실행 (Docker)
docker run -d --name claw-redis -p 6379:6379 redis:7-alpine \
  redis-server --requirepass <password>

# 4. 프로세스 기동 (순서 중요)
cd src
python -m app.runner                   # 신호 처리 파이프라인
python -m app.market_data_runner       # 현재가 폴링
python -m scripts.order_watcher        # 주문 감시
python -m app.signal_generator_runner  # AI 신호 생성기
```

---

## 주요 환경변수

`.env.example` 참조. 필수 값:

- `ANTHROPIC_API_KEY` — AI 신호 생성
- `KIS_APP_KEY` / `KIS_APP_SECRET` — 한국 거래소
- `IBKR_ACCOUNT_ID` — 미국 거래소 (IBKR Gateway 별도 실행 필요)
- `REDIS_URL` — Redis 연결
- `TG_BOT_TOKEN` — Telegram 제어판

---

## 운영 안전장치

- `claw:pause:global` — 전역 일시정지 (Redis 키)
- `claw:killswitch:{market}` — 시장별 킬스위치
- `gen:runner:lock` — AI 신호 생성기 중복 실행 방지
- Risk Engine 5-rule 게이트키퍼 (PAUSED / DUPLICATE_POSITION / MAX_CONCURRENT / KILLSWITCH / ALLOCATION_CAP)
- Strategy Engine 3-rule 필터 (dedupe / cooldown / daily_cap)

---

## 라이선스

Private
