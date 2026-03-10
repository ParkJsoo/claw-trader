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
runner: config kr_cooldown=600s kr_daily_cap=20 kr_max_concurrent=2 kr_daily_loss_limit=-500000
```

값이 다르면 → `source config/phase10_kr_micro.env` 후 재기동.

### ✅ 3. 실거래 차단 확인 (가장 중요)

```bash
redis-cli GET claw:pause:global
# 반드시 → true
```

`true`가 아니면 dry-run 시작 금지.

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
