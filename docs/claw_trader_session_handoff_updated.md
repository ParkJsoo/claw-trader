# 🧠 Claw‑Trader Session Handoff (Updated 2026-03-19)

---

## Runtime Reality (2026-04-17)

- 실제 운영 source of truth는 `config/supervisord.conf`
- 현재 실행 모드는 `EXECUTION_MODE=claude_only`
- KR 기본 exit는 `1.5% / 3.0% / 1.5%`, `KR_TRAIL_ONLY_TRIGGER_PCT=0.020`
- COIN 기본 exit는 `3.0% / 15.0% / 4.0%`, `COIN_EARLY_EXIT_SEC=600`, `COIN_EARLY_EXIT_PCT=0.010`
- COIN 실시간 exit primary는 `upbit_ws_exit_monitor`, `position_exit_runner`는 fallback 성격
- `daily_report_runner`는 COIN `perf:daily` 저장과 intraday `pnl:COIN` 동기화를 수행

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

## 📊 Current Phase: **14 완료 + AI call 최적화** (2026-03-19)

### Phase 이력
| Phase | 내용 | 결과 |
|-------|------|------|
| 11 | Execution rate 개선 | executed=13, rate=62.5% ✅ |
| 12 | 자동 매도 (position_exit_runner) | time_limit 30분 검증 ✅ |
| 13 | KR/US Fill Detection + PnL | realized PnL 자동 기록 ✅ |
| **14** | **뉴스 → AI + 동적 워치리스트 (KR/US)** | universe 30→8 자동 선택 ✅ |
| **14+** | **AI call 최적화 + 봇 PnL** | ~900 call/day 목표 ✅ |

### 현재 자동화 수준
```
뉴스 수집 → AI 판단 시 자동 참조 ✅
워치리스트 → 장중 1h/장외 6h마다 뉴스+모멘텀 기반 자동 교체 ✅
매수 신호 → AI(Claude+Qwen) 자동 생성 ✅
매수 실행 → 자동 ✅
자동 매도 (손절 -2% / 익절 +2% / 30분) → 자동 ✅
PnL 기록 → 자동 ✅
```

사람이 해야 하는 것: 프로세스 기동, 자금 관리, 긴급 pause

---

## ✅ Phase 14 구현 완료 (2026-03-18) — 뉴스 통합 + 동적 워치리스트

### 동적 워치리스트 (`src/app/watchlist_selector_runner.py`)
- `GEN_UNIVERSE_KR` 30종목 → 뉴스 sentiment/impact 스코어링 + 모멘텀
- 상위 8종목(`UNIVERSE_SELECT_COUNT`) 선택 → `dynamic:watchlist:KR` Redis SET (TTL 8h)
- 장중(09:00-15:30) 1시간 / 장외 6시간마다 갱신 (`WATCHLIST_SELECT_INTERVAL_MARKET_SEC=3600`, `WATCHLIST_SELECT_INTERVAL_SEC=21600`)
- ai_dual_eval_runner / consensus_signal_runner / market_data_runner 모두 동적 워치리스트 사용

### 뉴스 → AI 프롬프트 통합
- `ai_dual_eval_runner._eval_symbol()`: 오늘/어제 뉴스 3건 자동 fetch
- `base.py build_dual_prompt()`: news_summary 있을 때 "최근 뉴스" 섹션 추가

Redis 키 추가:
```
dynamic:watchlist:KR          # 현재 활성 워치리스트 SET (TTL 8h)
```

---

## ✅ 2026-03-19 개선 사항 (커밋 6c6e73d)

### AI call 최적화 (Issue 3)
- `ai_dual_eval_runner.py`: `DUAL_POLL_SEC` 120→180, `DUAL_DAILY_CALL_CAP` 2000→500/market
- ret_5m/range_5m prefilter 추가 (consensus_signal_runner와 동일 기준) — AI call 전 차단
- `config/phase10_kr_micro.env`에 `DUAL_POLL_SEC=180`, `DUAL_DAILY_CALL_CAP=500` 추가
- 예상 효과: 일일 ~900 call (Phase 14 Day 1의 1500/1500 소진 방지)

### news_runner 동적 워치리스트 반영 (Issue 5)
- `_get_watchlists()`: `parse_watchlist()` → `load_watchlist()` (Redis dynamic 우선)
- 매 폴링 시 watchlist 갱신 (watchlist_selector 장중 1h/장외 6h 변경 자동 반영)

### TG 봇 PnL 커맨드 (Issue 6)
- `/claw pnl`: realized/unrealized PnL + 오픈 포지션 목록 (qty, avg_price, unrealized)
- `pnl:{market}` hash + `position_index:{market}` sorted set + `position:{market}:{symbol}` hash 조회

### 확인된 사항
- **US Fill Detection**: `_sync_positions()` KR/US 동일 로직 — IbkrClient.get_us_holdings() 기반 ✅
- **market_data_runner 자동 갱신**: 60초마다 Redis dynamic watchlist 자동 반영 — 재시작 불필요 ✅

---

## ✅ Phase 13 구현 완료 (2026-03-18) — KR/US Fill Detection + PnL

- `position_exit_runner._sync_positions()`: 잔고 diff → BUY/SELL FillEvent → `claw:fill:queue`
- `scripts/position_engine`: fill queue 소비 → Portfolio Engine → realized PnL 자동 기록
- exec_id setnx 중복 방지 (TTL 24h), retry/DLQ 내장

Redis 키 추가:
```
claw:fill:queue               # FillEvent 큐 (lpush/brpop)
claw:fill:dlq                 # 처리 실패 DLQ
claw:fill_dedupe:{exec_id}    # 중복 push 방지 (TTL 24h)
```

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
- **Phase 14 AI 판단 통합 완료** — `ai_dual_eval_runner`가 뉴스 조회 후 프롬프트에 자동 주입

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

## 🚀 프로세스 기동 순서 (Phase 14, 프로젝트 루트에서)

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
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.watchlist_selector_runner >> logs/watchlist_selector.log 2>&1 &
```
> ⚠️ order_watcher: `PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60` 필수 (없으면 ttl=15s로 기동됨)
> ⚠️ position_engine: fill queue 소비 프로세스 — 미기동 시 PnL 기록 안 됨
> ⚠️ watchlist_selector: 미기동 시 env 고정 워치리스트로 fallback됨 (동작은 하나 동적 선택 안 됨)

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
| 03-18 | `2f86919` | position_engine parse_failed → idle 카운터 명칭 수정 |
| 03-18 | `0813a9d` | auto-pause TTL 없음 → 자정 KST 자동 만료 추가 (generator.py, signal_generator_runner.py) |
| 03-18 | `26d7e06` | signal_generator 재시작 시 MD_ERROR_SPIKE 오탐 수정 (md_err_prev 누적값으로 초기화) |

---

## 🗓 Phase 이력 요약

| Phase | 기간 | 핵심 | 결과 |
|-------|------|------|------|
| 8 | ~2026-02 | AI 신호 생성 + 무인 안전장치 | ✅ |
| 9 | 2026-03-05~10 | AI-First 안정화 | emit_rate 27.7% ✅ |
| 9.5 | 2026-03-10~11 | Claude+Qwen 듀얼런 | match_rate 84.7% ✅ |
| 10 | 2026-03-12~17 | KR micro dry-run 4일 | KIS 실매수 1건 ✅ |
| 11 | 2026-03-17~18 | Execution rate 개선 | executed=16, COOLDOWN=6, MAX_CONCURRENT=4 ✅ |
| 12 | 2026-03-18 | 자동 매도 | time_limit 30분 매도 검증 ✅ |
| 13 | 2026-03-18 | KR Fill Detection + PnL | realized PnL 자동 기록 ✅ |
| **14** | **2026-03-18~** | **뉴스 통합 + 동적 워치리스트** | universe 30→8 자동 선택 ✅ |

---

## ✅ 2026-03-19 작업 완료

### 코드리뷰 8건 수정 (`e01ed7c`)
- CRITICAL: `r.keys()` → `scan_iter` (O(N) 블로킹 제거)
- CRITICAL: IbkrClient 조건부 초기화 (KR-only 기동 시 크래시 방지)
- CRITICAL: strategy daily_cap → Lua 원자화 (race condition 제거)
- CRITICAL: BUY fill 감지 시 기존 포지션 존재 여부 체크 (PnL 중복 방지)
- 기타: signal_generator_runner NX 가드, runner REJECTED 주문 broker_reject 분류 등

### US 자동매매 기능 추가 (`dc5e997`)
- `IbkrClient.get_us_holdings()` 구현
- `position_exit_runner` KR/US 통합
- `consensus_signal_runner` US watchlist 처리
- `config/phase10_us_micro.env` 신규 작성

### US 동적 워치리스트 추가 (`668214e`)
- `watchlist_selector_runner.py` — GEN_UNIVERSE_US → `dynamic:watchlist:US` (KR과 동일 로직)
- `phase10_us_micro.env` — `GEN_UNIVERSE_US` 25종목 추가 (S&P500 대형주)
- KR/US 데이터 레이어 동등화 완료

### KR vs US 현황 (2026-03-19 기준)
| 컴포넌트 | KR | US |
|----------|----|----|
| consensus_signal_runner | ✅ | ✅ |
| ai_dual_eval_runner | ✅ | ✅ |
| position_exit_runner | ✅ | ✅ |
| 동적 워치리스트 | ✅ | ✅ |
| 뉴스 수집 | ✅ | ✅ |
| 시장 데이터 | ✅ 실시간 | ⚠️ Delayed Frozen (의도적) |

---

## 🚦 다음 세션 가이드

### 현재(2026-03-19) 상태
- Phase 14 + US 데이터 레이어 동등화 완료
- 테스트 141개 all pass
- main 브랜치 clean, origin 동기화 완료
- US 거래는 코드 완비, 실시간 데이터 구독만 미활성 (의도적)

### 다음 세션 시작 시 체크리스트
1. `tail -5 logs/runner.log` — `kr_cooldown=300s, daily_cap=40` 확인
2. `tail -3 logs/order_watcher.log` — `ttl_cancel=60s` 확인
3. `tail -3 logs/watchlist_selector.log` — KR/US 선택된 종목 확인
4. `tail -3 logs/position_engine.log` — `idle=` 로그 확인 (parse_failed 아님)
5. `claw:pause:global` — None이어야 정상
6. `dynamic:watchlist:KR`, `dynamic:watchlist:US` — 활성 워치리스트 확인
7. AI call count 초기화 여부 확인

### 관찰 지표 (Phase 14+)
- `dynamic:watchlist:KR` / `dynamic:watchlist:US` — 장중 1h/장외 6h마다 갱신 확인
- `position_engine` 로그 — FillEvent 처리 확인
- `pnl:KR` Redis 키 — realized PnL 누적 확인
- stop_loss / take_profit / time_limit 발동 비율

### 알려진 한계
- KIS 주문 상태 조회 API 없음 → 체결 감지는 holdings diff 방식
- 재기동 시 BUY fill 중복 push 가능 (dedupe TTL 24h로 방지)
- US 거래 미활성 (IBKR reqMarketDataType=4 유지 중 — 의도적)
- realized_pnl=0 (Phase 14 Day 1 체결 16건이지만 PnL 기록 0 → position_engine 소비 여부 확인 필요)

### 무인 운영 팁
- `caffeinate -i -s &` (전원 연결 필수)
- REDIS_PASS 추출: `python3 -c "import urllib.parse,os; u=urllib.parse.urlparse(os.environ['REDIS_URL']); print(u.password or '')"`
- 재기동 시 order_watcher는 반드시 `PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60` 포함

---

**Claw‑Trader Engine:** Phase 14 + US 데이터 레이어 (2026-03-19) — KR/US 동등화 완료, 테스트 141개 all pass
