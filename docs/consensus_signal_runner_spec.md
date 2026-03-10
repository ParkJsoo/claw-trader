# consensus_signal_runner — 책임 범위 문서

> 작성일: 2026-03-10
> 작성 기반: GPT 협의 + Claude Code 검토 확정
> 상태: ✅ 확정 (구현 전 고정)

---

## 역할 정의

**consensus_signal_runner는 candidate signal 생성기다.**

dual LLM 평가 결과(ai_dual_eval)를 신호 후보(candidate_signal)로 정규화하여
Redis에 enqueue하는 얇은 레이어.

> runner는 판단 결과를 정규화하고, Strategy/Risk가 채택 여부를 결정한다.

---

## 파이프라인 위치

```
ai_dual_eval_runner
      ↓  (ai:dual:last:{provider}:{market}:{symbol})
consensus_signal_runner   ← 여기
      ↓  (claw:signal:queue 또는 별도 candidate queue)
StrategyEngine
      ↓
RiskEngine
      ↓
OrderExecutor
```

---

## ✅ runner가 담당하는 것

| 항목 | 설명 |
|------|------|
| dual consensus 확인 | `claude_emit == 1 AND qwen_emit == 1` |
| entry prefilter | `ret_5m > 0 AND range_5m > 0.004` |
| 필드 무결성 체크 | symbol, price, ts_ms 누락 여부 |
| signal_id 생성 | UUID (기존 코드베이스 포맷 유지) |
| candidate_signal 구성 | 정규화된 신호 dict 생성 |
| Redis enqueue | candidate_signal → queue push |
| 감사 로그 저장 | Redis audit key 기록 |

---

## ❌ runner가 담당하지 않는 것

| 항목 | 담당 레이어 |
|------|------------|
| session 시간 검증 (09:30~11:00) | StrategyEngine |
| symbol cooldown (10분) | StrategyEngine |
| watchlist membership 확인 | StrategyEngine |
| 중복 신호 dedupe | StrategyEngine |
| max_positions (2) | RiskEngine |
| max_trades_per_day (6) | RiskEngine |
| daily loss guard | RiskEngine |
| available cash 확인 | RiskEngine |
| stop_loss 정책 | RiskEngine |
| 실제 주문 수량 계산 | OrderExecutor |
| KIS API 호출 | OrderExecutor |

---

## 구현 의사코드

```python
def run_once(market: str, symbol: str, r: Redis):
    # 1. dual eval 결과 읽기
    claude = r.hgetall(f"ai:dual:last:claude:{market}:{symbol}")
    qwen   = r.hgetall(f"ai:dual:last:qwen:{market}:{symbol}")

    if not claude or not qwen:
        return None  # 데이터 없음

    # 2. dual consensus 확인
    claude_emit = claude.get("emit") == "1"
    qwen_emit   = qwen.get("emit") == "1"
    if not (claude_emit and qwen_emit):
        return None  # consensus 불성립

    # 3. 방향 일치 확인
    if claude.get("direction") != qwen.get("direction"):
        return None  # 방향 불일치

    # 4. entry prefilter (코드 레벨)
    ret_5m    = float(claude.get("ret_5m", 0) or 0)
    range_5m  = float(claude.get("range_5m", 0) or 0)
    if ret_5m <= 0 or range_5m <= 0.004:
        return None  # prefilter 차단

    # 5. candidate_signal 생성
    signal = {
        "signal_id":    str(uuid.uuid4()),
        "market":       market,
        "symbol":       symbol,
        "timestamp":    datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "ts_ms":        str(int(time.time() * 1000)),
        "consensus":    "EMIT",
        "claude_emit":  1,
        "qwen_emit":    1,
        "direction":    claude.get("direction", "LONG"),
        "ret_5m":       str(ret_5m),
        "range_5m":     str(range_5m),
        "current_price": claude.get("current_price", ""),
        "source":       "consensus_signal_runner",
        "status":       "candidate",
    }

    # 6. Redis enqueue
    r.lpush("claw:signal:queue", json.dumps(signal))

    # 7. audit 기록
    _save_audit(r, market, signal)
    return signal
```

---

## Redis 키

```
# 입력 (ai_dual_eval_runner가 기록)
ai:dual:last:claude:{market}:{symbol}   # Claude 최신 판단
ai:dual:last:qwen:{market}:{symbol}     # Qwen 최신 판단

# 출력
claw:signal:queue                        # 기존 파이프라인 재사용
consensus:audit:{market}:{signal_id}    # 감사 로그 (TTL 7d)
consensus:stats:{market}:{YYYYMMDD}     # 일별 통계
consensus:daily_count:{market}:{YYYYMMDD}  # 일별 candidate 생성 수
```

---

## 설계 원칙

1. **얇게 유지**: runner 비대화 방지. 새 로직은 Strategy/Risk에 추가할 것
2. **기존 레이어 재사용**: cooldown/position/risk는 runner에 다시 구현하지 않음
3. **추적 가능성**: signal_id로 전체 파이프라인 추적 가능하게 유지
4. **실패 격리**: 개별 symbol 실패가 전체 loop에 영향 주지 않음
5. **dry-run 지원**: `DRY_RUN=true` 환경변수 시 enqueue 없이 로그만

---

## 폴링 주기

```
GEN_DUAL_POLL_SEC=120  # 2분 (ai_eval_runner와 동일)
```

---

## dry-run 검증 기준

| 지표 | 목표 |
|------|------|
| 하루 최종 executable_signal 수 | 3~10건 |
| StrategyEngine 통과율 | 확인 (session/cooldown 영향) |
| RiskEngine 통과율 | 확인 (position/daily 영향) |
| 동일 종목 반복 신호 | cooldown 정상 차단 확인 |
| 세션 boundary (09:30/11:00) | 정확히 게이팅되는지 확인 |
