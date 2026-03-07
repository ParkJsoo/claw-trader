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

**작성일:** 2026-03-07
**대상 Phase:** 9.5 — Claude vs Qwen 듀얼런
