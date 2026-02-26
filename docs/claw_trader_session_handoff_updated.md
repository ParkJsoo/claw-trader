# 🧠 Claw‑Trader Session Handoff (Updated)

---

## 🎯 Project Goal
- Objective: **Real profit generation**
- Mode: **Fully automated trading**
- Style: Aggressive / Short‑term / Momentum & Event‑driven

---

## 🚫 Hard Constraints
- No margin / leverage / credit trading
- No derivatives / futures
- Cash‑only risk model

---

## ✅ Completed Components

### 🔐 Security / Control
- Dedicated macOS account (environment isolation) ✅
- Telegram Control Plane (Allowlist + PIN) ✅
- Global Pause / Resume ✅
- Redis password protection ✅

---

### 🧱 Core Infrastructure
- Redis state store ✅ (Docker: claw-redis)
- Executor (Pause / Risk / Idempotency) ✅
- Reject / Audit logging ✅

---

### 🌍 Exchange Connectivity

🇰🇷 **KIS Client**
- kis_healthcheck.py ✅
- kis_order_smoke.py ✅

🇺🇸 **IBKR Client**
- ibkr_healthcheck.py ✅
- IBKR Gateway ping 연결 확인 ✅
- **reqMarketDataType(4) 적용** — Delayed Frozen 모드, Error 10089 해결 ✅
- **reconnect backoff 적용** — 지수 백오프 최대 60s, 실패 로깅 ✅
- available_cash=0 → ACCOUNT_SNAPSHOT_ERROR 처리 (3/3 입금 후 해결 예정) ⚠️

---

### 🔁 Order Lifecycle Control
- order_watcher.py 실행 가능 ✅
- TTL 기반 미체결 자동 취소 ✅
- Redis 주문 상태 기록 ✅

---

## ✅ Implemented Scripts

### Healthcheck
- scripts.kis_healthcheck ✅
- scripts.ibkr_healthcheck ✅

### Order Smoke Test
- scripts.kis_order_smoke ✅

### Signal Injection
- scripts.push_signal ✅ (US)
- scripts.push_signal_kr ✅ (KR)

### Watcher
- scripts.order_watcher ✅

### Portfolio (PHASE 4)
- scripts.query_positions ✅ — 포지션/PnL 조회
- scripts.position_engine ✅ — Fill 큐 소비 (선택, Watcher 내장 모드 기본)

---

## ⚙ Current Runtime Behaviour

✔ Signal → Strategy → Executor 파이프라인 정상
✔ Idempotency 정상 동작
✔ Risk Gate 정상 차단 (RiskEngine 5-rule gatekeeper)
✔ Strategy Filter 정상 동작 (StrategyEngine 3-rule filter)
✔ Reject Reason Redis 기록 (source: strategy / executor)
✔ Market Data Updater — 워치리스트 + 보유 포지션 심볼 현재가 폴링 + unrealized PnL 자동 갱신
✔ DataGuard — md:last_update stale 감지 (warn-only, hard_block 토글)
✔ AI Advisory — shadow mode 추천 기록 (파이프라인 영향 0)
✔ AI Signal Generator — KR/US 모두 정상 동작 ✅
✔ IbkrFeed — Delayed Frozen 모드 (reqMarketDataType(4)), AAPL/NVDA 가격 수신 확인 ✅
✔ Process Lock — gen:runner:lock (signal_generator_runner 중복 실행 방지) ✅
✔ Watcher 정상 대기
⚠️ claw:pause:global=true 설정 중 (실제 주문 차단 상태)

---

## 📊 System Status

**Current Phase:** 8 v3 Complete (IBKR Delayed Data + Process Lock)
**System Stability:** ⭐⭐⭐⭐⭐ Stable Foundation
**KR Pipeline:** ✅ 완전 동작 (장중 모멘텀 발생 시 신호 생성)
**US Pipeline:** ✅ Delayed Frozen 데이터 수신 중 (AAPL/NVDA 정상) — 인프라 검증 구간

---

## ✅ PHASE 8 v3 — IBKR Delayed Data + Process Lock 완료

### 구현 내역

**`src/market_data/ibkr_feed.py`**
- `reqMarketDataType(4)` — 연결 직후 Delayed Frozen 모드 설정
  - Error 10089 "market data subscription" 해결
  - 장외 시간에도 마지막 가격 반환
- reconnect 지수 백오프: 실패 시 2^n 초 대기, 최대 60s
- reconnect 로깅: 실패 횟수, 다음 백오프 시간, 재연결 성공 시 복구 메시지

**`src/app/signal_generator_runner.py`**
- 프로세스 락: `gen:runner:lock` SET NX EX 120
  - 이미 실행 중이면 exit(0)
  - 루프 시작마다 EXPIRE 120으로 TTL 갱신
  - finally 블록에서 락 삭제 (정상 종료 시 즉시 해제)

### 동작 확인 (이번 세션)
- US AAPL: 272.80 → 272.77 → 272.67 (Delayed Frozen 정상 수신) ✅
- US NVDA: 187.36 → 187.21 → 186.88 (Delayed Frozen 정상 수신) ✅
- KR 005930: mark_hist 134개 누적, AI no_emit=8 (장 마감 — 정상) ✅
- gen:runner:lock=1 — 프로세스 락 정상 동작 ✅

---

## ✅ PHASE 8 v2 — AI Signal Generator 완료

### 구현 내역
- `src/ai/generator.py` — AISignalGenerator (cold start 가드, 피처 계산, AI 호출, 감사 로그)
- `src/app/signal_generator_runner.py` — 독립 프로세스 (GEN_POLL_SEC=60, 워치리스트, exit(1) on no API key)
- `src/market_data/updater.py` — 워치리스트 심볼 폴링 지원 (extra_symbols 파라미터)
- `src/app/market_data_runner.py` — GEN_WATCHLIST_KR/US 읽어서 updater에 전달

### Risk Engine 개선
- `src/executor/risk.py` — available_cash <= 0을 ACCOUNT_SNAPSHOT_ERROR로 분리
  - meta에 equity/cash/currency 포함
  - except 블록 meta에 market/symbol/size_cash 추가

### generator.py 안전장치
- EXIT 신호: position:{market}:{symbol} qty 확인 후 없으면 no_position 차단
- daily_cap: INCR → 초과 시 DECR 롤백 (원자성 보장)
- daily_cap → cooldown 순서 (cap 초과 시 cooldown 키 소모 없음)
- stop_price: KR=원단위(Decimal("1")), US=센트단위(Decimal("0.01"))
- stop_price <= 0 방어 (stop_adjusted=True 플래그 감사 로그)
- signal에 ts_ms 추가 (프로젝트 전사 표준)

---

## 🔥 Immediate Next Priority

### 1. KR 장중 Feature 값 검증 (최우선)
- 장중(09:00~15:30 KST)에 반드시 확인
- `LRANGE mark_hist:KR:005930 0 5` → timestamp 증가 + price 변화 확인
- `ai:gen_stats:KR:{YYYYMMDD}` → no_emit 비율, reason 품질 확인
- ret_1m / ret_5m 이 0.0 고정이면 가격 갱신 문제

### 2. IBKR available_cash=0 해결 (3월 3일 입금 후)
- 입금 후 IBKR 계좌 관리에서 API 권한 확인
- Gateway → Trading Permissions → API 액세스 허용 여부
- `redis-cli SET claw:pause:global false` 후 US 실전 파이프라인 확인

### 3. claw:pause:global 해제 후 KR 실전 파이프라인 확인
- KR 장중에 모멘텀 발생 시 신호 생성 → Risk 통과 여부 확인
- Risk PAUSED 처리 확인 (주문 절대 안 나감)

### 4. 3/3 이후 — Delayed → Live 전환
- IBKR 라이브 시장 데이터 구독 활성화
- `reqMarketDataType(4)` → `reqMarketDataType(1)` 변경 (또는 삭제 — 라이브 기본값)
- US 실전 전략 검증 시작

---

## 🗓 운영 모드 (3/3 입금 전)

| 시장 | 모드 | 목적 |
|------|------|------|
| KR | 실전 품질 검증 | 전략/AI/Risk 검증, 장중 Feature 확인 |
| US | 인프라 검증 | Delayed Frozen 데이터 흐름, cold start 통과 확인 |

---

## 📈 Strategy Status
- Strategy Filter Engine ✅ — 3-rule filter (dedupe / cooldown / daily_cap)
- Signal Generation ✅ — AI Signal Generator v1 (Phase 8 v2)

---

## 🤖 AI / Signal Generator Status
- Phase 8 v1: Shadow Advisory ✅ — 파이프라인 영향 없이 추천 기록
- Phase 8 v2: AI Signal Generator ✅ — KR/US 모두 동작 확인
- Phase 8 v3: IBKR Delayed Data + Process Lock ✅
- 다음: AI 주도 신호 신뢰도 검증 후 실전 전환 (shadow 데이터 충분히 쌓인 후)

---

## 🧩 Redis Key Status

### Active Keys

**Control:**
- claw:pause:global — 현재 "true" 설정 중 (수동 주문 차단)

**Process Lock ✅**
- gen:runner:lock — signal_generator_runner 단일 실행 보장 (TTL 120s, 루프마다 갱신)

**Portfolio Engine (PHASE 4) ✅**
- position:{market}:{symbol}
- position_index:{market}
- pnl:{market}
- trade:{market}:{trade_id}
- claw:fill:queue

**Risk Engine (PHASE 5) ✅**
- claw:pause:reason
- claw:pause:meta
- claw:killswitch:{market}

**Strategy Engine (PHASE 6) ✅**
- strategy:dedupe:{market}:{signal_id}
- strategy:cooldown:{market}:{symbol}
- strategy:daily_count:{market}:{YYYYMMDD}
- strategy:pass_count:{market}:{YYYYMMDD}
- strategy:reject_count:{market}:{YYYYMMDD}

**Market Data (PHASE 7) ✅**
- mark:{market}:{symbol}
- md:error:{market}:{YYYYMMDD}
- md:last_update:{market}

**Market Data History (PHASE 8 v2) ✅**
- mark_hist:{market}:{symbol} — 최근 300개 (TTL 2d)

**AI Advisory (PHASE 8 v1) ✅**
- ai:advice:{market}:{signal_id}
- ai:advice_index:{market}:{YYYYMMDD}
- ai:advice_stats:{market}:{YYYYMMDD}

**AI Signal Generator (PHASE 8 v2) ✅**
- ai:gen:{market}:{signal_id}
- ai:gen_index:{market}:{YYYYMMDD}
- ai:gen_stats:{market}:{YYYYMMDD}
- gen:cooldown:{market}:{symbol}
- gen:daily_emit:{market}:{YYYYMMDD}

---

## 🚀 Guidance For Next Chat

Start From:

👉 **KR 장중 Feature 값 검증 → ai:gen_stats 확인 → 3/3 이후 IBKR 라이브 전환**

운영 루틴:
- `LRANGE mark_hist:KR:005930 0 5` — timestamp 증가 + price 변화 확인
- `HGETALL ai:gen_stats:KR:{YYYYMMDD}` — no_emit/generated/skip_* 분포
- `ZREVRANGE ai:gen_index:KR:{YYYYMMDD} 0 4` → 최근 신호 ID 추출
- `HGETALL ai:gen:{KR}:{signal_id}` → reason 품질, features_json 확인
- `GET md:last_update:KR` / `GET md:last_update:US` — stale 여부
- `GET gen:runner:lock` — 프로세스 락 생존 여부
- KR 장중(09:00~15:30 KST) / US 장중(23:30~06:00 KST)

Reference: docs/claw_trader_roadmap_v2.md, docs/execution_spec.md, docs/redis_keys.md

### 주요 구현 파일
- src/market_data/ibkr_feed.py — reqMarketDataType(4) + reconnect backoff
- src/ai/generator.py — AISignalGenerator
- src/app/signal_generator_runner.py — 프로세스 락 + 독립 프로세스
- src/market_data/updater.py — 워치리스트 폴링 지원
- src/app/market_data_runner.py — watchlist 주입

### 프로세스 기동 순서
```
cd src
python -m app.runner                   # 신호 처리 파이프라인
python -m app.market_data_runner       # 현재가 폴링 (워치리스트 포함)
python -m scripts.order_watcher        # 주문 상태 감시
python -m app.signal_generator_runner  # AI 신호 생성기 (프로세스 락 자동)
```

### .env 주요 변수
```
ANTHROPIC_API_KEY=sk-ant-...
GEN_WATCHLIST_KR=005930,000660
GEN_WATCHLIST_US=AAPL,NVDA
GEN_POLL_SEC=60
GEN_MAX_SIZE_CASH_KR=10000
GEN_MAX_SIZE_CASH_US=10
GEN_DAILY_EMIT_CAP=5
GEN_MIN_HIST=20
GEN_COOLDOWN_SEC=300
```

### 3/3 이후 체크리스트
```
1. IBKR 계좌 입금 확인
2. IBKR 라이브 시장 데이터 구독 (Market Data Connections)
3. ibkr_feed.py: reqMarketDataType(4) → reqMarketDataType(1) 또는 제거
4. redis-cli SET claw:pause:global false
5. KR/US 장중 실전 파이프라인 확인
```

---

**Claw‑Trader Engine:** Phase 8 v3 Complete — IBKR Delayed Data + Process Lock ✅
