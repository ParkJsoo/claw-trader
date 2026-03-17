# 🧠 Claw‑Trader Session Handoff (Updated 2026-03-17)

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

## 📊 Current Phase: **11 — Execution Rate 개선** (2026-03-17 진입)

### Phase 10 → 11 전환 근거
- Phase 10 Day 4 (2026-03-17): executable=1건 ✅ — KIS 실매수 체결 확인 → end-to-end verified
- **핵심 문제**: execution_rate ≈ 2% (candidate→실행 전환율 목표 10~20%)
- GPT 판단: "Signal은 많이 나오는데 실행이 안 된다 — 효율 최적화 단계"

### Phase 11 목표
| 지표 | Phase 10 결과 | Phase 11 목표 |
|------|--------------|--------------|
| execution_rate | ~2% | ≥ 10% |
| AI call/day | ~1500 | ≤ 900 |
| emit_rate | 22~27% | 10~25% |
| pipeline_error | 0 ✅ | 0 |
| candidate/day | 24~54 | 20~40 |

### Phase 11 exit 조건 (2거래일 연속)
- [ ] execution_rate ≥ 10%
- [ ] AI call/day ≤ 900
- [ ] pipeline_error = 0
- [ ] drop reason 분포 정상 (COOLDOWN/MAX_CONCURRENT 위주)

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

## 🚀 프로세스 기동 순서 (Phase 11, 프로젝트 루트에서)

```bash
# ⚠️ 반드시 set -a 사용
set -a && source .env && source config/phase10_kr_micro.env && set +a

# 프로세스 종료 (재시작 시)
pkill -f "python -m app" 2>/dev/null; pkill -f "python -m scripts.order_watcher" 2>/dev/null; sleep 2

# 기동 (9개)
cd /Users/henry_oc/develop/claw-trader
PYTHONPATH=src venv/bin/python -m app.runner >> logs/runner.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.market_data_runner >> logs/market_data.log 2>&1 &
PYTHONPATH=src venv/bin/python -m scripts.order_watcher >> logs/order_watcher.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.signal_generator_runner >> logs/signal_generator.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_eval_runner >> logs/ai_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner >> logs/ai_dual_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner >> logs/consensus_signal.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.openclaw_bot >> logs/openclaw_bot.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.news_runner >> logs/news_runner.log 2>&1 &
```

**기동 직후 확인:**
```bash
# runner config 출력 확인 (cooldown=300s 확인)
tail -5 logs/runner.log

# consensus runner 확인 (poll_sec=60, cooldown=300 확인)
tail -5 logs/consensus_signal.log

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

---

## 🗓 Phase 이력 요약

| Phase | 기간 | 핵심 | 결과 |
|-------|------|------|------|
| 8 | ~2026-02 | AI 신호 생성 + 무인 안전장치 | ✅ |
| 9 | 2026-03-05~10 | AI-First 안정화 | emit_rate 27.7% ✅ |
| 9.5 | 2026-03-10~11 | Claude+Qwen 듀얼런 | match_rate 84.7% ✅ |
| 10 | 2026-03-12~17 | KR micro dry-run 4일 | KIS 실매수 1건 ✅ |
| **11** | **2026-03-17~** | **Execution rate 개선** | 진행 중 |

---

## 🚦 다음 세션 가이드

### 오늘(2026-03-17) 상태
- Phase 11 구현 완료, 프로세스 재시작 완료 (pause 해제됨)
- 장 마감 후: dual eval 모든 종목 HOLD — 신호 없음 (장중 확인 불가)
- **내일(2026-03-18) 장중**이 Phase 11 첫 번째 유효 관찰 기회

### 다음 세션 시작 시 체크리스트
1. `tail -20 logs/runner.log` — `kr_cooldown=300s` 확인
2. `execution_funnel:KR:$(date +%Y%m%d)` — drop reason 분포 확인
3. `risk:reject_count:KR:$(date +%Y%m%d)` — risk reject 원인 확인
4. `consensus:stats:KR:$(date +%Y%m%d)` — candidate 수 확인
5. `ai:dual_call_count:KR:$(date +%Y%m%d)` — AI call 수 확인 (목표 ≤900)

### Phase 11 Day 1 목표 (2026-03-18)
- execution_rate ≥ 5% (개선 추세 확인)
- drop reason 중 `COOLDOWN` 비율 감소 확인 (300초로 단축 효과)
- AI call ≤ 900
- pipeline_error = 0

### 무인 운영 팁
- `caffeinate -i -s &` (전원 연결 필수)
- REDIS_PASS 추출: `python3 -c "import urllib.parse,os; u=urllib.parse.urlparse(os.environ['REDIS_URL']); print(u.password or '')"`

---

**Claw‑Trader Engine:** Phase 11 진입 (2026-03-17) — execution rate 개선 단계
