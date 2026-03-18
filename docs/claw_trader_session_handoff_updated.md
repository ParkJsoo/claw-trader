# 🧠 Claw‑Trader Session Handoff (Updated 2026-03-18)

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

## 📊 Current Phase: **12 — 자동 매도** (2026-03-18 구현 완료)

### Phase 11 결과 (Day 1: 2026-03-18)
| 지표 | 결과 | 목표 |
|------|------|------|
| execution_rate | **62.5%** ✅ | ≥ 10% |
| executed | 13건 | — |
| COOLDOWN reject | 6 | — |
| MAX_CONCURRENT reject | 4 | — |
| pipeline_error | 0 ✅ | 0 |

### Phase 12 목표 & 현황
- **목표**: 매수 후 자동 매도로 단타 loop 완성 (매수만 하던 문제 해결)
- **구현 완료**: position_exit_runner — 30분 time_limit 자동 매도 검증 완료 ✅
  - 015760(한국전력), 034220(LG디스플레이) 자동 매도 성공

---

## ✅ Phase 12 구현 완료 (2026-03-18) — 자동 매도

### position_exit_runner (신규)

**`src/app/position_exit_runner.py`**
- KIS `get_kr_holdings()` → Redis `position:KR:{symbol}` 동기화 (30초마다)
- exit 조건 감시: stop_loss -2%, take_profit +2%, time_limit 1800s
- 조건 충족 시 SELL limit 주문 (mark_price 기준) — global pause 무시
- 중복 방지: `claw:exit_lock:KR:{symbol}` SET NX TTL 60s

**`src/exchange/kis/client.py`**
- `get_kr_holdings()` 추가: TTTC8434R output1 → `[{symbol, qty, avg_price}, ...]`
- `_refresh_token()` RuntimeError 래핑 (app_secret 노출 방지)

**`config/phase10_kr_micro.env`**
```
EXIT_POLL_SEC=30
EXIT_STOP_LOSS_PCT=0.02
EXIT_TAKE_PROFIT_PCT=0.02
EXIT_TIME_LIMIT_SEC=1800
WATCHER_TTL_CANCEL_SEC=60
```

Redis 키 추가:
```
position_index:KR                    # 보유 종목 Set (TTL 7d)
position:KR:{symbol}                 # qty / avg_price / opened_ts / updated_ts
claw:exit_lock:KR:{symbol}           # TTL 60s — 중복 매도 방지
exit_runner:lock                     # 단일 프로세스 보장
claw:order_meta:KR:{order_id}        # exit 매도 주문 메타
```

---

## ✅ Phase 11 구현 완료 (2026-03-17)

### Step 1: Execution drop reason 로그 추가

**`src/app/runner.py`**
- `_record_funnel()` 추가 — `execution_funnel:{market}:{date}` hash (TTL 7d)
- strategy reject 시 `strategy_reject:{REASON}` 카운트
- execute_signal() ERROR 시 `risk_reject` 카운트
- 성공 시 `executed` 카운트

**`src/executor/risk.py`**
- `_record_reject_counter()` 추가 — `risk:reject_count:{market}:{date}` hash (TTL 7d)
- `check()` reject 시마다 호출 (MAX_CONCURRENT, DAILY_LOSS, ALLOCATION_CAP 등)

Redis 키:
```
execution_funnel:KR:{YYYYMMDD}     # strategy_reject:{REASON} / risk_reject / executed
risk:reject_count:KR:{YYYYMMDD}    # MAX_CONCURRENT / DAILY_LOSS / ALLOCATION_CAP 등
```

관찰 명령:
```bash
docker exec claw-redis redis-cli -a "$REDIS_PASS" HGETALL execution_funnel:KR:$(date +%Y%m%d)
docker exec claw-redis redis-cli -a "$REDIS_PASS" HGETALL risk:reject_count:KR:$(date +%Y%m%d)
```

---

### Step 2: Symbol-level cooldown (Phase 11 신규)

**`src/app/consensus_signal_runner.py`**
- `_SYMBOL_COOLDOWN_SEC = int(os.getenv("CONSENSUS_SYMBOL_COOLDOWN_SEC", "180"))`
- direction check 직후 `consensus:symbol_cooldown:{market}:{symbol}` SET NX EX 180
- 쿨다운 내 재emit 차단 → `reject_symbol_cooldown` 카운트

Redis 키:
```
consensus:symbol_cooldown:KR:{symbol}  # TTL = 180s (CONSENSUS_SYMBOL_COOLDOWN_SEC)
```

---

### Step 3: ret_5m threshold 강화

**`src/app/consensus_signal_runner.py`**
- `_MIN_RET_5M = float(os.getenv("CONSENSUS_MIN_RET_5M", "0.001"))`
- 기존 `ret_5m > 0` → `ret_5m > 0.001` (애매한 상승 제거)

**`config/phase10_kr_micro.env`**
- `CONSENSUS_MIN_RET_5M=0.001` 추가

---

### Step 4: AI prefilter (ret_1m) 추가

**`src/app/ai_dual_eval_runner.py`**
- `_eval_symbol()` 내 AI 호출 전 ret_1m 체크 추가
- `ret_1m < -0.005` (1분 수익률 -0.5% 이하) → AI 호출 skip
- `skip_prefilter_ret1m` 통계 기록

---

### Step 5: 파라미터 최적화

**`config/phase10_kr_micro.env`**
- `STRATEGY_KR_COOLDOWN_SEC`: 600 → **300** (단타 최적화)
- `CONSENSUS_POLL_SEC`: 30 → **60** (중복 emit 감소)
- `CONSENSUS_SYMBOL_COOLDOWN_SEC=180` 추가
- `CONSENSUS_MIN_RET_5M=0.001` 추가

---

### 테스트
- 전체: **118개 all pass** (Phase 11 cooldown 테스트 포함)
- `tests/test_consensus_signal_runner.py::TestRunOnceDedup::test_new_eval_result_is_pushed`
  - 두 번째 호출 전 `r.delete("consensus:symbol_cooldown:KR:005930")` 추가 (cooldown 해제 시뮬레이션)

---

## ✅ 완성된 인프라 전체

### 🔐 Security / Control
- Dedicated macOS account (environment isolation) ✅
- Telegram Control Plane (Allowlist + PIN) ✅
- Global Pause / Resume ✅
- Redis password protection ✅

### 🧱 Core Infrastructure
- Redis state store ✅ (Docker: claw-redis)
- Executor (Pause / Risk / Idempotency) ✅
- Reject / Audit logging ✅

### 🌍 Exchange Connectivity
- **KIS Client** (KR): 토큰 자동 갱신(401), Redis 캐싱(403 tokenP 해결) ✅
- **IBKR Client** (US): Delayed Frozen 모드(reqMarketDataType=4), reconnect backoff ✅

### 🔁 Order Lifecycle
- order_watcher.py — TTL 기반 미체결 자동 취소 ✅
- Redis 주문 상태 기록 ✅

### 🤖 AI Pipeline
- AISignalGenerator (ai/generator.py) — 일일 호출 캡 + auto-pause ✅
- Claude Provider + Qwen Provider (providers/) ✅
- Dual Eval Runner (ai_dual_eval_runner.py) — Phase 9.5 ✅
- **consensus_signal_runner.py** — Phase 10 핵심: dual→Signal→queue ✅
- AIAdvisor (shadow mode, 파이프라인 영향 0) ✅

### 📰 News Intelligence
- src/news/ — DART + Google RSS + Yahoo Finance 수집/분류/저장 ✅
- **판단 통합: Phase 11 이후 예정** (현재 수집만)

### 📊 Monitoring
- DataGuard — md:last_update stale 감지 ✅
- execution_funnel 로그 ✅ (Phase 11 신규)
- risk:reject_count 로그 ✅ (Phase 11 신규)
- TG 봇 (`/claw status/ai-status/news/help`) ✅

---

## 🗄️ Redis Key 맵 (전체)

**Control:**
```
claw:pause:global              # "true"/"false"
claw:pause:reason              # 자동 pause 사유
claw:pause:meta                # 상세 (market/detail/ts_ms/source)
```

**Process Lock:**
```
consensus:runner:lock          # TTL 120s
dual:runner:lock               # TTL 300s
app:runner:lock                # TTL 30s
gen:runner:lock                # TTL 120s
eval:runner:lock               # TTL 300s
```

**AI Dual Eval (Phase 9.5+):**
```
ai:dual:last:{provider}:{market}:{symbol}   # 최신 판단 (claude/qwen)
ai:dual_log:{provider}:{market}:{YYYYMMDD}  # 일별 로그
ai:dual_stats:{provider}:{market}:{YYYYMMDD}
ai:dual_call_count:{market}:{YYYYMMDD}      # 라운드 캡
ai:dual_compare:{market}:{YYYYMMDD}
```

**Consensus Signal Runner (Phase 10+):**
```
consensus:stats:KR:{YYYYMMDD}              # candidate/reject 카운트
consensus:daily_count:KR:{YYYYMMDD}
consensus:audit:KR:{signal_id}             # TTL 7d
consensus:seen:{market}:{symbol}:{c_ts}:{q_ts}  # dedup (TTL 6*POLL_SEC)
consensus:symbol_cooldown:KR:{symbol}      # Phase 11: TTL 180s
```

**Execution Funnel (Phase 11 신규):**
```
execution_funnel:{market}:{YYYYMMDD}       # strategy_reject:{REASON} / risk_reject / executed
risk:reject_count:{market}:{YYYYMMDD}      # MAX_CONCURRENT / DAILY_LOSS 등
```

**Portfolio / Risk / Strategy:**
```
position:{market}:{symbol}
claw:killswitch:{market}
strategy:cooldown:{market}:{symbol}
strategy:daily_count:{market}:{YYYYMMDD}
mark:{market}:{symbol}
mark_hist:{market}:{symbol}               # 최근 300개 (TTL 2d)
```

---

## 🚀 프로세스 기동 순서 (Phase 12, 프로젝트 루트에서)

```bash
# ⚠️ 반드시 set -a 사용
set -a && source .env && source config/phase10_kr_micro.env && set +a

# 프로세스 종료 (재시작 시)
pkill -f "python.*-m app" 2>/dev/null; pkill -f "python.*-m scripts" 2>/dev/null; sleep 2

# 기동 (10개)
cd /Users/henry_oc/develop/claw-trader
PYTHONPATH=src venv/bin/python -m app.runner >> logs/runner.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.market_data_runner >> logs/market_data.log 2>&1 &
PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60 PYTHONPATH=src venv/bin/python -m scripts.order_watcher >> logs/order_watcher.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.signal_generator_runner >> logs/signal_generator.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_eval_runner >> logs/ai_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner >> logs/ai_dual_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner >> logs/consensus_signal.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.openclaw_bot >> logs/openclaw_bot.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.news_runner >> logs/news_runner.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.position_exit_runner >> logs/position_exit.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.position_engine >> logs/position_engine.log 2>&1 &
```
> ⚠️ order_watcher: `PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60` 필수 (없으면 ttl=15s로 기동됨)
> ⚠️ position_engine: fill queue 소비 프로세스 — 미기동 시 PnL 기록 안 됨

**기동 직후 확인:**
```bash
# runner config 확인 (cooldown=300s, daily_cap=40)
tail -5 logs/runner.log

# order_watcher TTL 확인 (ttl_cancel=60s 확인)
tail -3 logs/order_watcher.log

# position_exit_runner 확인 (started 확인)
tail -3 logs/position_exit.log

# position_engine 확인 (started, consuming claw:fill:queue)
tail -3 logs/position_engine.log

# pause 상태 확인
REDIS_PASS=$(python3 -c "import urllib.parse,os; u=urllib.parse.urlparse(os.environ['REDIS_URL']); print(u.password or '')")
docker exec claw-redis redis-cli -a "$REDIS_PASS" GET claw:pause:global
```

---

## 📈 장중 관찰 루틴 (Phase 11)

```bash
TODAY=$(date +%Y%m%d)

# 1. Execution funnel 확인 (핵심)
docker exec claw-redis redis-cli -a "$REDIS_PASS" HGETALL execution_funnel:KR:$TODAY

# 2. Risk reject 원인 확인
docker exec claw-redis redis-cli -a "$REDIS_PASS" HGETALL risk:reject_count:KR:$TODAY

# 3. Consensus stats
docker exec claw-redis redis-cli -a "$REDIS_PASS" HGETALL consensus:stats:KR:$TODAY

# 4. AI call count
docker exec claw-redis redis-cli -a "$REDIS_PASS" GET ai:dual_call_count:KR:$TODAY

# 5. Dual eval stats
docker exec claw-redis redis-cli -a "$REDIS_PASS" HGETALL ai:dual_stats:consensus:KR:$TODAY

# 6. pause 상태
docker exec claw-redis redis-cli -a "$REDIS_PASS" GET claw:pause:global
docker exec claw-redis redis-cli -a "$REDIS_PASS" GET claw:pause:reason
```

---

## ⚙️ 운영 설정값 (Phase 11 기준)

`config/phase10_kr_micro.env`:
```bash
STRATEGY_KR_COOLDOWN_SEC=300        # 5분 (Phase 11: 10분→5분)
STRATEGY_KR_DAILY_CAP=40
RISK_KR_MAX_CONCURRENT=2
RISK_KR_DAILY_LOSS_LIMIT=-500000
RISK_KR_ALLOCATION_CAP_PCT=1.00    # 잔고 전액
GEN_DAILY_CALL_CAP=1500
CONSENSUS_POLL_SEC=60               # Phase 11: 30초→60초
CONSENSUS_SYMBOL_COOLDOWN_SEC=180   # Phase 11 신규
CONSENSUS_MIN_RET_5M=0.001          # Phase 11: 0.0→0.001
```

**워치리스트 (8종목, 10만원 이하):**
```
005930,105560,055550,086790,034020,010950,035720,032640
```

---

## 🔧 주요 이슈 & 수정 이력

| 날짜 | 커밋 | 수정 내용 |
|------|------|----------|
| 03-12 | `23cdaf2` | `_set_auto_pause` TG 스팸 수정 |
| 03-13 | `8ec78d7` | KIS available_cash fallback (`ord_psbl_cash or dnca_tot_amt`) |
| 03-16 | `3da23b2` | allocation_cap_pct env var 지원 |
| 03-17 | `8f80255` | KIS 토큰 Redis 캐싱 (403 tokenP 해결) |
| 03-18 | `17e0961` | position_exit_runner 2차 리뷰 수정 (avg_price/mark_price 가드 등) |
| 03-18 | `c6453d6` | order_watcher load_dotenv override 제거 (ttl=60s 미적용 버그 수정) |

---

## 🗓 Phase 이력 요약

| Phase | 기간 | 핵심 | 결과 |
|-------|------|------|------|
| 8 | ~2026-02 | AI 신호 생성 + 무인 안전장치 | ✅ |
| 9 | 2026-03-05~10 | AI-First 안정화 | emit_rate 27.7% ✅ |
| 9.5 | 2026-03-10~11 | Claude+Qwen 듀얼런 | match_rate 84.7% ✅ |
| 10 | 2026-03-12~17 | KR micro dry-run 4일 | KIS 실매수 1건 ✅ |
| 11 | 2026-03-17~18 | Execution rate 개선 | executed=13, rate=62.5% ✅ |
| **12** | **2026-03-18~** | **자동 매도** | time_limit 매도 검증 ✅ |

---

## 🚦 다음 세션 가이드

### 현재(2026-03-18) 상태
- Phase 12 구현 완료, 프로세스 10개 기동 중
- 자동 매도 검증 완료: 015760, 034220 time_limit 30분 → 자동 매도 ✅
- 현재 포지션 없음 (매도 완료) — 신규 매수 가능 상태

### 다음 세션 시작 시 체크리스트
1. `tail -5 logs/runner.log` — `kr_cooldown=300s, daily_cap=40` 확인
2. `tail -3 logs/order_watcher.log` — `ttl_cancel=60s` 확인
3. `tail -5 logs/position_exit.log` — `started` 또는 hold/exit 로그 확인
4. `claw:pause:global` — pause 없음 확인
5. `execution_funnel:KR:$(date +%Y%m%d)` — 오늘 실행 현황
6. `position_index:KR` — 현재 보유 포지션 확인

### Phase 12 관찰 지표
- stop_loss / take_profit / time_limit 각각 발동 비율 확인
- exit 후 재진입 여부 (MAX_CONCURRENT 슬롯 회복)
- 수익/손실 패턴 (PnL은 수동 확인 필요 — FillEvent 미연동)

### Phase 13 완료 (2026-03-18) — KR Fill Detection
- position_exit_runner: 잔고 diff → BUY/SELL FillEvent push → `claw:fill:queue`
- scripts/position_engine: fill queue 소비 → Portfolio Engine apply_fill → realized PnL 자동 기록
- exec_id setnx 중복 방지, retry/DLQ 내장
- **PnL 파이프라인 완성**: 매수 체결 → FillEvent(BUY) → 매도 체결 → FillEvent(SELL) → realized PnL

### 알려진 한계
- order_watcher가 KIS 주문 상태 조회 API 없음 → 체결 감지는 holdings diff 방식 사용
- 재기동 시 이미 push된 BUY fill이 중복 push될 수 있음 (dedupe TTL 24h로 방지)

### 무인 운영 팁
- `caffeinate -i -s &` (전원 연결 필수)
- REDIS_PASS 추출: `python3 -c "import urllib.parse,os; u=urllib.parse.urlparse(os.environ['REDIS_URL']); print(u.password or '')"`
- 재기동 시 order_watcher는 반드시 `PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60` 포함

---

**Claw‑Trader Engine:** Phase 12 (2026-03-18) — 자동 매도 완성, 단타 loop 검증 완료
