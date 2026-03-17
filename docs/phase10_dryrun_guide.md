# Phase 10 Dry-Run 운영 가이드
Version: 0.1
Date: 2026-03-10
Status: Draft

---

## 1. 프로세스 기동 순서 (Phase 10)

```bash
# 1. Phase 10 config 적용
source config/phase10_kr_micro.env

# 2. 기존 프로세스 기동 (순서 유지)
PYTHONPATH=src venv/bin/python -m app.market_data_runner &
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner &
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner &  # Phase 10 신규
PYTHONPATH=src venv/bin/python -m app.runner &                    # Phase 10 config 재기동 필요
```

> **주의:** `source config/phase10_kr_micro.env` 후 runner 재기동해야 config 반영됨 (동적 reload 아님).

---

## 2. 장 시작 전 5분 체크리스트

### ✅ 1. 프로세스 상태 확인

```bash
ps aux | grep app.runner
ps aux | grep consensus_signal_runner
```

- 중복 프로세스 없음
- 이전 버전 runner 살아있지 않음

### ✅ 2. Phase 10 config 적용 확인

runner 시작 로그에 아래가 출력되는지 확인:

```
runner: config kr_cooldown=600s kr_daily_cap=40 kr_max_concurrent=2 kr_daily_loss_limit=-500000
```

값이 다르면 → `source config/phase10_kr_micro.env` 후 재기동.

### ✅ 3. auto-pause 해제 확인 (가장 중요)

```bash
redis-cli GET claw:pause:global
# 반드시 → (nil)  ← Phase 10은 실거래 허용 상태로 운영
```

> **주의**: Phase 10은 실제 주문 테스트 목적이므로 `claw:pause:global`이 **(nil)**이어야 정상.
> 전날 AI_ERROR_SPIKE 등으로 auto-pause가 걸렸을 수 있음 → `DEL claw:pause:global`로 해제 후 시작.

### ✅ 4. Queue 연결 확인

```bash
redis-cli LLEN claw:signal:queue
```

backlog 없음 (0 또는 낮은 값).

### ✅ 5. 샘플 Signal 배관 테스트

장 전에 테스트용 Signal 1건을 queue에 직접 push해서 runner가 consume/parse/log 하는지 확인:

```bash
redis-cli LPUSH claw:signal:queue '{"signal_id":"test-001","ts":"2026-03-11T09:00:00+09:00","market":"KR","symbol":"005930","direction":"LONG","entry":{"price":"70000","size_cash":"70000"},"stop":{"price":"68600"}}'
```

runner 로그에 strategy/risk 처리 로그 확인 → `claw:pause:global=true`면 PAUSED reject 정상.

---

## 3. 장중 관찰 지표

Phase 10 목표: **신호 파이프라인 안정성 검증** (수익 최적화 아님)

### 📍 09:30~10:00 (30분) — 초기 이상 패턴 감지

| 지표 | 정상 범위 | 이상 신호 |
|------|----------|----------|
| candidate signal 수 | 5~30건 | 30+ → emit_rate 문제 / 0 → prefilter 과도 |
| consensus 통과율 | 10~30% | 40%+ → 필터 약함 / 5%- → 필터 과도 |
| cooldown reject 비율 | 적정 수준 | 과도 → cooldown 600s 조정 필요 |
| signal burst (동일 종목 1~2분 내 연속) | 없음 | 있음 → prefilter 문제 |

```bash
# candidate 통계 확인
redis-cli HGETALL consensus:stats:KR:$(date +%Y%m%d)

# strategy reject 분포
redis-cli HGETALL strategy:reject_count:KR:$(date +%Y%m%d)

# consensus queue 잔량
redis-cli LLEN claw:signal:queue
```

### 📍 09:30~10:30 (1시간) — 파이프라인 funnel 확인

```
candidate → strategy 통과 → risk 통과 → executable
```

| 지표 | 정상 범위 |
|------|----------|
| executable signal 수 | 3~5건 |
| risk reject 이유 분포 | DUPLICATE / MAX_CONCURRENT 정상 |
| strategy reject 이유 분포 | COOLDOWN / DAILY_CAP 정상 |

```bash
# risk reject 이유 확인 (reject 로그 키)
redis-cli KEYS "claw:reject:KR:*" | head -20
```

### 📍 EOD 15:30 — 일일 최종 판단

| 지표 | 정상 범위 | Phase 10 exit 판단 기준 |
|------|----------|------------------------|
| executable signal/day | 3~10건 | 0 → 파이프라인 문제 / 10+ → 과도 |
| emit_rate | 10~30% | 현재 27.7% (유지) |
| pipeline error | 0 | >0 → 즉시 확인 |
| consensus:stats reject 분포 | 정상 분류 | invalid_payload >0 → 데이터 문제 |

```bash
# 일별 candidate 수
redis-cli GET consensus:daily_count:KR:$(date +%Y%m%d)

# 전체 통계
redis-cli HGETALL consensus:stats:KR:$(date +%Y%m%d)

# strategy pass/reject
redis-cli GET strategy:pass_count:KR:$(date +%Y%m%d)
redis-cli HGETALL strategy:reject_count:KR:$(date +%Y%m%d)
```

---

## 4. 이상 상황 대응

| 증상 | 원인 추정 | 조치 |
|------|----------|------|
| candidate 0건 | dual eval 미기동 / prefilter 과도 | ai_dual_eval_runner 로그 확인 |
| signal burst (동일 종목 연속) | prefilter range_5m 기준 점검 | consensus_signal_runner 로그 확인 |
| cooldown reject 과도 | 600s 너무 길 가능성 | Phase 10 안정화 후 재조정 |
| executable 0건 | pause=true 또는 max_concurrent 문제 | pause 상태 / position 확인 |
| pipeline error >0 | Redis 연결 / Signal validation 오류 | 즉시 로그 확인 |

---

## 5. Phase 10 운영 Redis 키 요약

```
# consensus_signal_runner 출력
consensus:stats:KR:{YYYYMMDD}          # candidate/reject 카운트
consensus:daily_count:KR:{YYYYMMDD}    # 일별 candidate 생성 수
consensus:audit:KR:{signal_id}          # 개별 signal 감사 로그 (TTL 7d)

# strategy
strategy:pass_count:KR:{YYYYMMDD}
strategy:reject_count:KR:{YYYYMMDD}    # hash: COOLDOWN/DAILY_CAP/DUP_SIGNAL

# 실거래 차단
claw:pause:global                       # "true" 유지 필수
```

---

## 6. Phase 10 exit 판단 기준

2거래일 연속 아래 조건 충족 시 Phase 10 완료:

- [ ] executable signal/day: 3~10건 범위
- [ ] emit_rate: 10~30% 유지
- [ ] pipeline error: 0
- [ ] signal burst 없음
- [ ] cooldown/risk reject 분포 정상
- [ ] claw:pause:global=true 상태 유지 (dry-run 기간)

---

## 7. Day별 운영 기록

### Day 1 (2026-03-12)
- candidate=54 ✅, strategy_pass=20, pipeline_error=0 ✅
- **이슈 1**: `STRATEGY_KR_DAILY_CAP=20`이 09:00~10:30 (1.5시간) 소진 → Day 2부터 40으로 조정
- **이슈 2**: AI call cap(1000) 소진 → `_set_auto_pause` TG 스팸 버그 발견 및 수정 (commit `23cdaf2`)
  - 원인: `send_telegram()`이 `if set_ok:` 블록 밖에 있어 매번 발송
  - 수정: `send_telegram`을 `if set_ok:` 블록 안으로 이동 (idempotent)
- **Day 2 설정**: `STRATEGY_KR_DAILY_CAP=40`, `GEN_DAILY_CALL_CAP=1500`
- **기동 주의**: `set -a && source .env && source config/phase10_kr_micro.env && set +a` 필수
  (단순 `source`만 하면 env var가 child process에 전달 안 됨)

### Day 2 (2026-03-13)
- emit_rate=22.7% ✅, candidate=52, strategy_pass=40, pipeline_error=0 ✅
- executable=0건 (KIS ACCOUNT_SNAPSHOT_ERROR — available_cash=0)
- **이슈 1**: Anthropic API 크레딧 부족 → 10:02 AI_ERROR_SPIKE auto-pause → 재충전 후 재개
- **이슈 2**: KIS available_cash=0 → ACCOUNT_SNAPSHOT_ERROR → 총 103,996원 입금 완료
- **운영 팁**: REDIS_PASSWORD가 .env에 없음 → REDIS_URL에서 추출 필요
  ```bash
  REDIS_PASS=$(python3 -c "import urllib.parse,os; u=urllib.parse.urlparse(os.environ['REDIS_URL']); print(u.password or '')")
  docker exec claw-redis redis-cli -a "$REDIS_PASS" GET claw:pause:global
  ```
- **Day 3 세팅**: 변경 없음 (daily_cap=40, AI cap=1500, cooldown=600s, max_concurrent=2)
- **Day 3 목표**: executable 3~10건 + Risk reject 정상 분포(max_concurrent/cooldown/daily_cap) 확인 → Phase 10 exit
- **GPT 권고**: AI call 효율(1500calls/52candidates=28.8 calls/candidate) 개선은 Phase 11에서

### Day 3 (2026-03-16)
- candidate=24, strategy_pass=21, pipeline_error=0 ✅
- executable=0건 ❌ — 복합 원인 (아래 이슈 참조)
- **이슈 1**: Day 2 auto-pause 미해제 → 08:40~10:22 consensus 완전 차단 (2시간 손실)
  - 조치: `DEL claw:pause:global` 수동 해제
- **이슈 2**: KIS `ord_psbl_cash` 필드 없음 → `available_cash=0` → ACCOUNT_SNAPSHOT_ERROR
  - 수정: `ord_psbl_cash or dnca_tot_amt` fallback (commit `8ec78d7`)
- **이슈 3**: `allocation_cap_pct=20%` → cap=20,903원 → 거의 모든 종목 ALLOCATION_CAP_EXCEEDED
  - 수정: `RISK_KR_ALLOCATION_CAP_PCT=1.00` env var 추가 (commit `3da23b2`)
- **이슈 4**: 워치리스트 고가주(SK하이닉스 8만↑, NAVER 18만↑ 등) → 잔고 104,516원으로 주문 불가
  - 수정: 주가 10만원 이하 종목으로 교체 `005930,105560,055550,086790,034020,010950,035720,032640`
- **이슈 5**: 워치리스트 교체 후 `market_data_runner` 미재시작 → 새 종목 Redis 데이터 없음 → 005930만 평가
  - 조치: market_data_runner 재시작 (장 마감 후 → 내일부터 새 종목 데이터 수집)
- **운영 규칙 추가**: 워치리스트 변경 시 **market_data_runner 포함 전체 프로세스** 재시작 필수
- **Day 4 세팅**: 워치리스트 변경 외 동일 (daily_cap=40, AI cap=1500, cooldown=600s, max_concurrent=2, allocation_cap=100%)
- **Day 4 목표**: 새 워치리스트 기반 신호 생성 + executable 3~10건 확인 → Phase 10 exit

### Day 4 (2026-03-17) — Phase 10 최종일
- 08:49 전체 9개 프로세스 기동 완료 ✅
- pause=nil ✅, ai_pause=nil ✅, 큐 비어있음 ✅, allocation_cap=104,516원(100%) ✅
- 구형 고가주 잔류 신호(000660/207940 등) → ALLOCATION_CAP_EXCEEDED로 자동 소진 (영향 없음)
- 장 시작 전 모든 점검 통과 — 새 워치리스트 첫 온전한 하루
- candidate=54, strategy_pass=34, pipeline_error=0 ✅
- **executable=1건 ✅** — KIS 실매수 체결 확인 (신한지주 91,200원, 구형 잔류 신호)
- 체결 후 잔고 ~11,787원 → 매도 후 ~100,000원 복구
- **이슈 1**: KIS 403 tokenP 한도 초과 → MD_ERROR_SPIKE(delta=147) → Redis 토큰 캐싱으로 영구 수정 (commit `8f80255`)
- **이슈 2**: AI_CALL_CAP_EXCEEDED 13:13 auto-pause (call_count=-1, cap=1500) → AI cap 소진 시 pause 유지 (운영 규칙)
- **워치리스트 교체**: 3만원 이하 8종목 `.env` 반영 (`010140,015760,003490,034220,011200,004020,000080,009830`)

---

## 8. Phase 10 EXIT 선언 (2026-03-17)

**Phase 10 = EXIT 완료 ✅**

### GPT(Phase 9.5 진입 및 우선순위 스레드) 판단 근거
- end-to-end pipeline verified ✅
- KIS execution confirmed (Day4 실매수 체결) ✅
- pipeline_error = 0 ✅
- major bugs fixed ✅

### 4일 운영 총평
| Day | candidate | strategy_pass | executable | 주요 이슈 |
|-----|-----------|---------------|------------|----------|
| 1   | 54        | 20            | 0          | daily_cap 20→40 조정 |
| 2   | 52        | 40            | 0          | available_cash 파싱 오류, API 크레딧 |
| 3   | 24        | 21            | 0          | 5개 이슈 동시 발생 (전부 수정) |
| 4   | 54        | 34            | 1 ✅       | KIS 실매수 체결 확인 |

### Phase 10에서 수정/완료된 항목
- KIS available_cash 파싱 오류 수정 (`ord_psbl_cash or dnca_tot_amt` fallback)
- `allocation_cap_pct` 100% env var 지원
- 워치리스트 → 3만원 이하 종목으로 교체
- KIS 토큰 Redis 캐싱 (재시작 시 403 방지)
- `cancel_order` 404 graceful 처리
- `_set_auto_pause` TG 스팸 수정 (idempotent)
- AI call 효율 개선은 Phase 11로 이관

### Phase 11 핵심 과제 (GPT 권고)
- **핵심 질문**: candidate 50건에서 execution이 거의 안 나오는 이유 분석
- symbol cooldown / evaluation throttle / duplicate eval 방지 → AI call 40~60% 감소 예상
- 잔고 충전 후 정상 실거래 환경 구성
