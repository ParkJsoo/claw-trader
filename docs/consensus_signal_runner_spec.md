# consensus_signal_runner Responsibility Boundary
Version: 0.1-draft  
Date: 2026-03-10  
Status: Draft

---

## 1. 문서 목적

본 문서는 `consensus_signal_runner`의 책임 범위를 명확히 정의하여,
기존 StrategyEngine / RiskEngine / OrderExecutor와의 경계를 분리하고
중복 게이트 구현을 방지하기 위해 작성되었다.

핵심 목표는 다음과 같다.

- runner를 얇고 신뢰 가능한 candidate generator로 유지
- 기존 엔진과 책임 충돌 방지
- 로그 해석 단순화
- 향후 유지보수 및 확장성 확보

---

## 2. 핵심 정의

`consensus_signal_runner`는 dual-LLM 결과를 입력받아,
최소 프리필터를 통과한 경우에만 **candidate signal**을 생성하고 downstream으로 전달하는 컴포넌트다.

즉, runner는 **최종 매수 결정기**가 아니다.  
runner는 **candidate signal 생성기**다.

---

## 3. 설계 원칙

### 3.1 runner는 스펙의 실행기다
runner는 전략과 리스크 정책을 새로 정의하지 않는다.  
이미 Phase 10 spec에 정의된 최소 규칙을 기계적으로 적용한다.

### 3.2 runner는 얇아야 한다
runner가 비대해질수록 아래 문제가 생긴다.

- StrategyEngine와 역할 충돌
- RiskEngine와 정책 중복
- reject 이유 추적 어려움
- 수정 지점 분산
- 운영 일관성 저하

### 3.3 중복 게이트를 만들지 않는다
이미 StrategyEngine / RiskEngine에 존재하는 정책을 runner에 다시 구현하지 않는다.

### 3.4 후보 생성과 실행 승인을 분리한다
candidate 생성과 실행 승인은 다른 책임이다.  
이 경계를 유지해야 실전 운영 시 원인 분석이 쉬워진다.

---

## 4. 입력 / 출력

## 입력
runner는 최소한 다음 정보를 입력으로 받는다.

- symbol
- timestamp
- claude_emit
- qwen_emit
- ret_5m
- range_5m
- 기타 candidate 생성에 필요한 기본 market context

## 출력
runner 출력은 **candidate signal**이다.

- status = candidate
- source = consensus_signal_runner
- signal_id = existing UUID format
- downstream consumer가 Strategy / Risk / Executor로 이어질 수 있는 최소 필드 포함

---

## 5. runner가 해야 하는 일 (In Scope)

아래는 runner의 명시적 책임이다.

### 5.1 dual consensus 확인
- `claude_emit == 1`
- `qwen_emit == 1`

두 모델이 모두 EMIT일 때만 다음 단계로 진행한다.

### 5.2 최소 prefilter 적용
아래 필터는 runner에서 직접 적용한다.

- `ret_5m > 0`
- `range_5m > 0.004`

이 필터는 candidate 생성의 최소 조건이며, 전략/리스크 정책이라기보다 신호 후보 생성 전제 조건으로 본다.

### 5.3 데이터 무결성 확인
예:
- symbol 누락 여부
- timestamp 누락 여부
- 필수 numeric field 존재 여부
- NaN / invalid 값 방지
- 모델 결과 파싱 이상 여부

### 5.4 candidate signal 정규화
runner는 downstream 사용을 위해 signal payload를 정규화한다.

예:
- UUID signal_id 할당
- market 지정
- source 지정
- status 지정
- 공통 schema 맞춤

### 5.5 enqueue / publish
정규화된 candidate signal을 queue / stream / redis channel 등 기존 downstream 인터페이스로 전달한다.

### 5.6 로깅
아래 이벤트는 최소한 로깅한다.

- consensus 불충족
- prefilter 탈락
- candidate 생성 성공
- payload validation 실패
- publish 실패

---

## 6. runner가 하지 말아야 하는 일 (Out of Scope)

아래는 runner 책임이 아니다.

### 6.1 session gating
예:
- 09:30 이전 차단
- 11:00 이후 차단

이것은 StrategyEngine 책임이다.

### 6.2 watchlist 정책 판단
예:
- watchlist 포함 여부
- 섹터/종목군 정책
- 우선순위 종목 판단

이것은 StrategyEngine 책임이다.

### 6.3 cooldown / re-entry 판단
예:
- 같은 종목 10분 재진입 제한
- 최근 진입 여부
- 동일 방향 중복 진입 방지

이것은 StrategyEngine 책임이다.

### 6.4 position 존재 여부 확인
예:
- 이미 포지션이 열려 있는가
- 동시 보유 제한에 걸리는가

이것은 RiskEngine 책임이다.

### 6.5 max_positions / max_trades_per_day
이것은 RiskEngine 책임이다.

### 6.6 daily loss / exposure / cash guard
이것은 RiskEngine 책임이다.

### 6.7 order sizing / stop / take profit / order placement
이것은 OrderExecutor 또는 execution policy 책임이다.

---

## 7. 권장 처리 흐름

```text
upstream dual eval
  → consensus_signal_runner
  → candidate_signal
  → StrategyEngine
  → RiskEngine
  → OrderExecutor
```

runner는 첫 번째 변환 레이어다.

- raw dual eval result를 받는다
- minimum candidate 조건만 확인한다
- normalized candidate_signal을 생성한다
- downstream에 전달한다

그 이후의 승인 / 거절은 runner 밖에서 일어난다.

---

## 8. reject / pass 의미 정리

### runner reject
runner에서 reject되었다는 것은 다음 의미다.

- consensus 자체가 성립하지 않았거나
- candidate 최소 조건이 안 맞았거나
- 데이터가 불완전하다는 뜻

즉, **신호 후보로 올릴 가치도 없음**을 의미한다.

### Strategy reject
candidate는 됐지만 전략 정책상 받지 않는 경우다.

예:
- 세션 외 시간
- cooldown 중
- watchlist 정책 미충족

### Risk reject
전략적으로는 받을 수 있지만 실행 리스크상 거부하는 경우다.

예:
- max_positions 초과
- 당일 거래 한도 초과
- cash / exposure 문제

이 구분이 유지되어야 로그 해석이 쉬워진다.

---

## 9. 로깅 권장 포맷

최소한 아래 분류가 있으면 좋다.

- `runner.reject.consensus_failed`
- `runner.reject.prefilter_ret_5m`
- `runner.reject.prefilter_range_5m`
- `runner.reject.invalid_payload`
- `runner.pass.candidate_created`
- `runner.error.publish_failed`

이벤트 로그에는 가능하면 아래를 포함한다.

- symbol
- timestamp
- claude_emit
- qwen_emit
- ret_5m
- range_5m
- signal_id(생성 후)
- reason_code

---

## 10. Candidate Signal Minimum Schema

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
  "range_5m": 0.0052
}
```

---

## 11. 구현 체크리스트

### 필수
- dual consensus 확인
- prefilter 적용
- invalid payload 방어
- UUID 유지
- candidate schema 정규화
- enqueue / publish
- reject / pass 로그 구분

### 금지
- StrategyEngine 정책 복제
- RiskEngine 정책 복제
- executor 로직 혼입
- 중복 cooldown 구현
- position gate 재구현

---

## 12. 예시 의사결정

### case 1
- claude_emit = 1
- qwen_emit = 1
- ret_5m = 0.002
- range_5m = 0.005

결과:
- candidate 생성 가능

### case 2
- claude_emit = 1
- qwen_emit = 0

결과:
- runner reject
- reason = consensus_failed

### case 3
- claude_emit = 1
- qwen_emit = 1
- ret_5m = -0.001
- range_5m = 0.006

결과:
- runner reject
- reason = prefilter_ret_5m

### case 4
- candidate는 생성됨
- 하지만 현재 시간이 09:12

결과:
- runner pass
- 이후 StrategyEngine reject 가능

### case 5
- candidate는 생성됨
- 전략 승인도 됨
- 하지만 동시 포지션 수 초과

결과:
- runner pass
- Strategy pass
- Risk reject

---

## 13. 최종 원칙

한 문장으로 정리하면:

**consensus_signal_runner는 "합의된 dual eval 결과를 최소 조건으로 정규화해 candidate signal로 만드는 레이어"이며, 전략/리스크/주문 실행 정책의 소유자가 아니다.**

이 원칙을 유지하면 다음이 가능해진다.

- 구조 단순화
- 디버깅 용이성
- 정책 수정 시 영향 범위 축소
- 기존 코드베이스 재사용 극대화
- Phase 10 이후 확장성 확보

---

## 14. 구현 참고 — 의사코드

```python
def run_once(market: str, symbol: str, r: Redis) -> Optional[dict]:
    # 1. dual eval 결과 읽기
    claude = r.hgetall(f"ai:dual:last:claude:{market}:{symbol}")
    qwen   = r.hgetall(f"ai:dual:last:qwen:{market}:{symbol}")
    if not claude or not qwen:
        return None

    # 2. dual consensus 확인
    if claude.get("emit") != "1" or qwen.get("emit") != "1":
        log("runner.reject.consensus_failed", symbol=symbol)
        return None

    # 3. 방향 일치 확인
    if claude.get("direction") != qwen.get("direction"):
        log("runner.reject.direction_mismatch", symbol=symbol)
        return None

    # 4. entry prefilter
    ret_5m   = float(claude.get("ret_5m") or 0)
    range_5m = float(claude.get("range_5m") or 0)
    if ret_5m <= 0:
        log("runner.reject.prefilter_ret_5m", symbol=symbol, ret_5m=ret_5m)
        return None
    if range_5m <= 0.004:
        log("runner.reject.prefilter_range_5m", symbol=symbol, range_5m=range_5m)
        return None

    # 5. candidate_signal 생성
    signal = {
        "signal_id":     str(uuid.uuid4()),
        "market":        market,
        "symbol":        symbol,
        "timestamp":     datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "ts_ms":         str(int(time.time() * 1000)),
        "source":        "consensus_signal_runner",
        "status":        "candidate",
        "consensus":     "EMIT",
        "claude_emit":   1,
        "qwen_emit":     1,
        "direction":     claude.get("direction", "LONG"),
        "ret_5m":        str(ret_5m),
        "range_5m":      str(range_5m),
        "current_price": claude.get("current_price", ""),
    }

    # 6. Redis enqueue (기존 파이프라인 재사용)
    r.lpush("claw:signal:queue", json.dumps(signal))
    log("runner.pass.candidate_created", signal_id=signal["signal_id"], symbol=symbol)

    # 7. audit 기록
    _save_audit(r, market, signal)
    return signal
```

---

## 15. Redis 키

```
# 입력 (ai_dual_eval_runner가 기록)
ai:dual:last:claude:{market}:{symbol}       # Claude 최신 판단 (hash)
ai:dual:last:qwen:{market}:{symbol}         # Qwen 최신 판단 (hash)

# 출력
claw:signal:queue                           # 기존 signal 파이프라인 큐 재사용
consensus:audit:{market}:{signal_id}        # 감사 로그 (TTL 7d)
consensus:stats:{market}:{YYYYMMDD}         # 일별 통계 (candidate/reject_* 카운트)
consensus:daily_count:{market}:{YYYYMMDD}   # 일별 candidate 생성 수
consensus:runner:lock                       # 프로세스 락 (TTL 120s)
```

---
