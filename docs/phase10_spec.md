# Claw-Trader Phase 10 Spec Draft
Version: 0.1-draft  
Date: 2026-03-10  
Status: Draft for review before 2026-03-11 market open

---

## 1. 목적

Phase 10의 목적은 dual-LLM 합의 결과를 실제 트레이딩 가능한 **candidate signal → approved signal → executable signal** 흐름으로 연결하고, KR micro trading 환경에서 안전하게 dry-run 및 초기 실거래 진입이 가능하도록 운영 스펙을 고정하는 것이다.

본 문서는 **운영 규칙**, **책임 경계**, **기본 수치**, **검증 포인트**를 정의한다.

---

## 2. 적용 범위

- Market: KR only
- Trading mode: micro trading
- Execution scope: small-size live / paper-like controlled execution
- Data focus: KR signal pipeline
- Out of scope:
  - US live trading 확장
  - IBKR real-time market data 전환
  - watchlist 대규모 확장
  - multi-model voting 확장(3-LLM 이상)

---

## 3. Phase 10 핵심 목표

1. dual eval 합의 결과를 실제 신호 후보(candidate signal)로 변환한다.
2. StrategyEngine / RiskEngine / OrderExecutor와 자연스럽게 연결한다.
3. runner는 얇게 유지하고, 기존 엔진 책임을 침범하지 않는다.
4. 장중 신호 흐름이 예측 가능하고 로깅 가능한 상태를 만든다.
5. KR micro trading을 제한된 범위에서 안전하게 시작할 수 있는 운영 기준을 고정한다.

---

## 4. 운영 원칙

### 4.1 설계 원칙
- consensus_signal_runner는 **스펙의 실행기**이며, 전략/리스크 정책의 소유자가 아니다.
- runner는 **candidate signal 생성기**로 동작한다.
- session gating, re-entry 정책, cooldown 정책, position 제한, daily risk 제한은 기존 StrategyEngine / RiskEngine이 담당한다.
- 중복 게이트를 새로 만들지 않는다.
- 기존 코드베이스의 책임 구조를 유지한다.

### 4.2 안전 원칙
- 초기 단계에서는 신호 수보다 **신호 품질**과 **운영 안정성**을 우선한다.
- watchlist 확대보다 파이프라인 안정화가 우선이다.
- 실거래 진입 시에도 size는 최소 단위로 시작한다.
- 신호 생성 로직과 주문 실행 로직을 강하게 분리한다.

---

## 5. 시장 / 세션 스펙

- Market: KR
- Session window: 09:30 ~ 11:00 KST
- Session ownership: StrategyEngine
- Session 외 시간대 candidate 생성은 가능하더라도, 실제 진입 승인 여부는 StrategyEngine이 결정한다.

### 세션 운영 의도
- 장 초반 극초기 노이즈(09:00~09:30)를 피한다.
- 유동성과 변동성이 아직 살아 있는 구간만 사용한다.
- 점심 전 과도한 noise / decay 구간 진입을 제한한다.

---

## 6. 종목 / 포지션 스펙

### 6.1 Watchlist
- Initial watchlist size: 8 symbols
- Watchlist ownership: StrategyEngine / configuration layer
- Phase 10 동안 watchlist는 고정한다.
- 8 → 10 확장은 Phase 10 안정화 이후 별도 검토한다.

### 6.2 Position sizing
- Entry size: 1주 단위
- Position sizing ownership: Order sizing / execution policy
- 이유:
  - Phase 10은 수익 극대화가 아니라 실행 검증 단계
  - signal quality와 executor 안정성을 먼저 본다

### 6.3 Max positions
- Recommended default: 2
- Ownership: RiskEngine

#### rationale
- 1개는 지나치게 보수적이라 관찰 데이터가 부족할 수 있음
- 3개 이상은 micro trading 초기 단계에서 분산보다 운영 복잡성을 키움
- 2개는 리스크와 관찰 효율의 균형점

### 6.4 Max trades per day
- Recommended default: 6
- Ownership: RiskEngine

#### rationale
- 초기 단계에서 과도한 churn 방지
- candidate가 많더라도 최종 실행 수를 제한해 로그 해석과 운영 안정성을 확보

---

## 7. 진입 스펙

### 7.1 Entry source
- entry_source = consensus only
- dual eval 결과가 모두 EMIT일 때만 candidate 생성 가능

### 7.2 Runner prefilter
runner는 아래 조건을 모두 만족할 때만 candidate signal 생성 가능:

- claude_emit == 1
- qwen_emit == 1
- ret_5m > 0
- range_5m > 0.004
- 필수 필드 존재(symbol, timestamp, ret_5m, range_5m, model outputs 등)
- 데이터 무결성 이상 없음

### 7.3 Entry ownership
- candidate 생성: consensus_signal_runner
- 전략 승인: StrategyEngine
- 리스크 승인: RiskEngine
- 주문 실행: OrderExecutor

---

## 8. 재진입 / cooldown 정책

### 기본 원칙
- cooldown / re-entry 정책은 runner 소유가 아니다.
- runner는 candidate를 생성할 뿐, 종목별 반복 진입 제한을 직접 집행하지 않는다.

### Recommended default
- symbol cooldown: 10분
- ownership: StrategyEngine

#### rationale
- 5분은 너무 촘촘해서 동일 흐름 내 반복 진입 가능성이 큼
- 15분은 micro momentum 전략에서 기회를 과도하게 줄일 수 있음
- 10분은 초기 운영에서 가장 균형적

### note
- 기존 StrategyEngine에 이미 유사 정책이 있으면 그 구현을 재사용한다.
- 동일 정책을 runner에 중복 구현하지 않는다.

---

## 9. 손절 / 청산 스펙

### stop loss
- stop_loss = -2.0%
- ownership: Risk / execution policy

### take profit
- Phase 10 기본값: 고정 take profit 없음 또는 기존 exit policy 재사용
- 이유:
  - 현재 단계의 핵심은 entry pipeline 검증
  - 익절 최적화는 Phase 10 안정화 이후 별도 정교화 가능

### exit ownership
- exit signal / stop handling / protective execution은 기존 Risk / Executor 구조를 따른다.

---

## 10. Emit-rate 운영 해석

### 현 상태
- post-fix emit_rate: 27.7%
- 목표 범위: 10% ~ 30%
- 현재 값은 허용 범위 내이며 즉시 추가 축소가 필수는 아니다.

### 운영 판단
- Phase 10 진입 시점에서는 emit_rate를 더 낮추는 것보다,
  **candidate → approved → executable 전환율**을 먼저 보는 것이 맞다.

### 정책
- 당장 목표를 20% 이하로 추가 축소하지 않는다.
- 다만 dry-run 결과 executable signal 수가 과도하면 이후 재조정 가능하다.

---

## 11. Candidate Signal Schema Draft

주의: `signal_id`는 기존 코드베이스의 **UUID 포맷을 유지**한다.  
새로운 커스텀 문자열 포맷을 도입하지 않는다.

```json
{
  "signal_id": "uuid",
  "market": "KR",
  "symbol": "005930",
  "timestamp": "2026-03-11T09:35:00+09:00",
  "source": "consensus_signal_runner",
  "status": "candidate",
  "consensus": "EMIT",
  "claude_emit": 1,
  "qwen_emit": 1,
  "ret_5m": 0.0031,
  "range_5m": 0.0052,
  "meta": {
    "schema_version": "phase10-v1",
    "watchlist_name": "kr_micro_v1"
  }
}
```

### required fields
- signal_id
- market
- symbol
- timestamp
- source
- status
- consensus
- claude_emit
- qwen_emit
- ret_5m
- range_5m

### optional fields
- volume_ratio
- spread
- raw model reasons
- trace ids
- upstream eval ids

---

## 12. 책임 경계 요약

### consensus_signal_runner
- dual consensus 확인
- 최소 프리필터 적용
- candidate_signal 생성
- enqueue / publish

### StrategyEngine
- session gating
- watchlist / symbol policy
- re-entry / cooldown
- 전략적 승인 여부

### RiskEngine
- max_positions
- max_trades_per_day
- daily loss guard
- exposure / cash / execution safety

### OrderExecutor
- 수량 계산
- 주문 생성
- 주문 전송
- 주문 결과 추적

---

## 13. Dry-run 검증 항목

### 필수 검증
- candidate_signal 생성 건수
- Strategy 승인 건수
- Risk 승인 건수
- executable_signal 최종 건수
- 종목별 반복 signal 패턴
- session 시간대 분포
- 로그 가독성
- duplicate / burst 여부
- candidate 생성 시 필드 누락 여부

### 관찰 목표
- 최종 executable signal 수가 과도하지 않은가
- 동일 종목에서 짧은 시간 반복 candidate가 과도한가
- session 경계 전후 이상 신호가 있는가
- RiskEngine reject 이유가 명확히 남는가

---

## 14. 비목표(Non-goals)

Phase 10에서는 아래를 목표로 하지 않는다.

- 수익률 최적화
- 종목 수 확대
- US 실시간 데이터 정착
- 3모델 이상 voting
- take profit 고도화
- 대형 포지션 운영

---

## 15. 내일 장 전 우선순위

1. 본 스펙 리뷰 및 잠금
2. responsibility boundary 확정
3. candidate_signal schema 확정
4. consensus_signal_runner 구현
5. Strategy / Risk 재사용 연결
6. dry-run 준비
7. 장중 관찰

---

## 16. 최종 결정 요약

- 진행 순서: **C → A → dry-run → 장중 확인**
- max_positions: **2**
- max_trades_per_day: **6**
- symbol cooldown: **10분**
- entry prefilter: **ret_5m > 0 AND range_5m > 0.004**
- emit_rate는 당장 더 낮추지 않고 현 상태로 운영 검증
- signal_id는 기존 **UUID 유지**
- B(IBKR live), D(watchlist 확장)는 Phase 10 안정화 이후 검토

---
