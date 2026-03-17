# Phase 11 로드맵 — Execution Rate 개선
Version: 0.1
Date: 2026-03-17
Status: Active

---

## 목표

**Phase 10에서 확인된 핵심 문제**:
- candidate ≈ 50, strategy_pass ≈ 20~40, executable ≈ 0~1
- execution_rate ≈ 2% (정상 자동매매 시스템: 10~30%)

**Phase 11 목표**:
- execution_rate: 2% → 10~20%
- AI call/day: 1500 → 600~900
- emit_rate: 10~25% 유지
- pipeline_error: 0

**핵심 진단 (GPT)**:
> "Signal은 많이 나오는데 실행이 거의 안 된다"
> → 게이트 구조(Strategy/Risk)와 Signal 품질이 안 맞는 상태
> → Phase 11은 "기능 추가"가 아니라 **효율 최적화 + 실행률 개선 단계**

---

## 원인 분석

| 원인 | 현재 상태 | 영향 |
|------|----------|------|
| Signal 품질 (느슨) | ret_5m > 0 (음수만 제거) | "약한 상승"도 emit → 대부분 탈락 |
| 중복 Signal | symbol-level cooldown 없음 | 같은 종목 반복 emit → cooldown 막힘 |
| Strategy cooldown 과도 | 600초 (10분) | 단타에서 너무 길어 execution 차단 |
| Risk Gate 잔존 | max_concurrent=2, 잔고 제한 | 실행 가능 후보 자체 제한 |

---

## Phase 11 로드맵 (우선순위 순)

### 🥇 Phase 11-1 (최우선): execution drop reason 로그 추가

**원인 추적 없이는 개선 불가능.**

로그 추가 위치:
- `src/executor/core.py` — risk reject 시 reason 기록
- `src/strategy/engine.py` — strategy reject 시 reason 기록

추가할 로그 구조:
```json
{
  "symbol": "005930",
  "dropped_by": "strategy:COOLDOWN",
  "detail": "cooldown=600s, last_signal=300s ago"
}
```

Redis 키:
```
execution_drop:KR:{YYYYMMDD}  # hash: {reason: count}
```

**관찰 지표**:
- candidate → strategy_pass rate
- strategy_pass → executable rate
- drop reason distribution

---

### 🥈 Phase 11-2: Signal 필터 강화 + symbol cooldown

#### 2-1. Signal 필터 임계값 강화

현재:
```
ret_5m > 0
range_5m > 0.004
```

추천:
```
ret_5m > 0.001 ~ 0.002  (애매한 상승 제거)
range_5m > 0.004  (유지)
volume 증가 조건 추가 (optional)
```

수정 위치: `src/app/consensus_signal_runner.py` — `_MIN_RET_5M`

#### 2-2. Symbol-level cooldown 도입

consensus_signal_runner에서 Redis 기반 심볼별 쿨다운:
```
consensus:symbol_cooldown:{market}:{symbol}  # TTL = 180s
```

효과:
- AI call 감소
- 중복 signal 제거
- execution rate 상승

---

### 🥉 Phase 11-3: AI call 최적화

#### 3-1. Evaluation throttle 증가
- 현재: 30초 폴링
- 추천: 60초 폴링
- 수정: `CONSENSUS_POLL_SEC=60`

#### 3-2. AI 호출 전 prefilter 강화
AI 평가 전 걸러낼 조건:
- ret_1m 기준
- volume spike 확인
- spread 체크

#### 3-3. Symbol priority
- 전 종목 평가 → 상위 N개만 (거래대금, 모멘텀 기준)

---

### 🔧 Phase 11-4: Strategy/Risk 파라미터 재조정

| 파라미터 | 현재 | 추천 | 이유 |
|----------|------|------|------|
| cooldown | 600초 | 300초 | 단타에서 너무 긺, execution 주요 차단 원인 |
| daily_cap | 40 | 20~30 | 잔고 제한 환경에서 의미 없는 수치 |
| max_concurrent | 2 | 2 유지 | 적정 수준 |

---

## 진행 순서 (중요 — 이 순서 지켜야 원인 추적 가능)

```
1. execution drop reason 로그 추가  ← 먼저!
2. symbol cooldown 적용
3. signal threshold 강화 (ret_5m > 0.001)
4. AI call 최적화 (poll 60초, prefilter 강화)
5. cooldown 300초 조정
```

> **주의**: 순서 안 지키면 원인 추적 꼬인다 (GPT 권고).

---

## Phase 11 KPI

| 지표 | Phase 10 결과 | Phase 11 목표 |
|------|--------------|--------------|
| execution_rate | ~2% | ≥ 10% |
| AI call/day | ~1500 | ≤ 900 |
| emit_rate | 22~27% | 10~25% |
| pipeline_error | 0 ✅ | 0 |
| candidate/day | 24~54 | 20~40 |

---

## Phase 11 exit 조건

2거래일 연속:
- [ ] execution_rate ≥ 10%
- [ ] AI call/day ≤ 900
- [ ] pipeline_error = 0
- [ ] drop reason 분포 정상 (COOLDOWN/MAX_CONCURRENT 위주)

---

## Day별 운영 기록

### Day 1 (2026-03-xx)
- TBD
