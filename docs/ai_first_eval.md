# AI-First Eval Runner

> **모드**: AI-First / No-Trade — `claw:pause:global=true` 유지 상태에서 AI 평가만 수행.
> 주문/Executor는 절대 호출하지 않음.

---

## 실행

```bash
cd /Users/henry_oc/develop/claw-trader/src
PYTHONPATH=src python -m app.ai_eval_runner
```

## 환경변수 (선택)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `EVAL_POLL_SEC` | `120` | 폴링 간격(초) |
| `EVAL_DAILY_CALL_CAP` | `2000` | 시장별 일일 AI 호출 상한 |
| `EVAL_STATUS_LOG_SEC` | `600` | 상태 로그 출력 간격(초) |
| `GEN_MIN_HIST` | `20` | cold-start 가드 최소 히스토리 수 (signal_generator와 공유) |

> 2종목 · 120초 폴링 기준: 하루 약 1,440 calls/market → cap 2,000 이내 ✅

---

## 주요 Redis 키

| 키 | 형식 | 설명 |
|----|------|------|
| `ai:eval:last:{market}:{symbol}` | HASH | 심볼별 최신 AI 판단 (overwrite) |
| `ai:eval_log:{market}:{YYYYMMDD}` | LIST | 일일 판단 로그 (최신 우선, 최대 500개) |
| `ai:eval_stats:{market}:{YYYYMMDD}` | HASH | 일별 통계 (emit/no_emit/error/skip_*) |
| `ai:eval_call_count:{market}:{YYYYMMDD}` | STRING | 일별 AI 호출 수 (signal_generator와 별도) |
| `eval:runner:lock` | STRING (TTL) | 중복 실행 방지 락 |

---

## 상태 확인 (Redis)

```bash
# 최신 판단 확인
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:eval:last:KR:005930
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:eval:last:US:AAPL

# 오늘 통계
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:eval_stats:KR:$(date +%Y%m%d)
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:eval_stats:US:$(date +%Y%m%d)

# 오늘 호출 수
docker exec claw-redis redis-cli -a henry0308 GET ai:eval_call_count:KR:$(date +%Y%m%d)
docker exec claw-redis redis-cli -a henry0308 GET ai:eval_call_count:US:$(date +%Y%m%d)

# 락 상태 (정상: 1~300)
docker exec claw-redis redis-cli -a henry0308 TTL eval:runner:lock

# 최근 로그 3개
docker exec claw-redis redis-cli -a henry0308 LRANGE ai:eval_log:KR:$(date +%Y%m%d) 0 2
```

---

## ai:eval:last 필드 구조

```json
{
  "ts_ms": "1234567890000",
  "market": "KR",
  "symbol": "005930",
  "direction": "LONG",
  "emit": "1",
  "reason": "Positive 5-min momentum with narrow range",
  "features_json": "{\"current_price\": \"75000\", \"ret_1m\": \"0.0013\", ...}",
  "model": "claude-haiku-4-5-20251001"
}
```

---

## signal_generator_runner와의 관계

| 항목 | signal_generator_runner | ai_eval_runner |
|------|------------------------|----------------|
| pause 의존 | pause=true → AI 호출 0 | pause 무관하게 실행 |
| signal queue | push | 절대 push 안 함 |
| Executor 연결 | 있음 (pause=false 시) | 없음 |
| call_count 키 | `ai:call_count:*` | `ai:eval_call_count:*` (별도) |
| 락 키 | `gen:runner:lock` | `eval:runner:lock` (별도) |

---

## 다음 단계 (Phase 9 로드맵)

1. **안정화** (1~2거래일): eval 로그 쌓이면서 feature 0.0 / cold_start / error 패턴 확인
2. **watchlist 확장**: KR 5~10종목 추가 (안정화 확인 후)
3. **통계 리포트**: emit율/방향 분포/feature 상관관계 분석 (2단계)
4. **Qwen 듀얼런**: Claude 단일 안정화 + 저장 포맷 확정 후

---

**작성일**: 2026-03-03
**대상 Phase**: 9 (AI-First / No-Trade)
