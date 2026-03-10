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
- available_cash=0 → ACCOUNT_SNAPSHOT_ERROR 처리 (입금 완료, live 구독 전까지 유지) ⚠️

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

**Current Phase:** 10 준비 완료 — Phase 9.5 조건 충족, consensus_signal_runner 구현 완료 (2026-03-10)
**System Stability:** ⭐⭐⭐⭐⭐ Production-Ready (pause=true 유지 — 실주문 없음)
**Current Universe Mode:** Static (.env-based watchlist, KR 8종목)
**Next Evolution:** Phase 9.5 clean full-day 확인(2026-03-11) → Phase 10 dry-run 시작
**KR Pipeline:** ✅ 완전 동작 (장중 모멘텀 발생 시 신호 생성)
**US Pipeline:** ✅ Delayed Frozen (reqMarketDataType=4) — live 구독 전까지 유지
**md:last_update age:** KR/US 모두 정상 갱신 중 ✅
**Running Processes:** app.runner / app.market_data_runner / scripts.order_watcher / app.signal_generator_runner / app.ai_eval_runner / **app.ai_dual_eval_runner** / **app.consensus_signal_runner** / **app.openclaw_bot** / **app.news_runner** (9개) ✅
**gen:runner:lock TTL:** ~80s ✅ / **eval:runner:lock TTL:** ~300s ✅
**caffeinate -i -s 검증:** 뚜껑 닫아도 28초 간격 폴링 유지 — gap 없음 ✅

---

## ✅ PHASE 10 — consensus_signal_runner 구현 완료 (2026-03-10)

### 신규 파일
- `src/app/consensus_signal_runner.py` — dual eval → Signal 생성 → claw:signal:queue push
- `config/phase10_kr_micro.env` — Phase 10 KR 운영값 프로파일 (cooldown=600s, max_concurrent=2)
- `docs/phase10_dryrun_guide.md` — dry-run 체크리스트 + 장중 관찰 지표
- `tests/test_consensus_signal_runner.py` — 24개 all pass

### 처리 흐름
```
ai:dual:last:claude/qwen:{market}:{symbol}
  → consensus 확인 (both emit=1, direction LONG 일치)
  → prefilter (ret_5m > 0, range_5m > 0.004)
  → Signal 생성 (entry.price=current_price, size_cash=current_price, stop=-2%)
  → Pydantic 검증
  → claw:signal:queue lpush
  → StrategyEngine → RiskEngine → OrderExecutor (기존 파이프라인 재사용)
```

### Phase 10 Config (runner.py env var override)
- `STRATEGY_KR_COOLDOWN_SEC=600` (기본 300 → Phase 10: 600)
- `RISK_KR_MAX_CONCURRENT=2` (기본 5 → Phase 10: 2)
- runner 시작 시 `runner: config kr_cooldown=600s kr_max_concurrent=2 ...` 출력

### Redis 키 (신규)
```
consensus:stats:KR:{YYYYMMDD}          # candidate/reject 카운트
consensus:daily_count:KR:{YYYYMMDD}    # 일별 candidate 생성 수
consensus:audit:KR:{signal_id}          # 감사 로그 (TTL 7d)
consensus:runner:lock                   # 프로세스 락 (TTL 120s)
```

### 테스트 현황
- 전체: **106 all pass** (기존 82 + consensus 24)

---

## ✅ PHASE 9.5 — Claude vs Qwen 듀얼런 비교 엔진 (구현 완료 2026-03-07)

### 신규 파일
- `src/ai/providers/base.py` — DecisionResult + DecisionProvider + build_dual_prompt (공통 프롬프트)
- `src/ai/providers/claude_provider.py` — Anthropic 판단 Provider (OverloadedError retry 2회)
- `src/ai/providers/qwen_provider.py` — Ollama REST 판단 Provider (urllib 전용)
- `src/app/ai_dual_eval_runner.py` — 듀얼런 runner (주문 없음)
- `docs/ai_dual_run.md` — 아키텍처 + Redis 키 + 관찰 지표

### 합의 정책
| Claude | Qwen | direction 일치 | consensus |
|--------|------|----------------|-----------|
| emit=true | emit=true | O | EMIT |
| emit=true | emit=true | X | HOLD |
| 한쪽만 emit | - | - | HOLD |
| emit=false | emit=false | - | SKIP |

### Redis 키
```
ai:dual:last:{provider}:{market}:{symbol}     # 최신 판단
ai:dual_compare:{market}:{YYYYMMDD}           # 비교 통계
ai:dual_stats:consensus:{market}:{YYYYMMDD}   # consensus 통계
ai:dual_call_count:{market}:{YYYYMMDD}        # 라운드 캡 (2000/market/day)
```

### 기동 방법 (Ollama 설치 후)
```bash
ollama pull qwen2.5:7b && ollama serve
PYTHONPATH=src ../venv/bin/python -m app.ai_dual_eval_runner
```

### Phase 9.5 완료 조건 (2026-03-10 EOD 판정)
- ✅ match_rate ≥ 50% (84.7% EOD 확인)
- ✅ claude emit_rate 10~30% (post-fix 27.7% / dual 28.5%)
- ✅ qwen_only_emit ≤ 20% (3.9% EOD 확인)
- ✅ error_rate < 5% (0%)
- ⏸ 2026-03-11 clean full-day 재확인 → Phase 9.5 최종 확정

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

## ✅ News Intelligence Layer (2026-03-07 구현 완료)

### 신규 파일
- `src/news/models.py` — NewsItem dataclass (reliability 필드: dart=0.95, yahoo=0.80, google=0.65)
- `src/news/collector.py` — DART API + Google News RSS + Yahoo Finance RSS
  - 언어 필터: KR=한글 포함 필수, US=영어 포함 필수
- `src/news/classifier.py` — Qwen 분류/요약 (KR 한국어 프롬프트, US 영어 프롬프트)
- `src/news/redis_writer.py` — Redis 저장 + URL dedup + get_symbol_context()
- `src/app/news_runner.py` — 독립 프로세스 (30분 폴링, 프로세스 락)

### Redis 키
```
news:raw:{market}:{YYYYMMDD}              # 전체 수집 (max 200, TTL 2d)
news:symbol:{market}:{symbol}:{YYYYMMDD} # 종목별 분류 뉴스 (max 50, TTL 1d)
news:macro:{market}:{YYYYMMDD}            # 매크로 뉴스 (max 50, TTL 1d)
news:seen:{market}:{YYYYMMDD}             # URL dedup set (TTL 2d)
news:stats:{market}:{YYYYMMDD}            # 통계 (TTL 7d)
news:runner:lock                          # 프로세스 락 (TTL 1h)
```

### 기동
```bash
PYTHONPATH=src ../venv/bin/python -m app.news_runner
```

### 판단 통합 시점
- get_symbol_context(r, market, symbol, today) 준비됨
- 적용 시점: Phase 10 이후 (현재는 수집/분류/저장만)

---

## ✅ 코드 리뷰 + 보안 강화 (2026-03-07)

### 보안 수정 (보안 점수 8.0/10)
- **XXE 방어**: `defusedxml` 도입 (RSS XML 파싱)
- **DART SSRF 방어**: `rcept_no` 14자리 숫자 정규식 검증
- **프롬프트 인젝션 3단계 방어**:
  1. `_sanitize_input()` — 뉴스 title/excerpt에서 인젝션 패턴 제거
  2. `_SUMMARY_ALLOWLIST_RE` — Qwen 출력 ai_summary에 한글/영문/숫자/문장부호만 허용
  3. `_safe_summary()` — Claude 프롬프트 삽입 전 재필터
- **auto_pause NX**: generator의 `_set_auto_pause`에 `SET NX` 적용 → 기존 reason 덮어쓰기 방지
- **KIS 토큰 갱신**: `_request_with_retry()` — 401 감지 시 토큰 재발급 + 1회 재시도

### 품질 수정 (품질 점수 7.5/10)
- **공통 파서 추출**: `parse_decision_response()` → `base.py` (ClaudeProvider/QwenProvider 중복 제거)
- **write_batch 이중 is_seen 제거**: `write_item()` → bool 반환, 외부 체크 불필요
- **매크로 키워드 env 파싱 수정**: `_load_macro_keywords()` — 빈 env 시 기본값 정상 반환
- **classify_batch 병렬화**: `ThreadPoolExecutor(max_workers=4)` → ~4x 속도 향상
- **runner pause 조기 체크**: 루프 초기에 pause 감지 → 불필요한 strategy/advisor 호출 제거
- **미사용 import 정리**: claude_provider, collector의 불필요 import 제거

### 주요 잔여 리스크
- ~~테스트 코드 부재~~ → **2026-03-09 해결**: tests/ 신규 82개 all pass ✅

---

## 🔥 Immediate Next Priority (2026-03-10 기준)

### 현재 모드: AI-First / No-Trade
- `claw:pause:global=true` 유지 — 실주문 없음
- Phase 9 Day 1 EOD (2026-03-05): emit_rate=11.9%, error_rate=1.9% ✅
- Phase 9 Day 2 장마감 (2026-03-06): KR emit_rate=3.2%(장마감 후), cap=2000/2000 소진
- 2026-03-07: 뉴스 파이프라인 구현 + 코드 리뷰/보안 강화 완료
- 2026-03-09: 자체 코드리뷰/보안점검 + 치명 버그 수정 + 테스트 82개 all pass ✅
- 2026-03-10: KR 프롬프트 emit 조건 강화 + 장중 효과 확인 ✅

### 2026-03-10 주요 작업
- **emit_rate 과다 문제 발견**: 장 초반 55.9% (목표 10~30%)
- **원인**: 프롬프트 threshold 0.002 너무 낮음 + ret_5m 음수여도 range만으로 EMIT
- **수정 (커밋 `b4eaf9c`)**: `ret_5m > 0 AND range_5m > 0.004` 조건으로 강화
  - 수정 파일: `src/ai/generator.py`, `src/ai/providers/base.py`
  - 신규 프롬프트: "Emit a LONG signal only when BOTH: 1. ret_5m > 0  2. range_5m > 0.004"
- **효과 확인**: 수정 후 신규 판단 26.6% → 목표 범위 진입 ✅
- **C(코드 레벨 프리필터) 보류**: Phase 10 consensus_signal_runner에서 적용 예정
- **듀얼런 (09:30 기준)**: match_rate=83.8%, qwen_only_emit=3.1% ✅

### 로드맵 (GPT 협의 확정)
- **Phase 9** (현재): AI-First 안정화 — 오늘 장 마감 후 최종 판정
- **Phase 9.5**: Claude vs Qwen 듀얼런 (match_rate≥50%, qwen_only_emit≤20%, error_rate<5%)
- **Phase 10**: KR micro trading (8종목, 09:30~11:00, 1주 단위, stop_loss=2%)
- **Phase 11**: watchlist 확장(8→10→12) + US 활성화

### 1. Phase 9 최종 판정 (오늘 장 마감 후 15:30 KST)
- `ai:eval_stats:KR:{오늘}` → 신규 프롬프트 기준 emit_rate 10~30% 확인
- error_rate < 5% 유지 확인
- 조건 충족 시 → Phase 9.5 공식 진입 선언

### 2. IBKR live 구독 해결 후
- `reqMarketDataType(4)` → `(1)` 변경 (`ibkr_feed.py:51`)
- market_data_runner 재기동 → US MD 신선도 확인

---

## 🗓 운영 모드 (IBKR 입금 완료)

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

**AI Eval Runner (PHASE 9) ✅**
- ai:eval:last:{market}:{symbol} — 최신 AI 판단 (hash, overwrite)
- ai:eval_log:{market}:{YYYYMMDD} — 일별 판단 로그 (list, 최대 500)
- ai:eval_stats:{market}:{YYYYMMDD} — 일별 통계 (emit/no_emit/error/skip_*)
- ai:eval_call_count:{market}:{YYYYMMDD} — 일별 호출 수 (cap=2000, 별도)
- eval:runner:lock — 프로세스 락 (TTL 300s)

---

## 🚀 Guidance For Next Chat

**현재 모드: Phase 10 준비 완료 — dry-run 대기 중 (2026-03-10)**

Start From:
1. **[즉시]** Phase 10 dry-run 시작: `source config/phase10_kr_micro.env` → runner 재기동 → consensus_signal_runner 기동
2. **[2026-03-11 장 전]** `docs/phase10_dryrun_guide.md` 체크리스트 5개 항목 순서대로 확인
3. **[2026-03-11 EOD]** 장중 관찰 지표 확인 (candidate 5~30건, emit_rate 10~30%, executable 3~10건)
4. IBKR live 구독 완료 시 → `ibkr_feed.py:51` reqMarketDataType 4→1 변경

Phase 9 AI-First Exit 조건 (2026-03-10 EOD 전항목 충족 ✅):
- ✅ error_rate < 5% (0%)
- ✅ emit_rate 10~30% (post-fix 27.7% / dual 28.5%)
- ✅ md_age < 30s (2초)
- ✅ runner crash 없음
- ✅ watchlist 8종목 안정
- ✅ Risk Engine 활성
- ✅ 테스트 82개 all pass
- ✅ 장중 안정 운영 확인 (2026-03-10)

> ⚠️ pause 해제(실주문)는 Phase 9.5 듀얼런 완료 후 Phase 10에서만

운영 루틴:
```bash
# md 신선도
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" GET md:last_update:KR
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" GET md:last_update:US

# 가격 변화 확인
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" LRANGE mark_hist:KR:005930 0 4

# AI 통계
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" HGETALL ai:gen_stats:KR:$(date +%Y%m%d)

# AI 호출 수
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" GET ai:call_count:KR:$(date +%Y%m%d)

# 프로세스 락
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" TTL gen:runner:lock
```

### 주요 구현 파일
- src/guards/notifier.py — Telegram 알림
- src/ai/generator.py — AI 호출 캡 + auto-pause
- src/app/signal_generator_runner.py — 헬스 모니터 + auto-pause + TG 스팸 방지
- src/market_data/ibkr_feed.py — Delayed Frozen + reconnect backoff

### 프로세스 기동 순서 (Phase 10, 프로젝트 루트에서 실행)
```bash
# Phase 10 config 먼저 적용 (재기동 시 반영)
source config/phase10_kr_micro.env

# /Users/henry_oc/develop/claw-trader 에서 실행
PYTHONPATH=src venv/bin/python -m app.runner                    # 신호 처리 파이프라인 (Phase 10 config)
PYTHONPATH=src venv/bin/python -m app.market_data_runner        # 현재가 폴링
PYTHONPATH=src venv/bin/python -m scripts.order_watcher         # 주문 감시
PYTHONPATH=src venv/bin/python -m app.signal_generator_runner   # AI 신호 생성기
PYTHONPATH=src venv/bin/python -m app.ai_eval_runner            # AI 평가 러너
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner       # Claude vs Qwen 듀얼런 (Phase 9.5)
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner   # Phase 10 신규: dual → Signal → queue
PYTHONPATH=src venv/bin/python -m app.openclaw_bot              # TG 운영 봇
PYTHONPATH=src venv/bin/python -m app.news_runner               # 뉴스 수집/분류 (30분 폴링)
```

### .env 주요 변수
```
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
GEN_WATCHLIST_KR=005930,000660,...  # 실제 운영 8종목 (.env 참조)
GEN_WATCHLIST_US=AAPL,NVDA          # 실제 값은 .env 참조
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

### Live 전환 상태 (트랙 분기)
```
✅ STEP 1: IBKR 입금 확인 완료 (3/3)
⏸ STEP 2: IBKR live 구독 미완료 → reqMarketDataType(4) 유지
✅ STEP 3: 프로세스 재기동 + US MD 갱신 확인
⏸ STEP 4~7: AI-First 트랙(트랙 B) — 실주문 뒤로 미룸

트랙 A(실주문)로 전환 시:
  → IBKR live 구독 완료 후 ibkr_feed.py:51 → reqMarketDataType(1)
  → docs/live_transition_checklist.md STEP 4부터 진행
```

### 무인 운영 팁
- 뚜껑 닫아도 프로세스 유지: `caffeinate -i -s &` (전원 연결 필수)
  - `-i`: 소프트웨어 잠자기 방지
  - `-s`: 뚜껑 닫기(lid-close) 잠자기 방지
  - `caffeinate -i`만으로는 뚜껑 닫기 시 sleep 발생 → lock TTL 만료 주의
  - **`caffeinate -i -s` 현장 검증 완료 (2026-03-02)**: 뚜껑 닫고 40분 후에도 28초 간격 폴링 유지, gap 없음 ✅
- 잠자기 후 재기동 시 gen:runner:lock TTL 확인 필수
  - TTL=-2이면 signal_generator_runner 재시작 필요

---

## ✅ 2026-03-09 코드리뷰/보안점검 + 치명 버그 수정

### 치명 버그 수정
- **KIS BUY/SELL 미구분**: `place_order()` tr_id가 항상 TTTC0802U(매수)였음 → BUY=TTTC0802U / SELL=TTTC0801U 분기 수정 (실거래 시 매도가 매수로 처리되는 치명적 버그)

### 보안/안정성 수정
- **Signal 입력값 검증**: `SignalEntry.price/size_cash` 양수 field_validator 추가 (Pydantic)
- **symbol 형식 검증**: `Signal.symbol` → `^[A-Z0-9]{1,10}$` 정규식 검증
- **KIS 시크릿 마스킹**: 예외 시 `app_secret` 노출 방지 — `RuntimeError` 래핑

### 원자성/안정성
- **ai/generator.py daily_cap**: INCR/DECR 비원자 → `_LUA_CAP_INCR` Lua 스크립트로 원자화 (멀티프로세스 안전)
- **price_near() tolerance**: 2분 초과 데이터 무효 처리 (잘못된 피처 계산 방지)
- **strategy daily_cap DECR 롤백**: 초과 시 카운터 롤백 → 실제 통과 수만 카운트
- **Executor cancel() 소유권 검증**: `claw:order_meta` 존재 시에만 취소 허용

### 테스트 (신규 82개 all pass)
- `tests/test_risk_engine.py` — RiskEngine 5규칙
- `tests/test_strategy_engine.py` — StrategyEngine 3규칙
- `tests/test_parse_decision_response.py` — AI 응답 파서
- `tests/test_security_filters.py` — 보안 필터 (sanitize/allowlist/인젝션)

커밋: `d9865dc`

---

## ✅ 2026-03-10 KR 프롬프트 emit 조건 강화

### 문제
- 장 초반 KR emit_rate = 55.9% (목표 10~30%) 초과
- 원인: `range_5m > 0.002` 단독 조건 — 방향성 없는 변동성에도 EMIT
- ret_5m 음수인데도 range_5m만으로 EMIT하는 케이스 다수 (예: 207940)

### 수정 (커밋 `b4eaf9c`)
- 수정 파일 2곳: `src/ai/generator.py:135`, `src/ai/providers/base.py:70`
- 변경 전: `"Signal on clear 1-5min momentum with range_5m > 0.002"`
- 변경 후:
  ```
  Emit a LONG signal only when BOTH conditions are clearly satisfied:
  1. ret_5m > 0  2. range_5m > 0.004.
  If ret_5m <= 0, do NOT emit LONG.
  If volatility exists but directional momentum is unclear, return HOLD.
  Prefer HOLD over weak or ambiguous setups.
  ```

### 효과 (수정 후 20분 관찰)
- 신규 판단 64건 기준 emit_rate: **26.6%** → 목표 범위(10~30%) 진입 ✅
- 로그 확인: ret_5m 음수 → "fails condition 1" 정상 차단

### C(코드 레벨 프리필터) 보류 결정 (GPT 협의)
- Phase 9.5 관찰 단계에서 hard filter 넣으면 모델 raw 성향 관찰 흐려짐
- 적용 시점: Phase 10 `consensus_signal_runner` 실행 게이트
  ```
  if ret_5m <= 0: do not push even if consensus=EMIT
  ```

---

**Claw‑Trader Engine:** Phase 9.5 진입 선언 (2026-03-10 EOD) — 2026-03-11 clean full-day 확인 후 확정 ✅
