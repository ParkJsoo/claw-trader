# AI Dual-Run (Phase 9.5)

Claude vs Qwen 듀얼런 비교 엔진. 주문 없음 — 판단 비교 및 합의 검증 전용.

---

## 아키텍처

```
market_data_runner (가격 수집)
        ↓
ai_dual_eval_runner
  ├── ClaudeProvider  → ai:dual:last:claude:{market}:{symbol}
  ├── QwenProvider    → ai:dual:last:qwen:{market}:{symbol}
  └── consensus       → ai:dual:last:consensus:{market}:{symbol}
                                    ↓
                        (주문 없음 — Phase 10에서만 연결)
```

---

## 합의 정책

| Claude | Qwen | 방향 일치 | consensus |
|--------|------|-----------|-----------|
| emit=true | emit=true | O | EMIT |
| emit=true | emit=true | X | HOLD |
| emit=true | emit=false | - | HOLD |
| emit=false | emit=true | - | HOLD |
| emit=false | emit=false | - | SKIP |

Phase 9.5에서는 consensus=EMIT이어도 주문 생성 없음.

---

## Redis 키

### 개별 판단
```
ai:dual:last:{provider}:{market}:{symbol}    # 최신 판단 (hash)
ai:dual_log:{provider}:{market}:{YYYYMMDD}   # 일별 로그 (list)
ai:dual_stats:{provider}:{market}:{YYYYMMDD} # 통계 (hash)
ai:dual_call_count:{market}:{YYYYMMDD}       # 라운드 캡 카운터
```
provider: `claude` | `qwen` | `consensus`

### 비교 통계
```
ai:dual_compare:{market}:{YYYYMMDD}
  fields: both_emit_same_dir / both_emit_diff_dir
          claude_only_emit / qwen_only_emit
          both_no_emit / match_count / mismatch_count
```

---

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `AI_MODEL` | `claude-haiku-4-5-20251001` | Claude 모델 |
| `QWEN_MODEL` | `qwen2.5:7b` | Qwen 모델 (Ollama) |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama 서버 URL |
| `QWEN_TIMEOUT` | `30` | Qwen API 타임아웃(초) |
| `QWEN_TEMPERATURE` | `0.1` | Qwen temperature |
| `DUAL_POLL_SEC` | `120` | 폴링 간격(초) |
| `DUAL_DAILY_CALL_CAP` | `2000` | 시장별 라운드 일일 캡 |
| `DUAL_STATUS_LOG_SEC` | `600` | 상태 로그 간격(초) |
| `GEN_MIN_HIST` | `20` | cold start 최소 히스토리 |

---

## 기동 방법

Ollama + Qwen 먼저 실행:
```bash
ollama pull qwen2.5:7b
ollama serve
```

dual runner 기동:
```bash
cd /Users/henry_oc/develop/claw-trader/src
PYTHONPATH=src ../venv/bin/python -m app.ai_dual_eval_runner
```

---

## 관찰 지표 (Redis)

```bash
# 비교 통계
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" HGETALL ai:dual_compare:KR:$(date +%Y%m%d)

# consensus 통계
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" HGETALL ai:dual_stats:consensus:KR:$(date +%Y%m%d)

# 개별 최신 판단
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" HGETALL ai:dual:last:claude:KR:005930
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" HGETALL ai:dual:last:qwen:KR:005930
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" HGETALL ai:dual:last:consensus:KR:005930

# 호출 횟수
docker exec claw-redis redis-cli -a "$REDIS_PASSWORD" GET ai:dual_call_count:KR:$(date +%Y%m%d)
```

---

## Phase 9.5 완료 조건

- [ ] match_count / (match_count + mismatch_count) >= 50% (1거래일 관찰)
- [ ] claude emit_rate 10~30% (장중)
- [ ] qwen emit_rate 과민 아님 (> 70% 이면 재검토)
- [ ] error_rate < 5% (양쪽 합산)

완료 시 → Phase 10 (합의 정책 기반 KR micro trading 연결)

---

---

## Phase 10 설계 (미래)

### 아키텍처
```
ai_dual_eval_runner (평가/저장 전용)
        ↓
ai:dual:last:consensus:{market}:{symbol}
        ↓
consensus_signal_runner (신규 — Phase 10에서만 기동)
        ↓
claw:signal:queue push
        ↓
기존 Risk Engine / Executor / Order Watcher
```

### consensus_signal_runner 처리 순서
1. `ai:dual:last:consensus:{market}:{symbol}` 읽기
2. consensus != EMIT → skip
3. ts_ms 읽기 → `gen:dual_dedup:{market}:{symbol}` 비교 (이미 처리한 ts_ms면 skip)
4. `gen:dual_cooldown:{market}:{symbol}` 확인
5. confidence threshold 확인 (Claude emit 중 confidence >= 0.6)
6. claw:signal:queue push
7. dedup key 갱신 + cooldown 설정

### dedup Redis 키
```
gen:dual_dedup:{market}:{symbol}   = last_pushed_ts_ms  # TTL 7d
gen:dual_cooldown:{market}:{symbol} = 1                  # TTL 300s
```
- 평가 레이어 키(ai:dual:last:consensus) 수정하지 않음

### signal 포맷 (기존 포맷 호환)
```json
{
  "signal_id": "...",
  "ts": "...",
  "ts_ms": "...",
  "market": "KR",
  "symbol": "005930",
  "direction": "LONG",
  "entry": {"price": "...", "size_cash": "..."},
  "stop": {"price": "..."}
}
```
- size_cash = 현재가 × 1주 (KR) / $50 (US)
- stop.price = entry.price × 0.98 (stop_loss 2%)

### Qwen fallback
- 운영 모드 플래그: `EXECUTION_MODE=dual` (기본) / `EXECUTION_MODE=claude_only`
- match_rate < 50% + Qwen 과민 → claude_only로 전환

---

**작성일:** 2026-03-07
**대상 Phase:** 9.5 — Claude vs Qwen 듀얼런 (Phase 10 설계 포함)
