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
- `reqMarketDataType(4)` — Delayed Frozen 모드, Error 10089 해결 ✅
- reconnect backoff 적용 — 지수 백오프 최대 60s, 실패 로깅 ✅
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
✔ Telegram Notifier — `guards/notifier.py` (자동 pause 시 알림) ✅
✔ AI 호출 하드 캡 — `ai:call_count` 일일 1000회 제한, 초과 시 auto-pause + TG ✅
✔ 비정상 자동 중지 — MD_STALE / MD_ERROR_SPIKE / AI_ERROR_SPIKE 감지 → auto-pause + TG ✅
✔ 10분 상태 요약 로그 — md_age / hist 길이 / md_err_delta / ai_stats / lock_ttl ✅
✔ TG 스팸 방지 — 이미 pause 상태에서 중복 알림 차단 ✅
✔ Watcher 정상 대기
⚠️ claw:pause:global=true 설정 중 (실제 주문 차단 상태)

---

## 📊 System Status

**Current Phase:** 8 v4 Complete (무인 운영 안전장치)
**System Stability:** ⭐⭐⭐⭐⭐ Production-Ready (pause 상태 — 무인 운영 중)
**Current Universe Mode:** Static (.env-based watchlist)
**Next Evolution:** AI-assisted candidate pool (Phase 10)
**KR Pipeline:** ✅ 완전 동작 (장중 모멘텀 발생 시 신호 생성)
**US Pipeline:** ✅ Delayed Frozen 데이터 수신 중 (AAPL/NVDA 정상)
**md:last_update age:** KR ~4s / US ~7s ✅
**Running Processes:** app.runner / app.market_data_runner / scripts.order_watcher / app.signal_generator_runner ✅
**gen:runner:lock TTL:** ~63s (정상 갱신 중) ✅

---

## ✅ PHASE 8 v4 — 무인 운영 안전장치 완료

### 구현 내역

**`src/guards/notifier.py`** (신규)
- `send_telegram(message)` — urllib 기반 Telegram 알림 (fire-and-forget)
- 실패 시 False 반환, 예외 전파 없음

**`src/ai/generator.py`** 변경
- `_GEN_DAILY_CALL_CAP=1000` — emit 여부와 무관한 AI API 호출 수 하드 캡
- `_set_auto_pause()` 메서드 — pause=true + reason/meta Redis 기록 + TG 알림
- `ai:call_count:{market}:{YYYYMMDD}` — API 호출 직전 INCR, 초과 시 자동 pause + return None

**`src/app/signal_generator_runner.py`** 변경
- 시작 시 `claw:pause:global` 확인 — false면 WARN 출력
- `_health_check()` — 10분마다 상태 로그 + 비정상 감지 (MD_STALE/MD_ERROR_SPIKE/AI_ERROR_SPIKE)
- `_do_auto_pause()` — pause=true + reason/meta + TG 알림 + 로그
- 이미 pause 상태면 anomaly 감지 시 상태 로그만, TG 중복 알림 없음
- pause 상태에서 AI 호출 없이 60s sleep (lock 하트비트 유지)

### 동작 확인 (Phase 8 v4 세션)
- MD_STALE(US) 자동 감지 + TG 알림 발송 확인 ✅
- pause 상태에서 AI 호출 0 확인 ✅
- TG 스팸 방지 수정 후 재확인 ✅
- gen:runner:lock TTL 갱신 정상 ✅

### GPT 피드백 검토 (2026-02-27)
- GPT가 TG 스팸 방지 코드 존재 여부 질문
- 확인 결과: signal_generator_runner.py:191에 `if anomalies and not _is_paused(r):` 가드 구현 완료 (방법 A)
- GPT도 "지금 상태로 무인 운영 진행해도 된다" 결론
- 프로세스 재기동 후 전체 상태 정상 확인 (KR 4s / US 7s / lock TTL 63s)

---

## ✅ PHASE 8 v3 — IBKR Delayed Data + Process Lock 완료

**`src/market_data/ibkr_feed.py`**
- `reqMarketDataType(4)` — Delayed Frozen 모드
- reconnect 지수 백오프 (최대 60s)

**`src/app/signal_generator_runner.py`**
- `gen:runner:lock` SET NX EX 120 + 루프마다 갱신 + finally 해제

---

## ✅ PHASE 8 v2 — AI Signal Generator 완료

- `src/ai/generator.py` — AISignalGenerator
- `src/app/signal_generator_runner.py` — 독립 프로세스
- `src/market_data/updater.py` — 워치리스트 폴링 지원
- `src/app/market_data_runner.py` — watchlist 주입

---

## 🔥 Immediate Next Priority

### 1. KR 장중 Feature 값 검증 (최우선)
- 장중(09:00~15:30 KST)에 반드시 확인
- `LRANGE mark_hist:KR:005930 0 5` → timestamp 증가 + price 변화
- `HGETALL ai:gen_stats:KR:{YYYYMMDD}` → no_emit/generated 비율, reason 품질
- ret_1m/ret_5m 0.0 고정이면 가격 갱신 문제

### 2. claw:pause:global 해제 후 KR 실전 파이프라인 확인
- KR 장중에 모멘텀 발생 시 신호 생성 → Risk 통과 여부 확인
- `docker exec claw-redis redis-cli -a henry0308 SET claw:pause:global false`

### 3. IBKR available_cash=0 해결 (3월 3일 입금 후)
- 입금 후 IBKR 계좌 API 권한 확인
- `reqMarketDataType(4)` → `reqMarketDataType(1)` 변경 (라이브 전환)

---

## 🗓 운영 모드 (3/3 입금 전)

| 시장 | 모드 | 목적 |
|------|------|------|
| KR | 실전 품질 검증 | 전략/AI/Risk 검증, 장중 Feature 확인 |
| US | 인프라 검증 | Delayed Frozen 데이터 흐름, cold start 통과 |

---

## 🧩 Redis Key Status

**Control:**
- `claw:pause:global` — 현재 "true" (수동 주문 차단)
- `claw:pause:reason` — 자동 pause 사유
- `claw:pause:meta` — 자동 pause 상세 (market/detail/ts_ms/source)

**Process Lock ✅**
- `gen:runner:lock` — TTL 120s, 루프마다 갱신

**AI Call Count (Phase 8 v4) ✅**
- `ai:call_count:{market}:{YYYYMMDD}` — API 호출 수 (TTL 3d)

**Portfolio Engine (PHASE 4) ✅**
- position:{market}:{symbol} / position_index:{market} / pnl:{market}

**Risk Engine (PHASE 5) ✅**
- claw:pause:reason / claw:pause:meta / claw:killswitch:{market}

**Strategy Engine (PHASE 6) ✅**
- strategy:dedupe/cooldown/daily_count/pass_count/reject_count

**Market Data (PHASE 7) ✅**
- mark:{market}:{symbol} / md:error:{market}:{YYYYMMDD} / md:last_update:{market}

**Market Data History (PHASE 8 v2) ✅**
- mark_hist:{market}:{symbol} — 최근 300개 (TTL 2d)

**AI Signal Generator (PHASE 8 v2) ✅**
- ai:gen:{market}:{signal_id} / ai:gen_index / ai:gen_stats
- gen:cooldown:{market}:{symbol} / gen:daily_emit:{market}:{YYYYMMDD}

---

## 🚀 Guidance For Next Chat

Start From:

👉 **KR 장중(09:00~15:30 KST) Feature 값 검증 → ai:gen_stats 확인 → 3/3 이후 IBKR 라이브 전환**

운영 루틴:
```bash
# md 신선도
docker exec claw-redis redis-cli -a henry0308 GET md:last_update:KR
docker exec claw-redis redis-cli -a henry0308 GET md:last_update:US

# 가격 변화 확인
docker exec claw-redis redis-cli -a henry0308 LRANGE mark_hist:KR:005930 0 4

# AI 통계
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:gen_stats:KR:$(date +%Y%m%d)

# AI 호출 수
docker exec claw-redis redis-cli -a henry0308 GET ai:call_count:KR:$(date +%Y%m%d)

# 프로세스 락
docker exec claw-redis redis-cli -a henry0308 TTL gen:runner:lock
```

### 주요 구현 파일
- src/guards/notifier.py — Telegram 알림
- src/ai/generator.py — AI 호출 캡 + auto-pause
- src/app/signal_generator_runner.py — 헬스 모니터 + auto-pause + TG 스팸 방지
- src/market_data/ibkr_feed.py — Delayed Frozen + reconnect backoff

### 프로세스 기동 순서
```
cd src
python -m app.runner                   # 신호 처리 파이프라인
python -m app.market_data_runner       # 현재가 폴링
python -m scripts.order_watcher        # 주문 감시
python -m app.signal_generator_runner  # AI 신호 생성기 (락 + 헬스 모니터)
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
GEN_DAILY_CALL_CAP=1000    # AI API 호출 수 하드 캡
GEN_STATUS_LOG_SEC=600     # 상태 로그 간격 (기본 10분)
GEN_MD_STALE_SEC=180       # md stale 임계값 (초)
GEN_MD_ERROR_SPIKE=50      # md 오류 급증 임계값 (인터벌당)
GEN_AI_ERROR_SPIKE=10      # AI 오류 급증 임계값 (인터벌당)
```

### Watchlist Operational Rule

현재 운영 모드에서:

- 신규 진입(New Entry)은 `GEN_WATCHLIST_*`에 정의된 종목에 한정된다.
- 이미 보유 중인 포지션은 watchlist 여부와 관계없이 관리된다.
- watchlist 변경은 전략 변경 이벤트로 간주한다.
- 무인 운영 중에는 watchlist를 변경하지 않는다.

### 3/3 이후 체크리스트
```
1. IBKR 계좌 입금 확인
2. IBKR 라이브 시장 데이터 구독 (Market Data Connections)
3. ibkr_feed.py: reqMarketDataType(4) → reqMarketDataType(1) 또는 제거
4. docker exec claw-redis redis-cli -a <pw> SET claw:pause:global false
5. KR/US 장중 실전 파이프라인 확인
```

---

**Claw‑Trader Engine:** Phase 8 v4 Complete — 무인 운영 안전장치 ✅
