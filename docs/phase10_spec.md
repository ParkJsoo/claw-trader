# Phase 10 — KR Micro Trading 운영 스펙

> 작성일: 2026-03-10
> 작성 기반: GPT 협의 + Claude Code 검토 확정
> 상태: ✅ 확정 (구현 전 고정)

---

## 🎯 목표

Phase 9.5 (Claude vs Qwen 듀얼런) 안정화 후,
KR 시장에서 최소 단위 실거래를 통한 파이프라인 검증.

**실거래 시작 조건**: Phase 9.5 clean full-day 확인 완료 후 `claw:pause:global` 해제

---

## 📋 운영 파라미터 (고정값)

| 항목 | 값 |
|------|-----|
| market | KR only |
| mode | micro trading |
| watchlist | 8종목 (`.env GEN_WATCHLIST_KR` 참조) |
| trading session | 09:30 ~ 11:00 KST |
| position_size | 1주 (고정) |
| stop_loss | -2% |
| take_profit | 없음 (momentum trailing) |
| max_positions | **2** |
| max_trades_per_day | **6** |
| symbol cooldown | **10분** |
| entry prefilter | `ret_5m > 0 AND range_5m > 0.004` |
| signal_id | UUID (기존 코드베이스 포맷 유지) |
| entry source | consensus=EMIT only |

---

## 🔄 진행 순서

```
C (Phase 10 spec 확정)
  ↓
A (consensus_signal_runner 설계/구현)
  ↓
dry-run (실주문 없이 파이프라인 검증)
  ↓
장중 signal frequency 확인 (목표: 3~10건/일)
  ↓
실거래 전환
```

---

## 🏗 파이프라인 흐름

```
dual LLM eval (ai_dual_eval_runner)
        ↓
consensus_signal_runner   ← candidate signal 생성기 (얇게 유지)
        ↓
StrategyEngine            ← 전략 채택 여부 (session/cooldown/watchlist)
        ↓
RiskEngine                ← 실행 최종 승인 (position/daily loss/exposure)
        ↓
OrderExecutor             ← 실제 주문 전송
```

---

## 📐 레이어별 책임 분리 (고정)

### consensus_signal_runner (candidate 생성기)
- dual consensus 확인 (`claude_emit == 1 AND qwen_emit == 1`)
- entry prefilter: `ret_5m > 0 AND range_5m > 0.004`
- 필수 필드 무결성 체크
- signal_id (UUID) 생성
- candidate_signal → Redis enqueue
- **범위 초과 금지**: session gating, cooldown, position check, risk gate 제외

### StrategyEngine (전략 채택)
- trading session 시간 검증 (09:30~11:00 KST)
- 종목별 symbol cooldown (10분)
- watchlist membership 확인
- 중복 신호 dedupe

### RiskEngine (실행 승인)
- max_positions: 2
- max_trades_per_day: 6
- daily loss guard
- symbol exposure 확인
- available cash 확인
- stop_loss 정책 적합성 (-2%)

### OrderExecutor (주문 실행)
- 승인된 신호만 주문 전환
- 수량 계산 (1주 고정)
- KIS API 호출

---

## 📦 candidate_signal 스키마

```json
{
  "signal_id": "<UUID>",
  "market": "KR",
  "symbol": "005930",
  "timestamp": "2026-03-11T09:35:00+09:00",
  "ts_ms": "<unix_ms>",
  "consensus": "EMIT",
  "claude_emit": 1,
  "qwen_emit": 1,
  "ret_5m": 0.0031,
  "range_5m": 0.0052,
  "current_price": "75000",
  "source": "consensus_signal_runner",
  "status": "candidate"
}
```

---

## ✅ dry-run 검증 항목

- [ ] candidate_signal 생성 빈도 (목표: 하루 3~10건 최종 실행 신호)
- [ ] StrategyEngine 통과율
- [ ] RiskEngine 통과율
- [ ] 동일 종목 반복 신호 cooldown 정상 동작
- [ ] 09:30~11:00 세션 boundary 정상 처리
- [ ] 로그 가독성 (어느 레이어에서 막혔는지 추적 가능)

---

## 🚧 Phase 10 이후 예정

| 단계 | 내용 |
|------|------|
| Phase 10 안정화 | micro trading 3~7거래일 검증 |
| Phase 11 | watchlist KR 8→10종목 확장 |
| Phase 11+ | US IBKR live 구독 전환 (`reqMarketDataType 4→1`) |
| Phase 12 | 3-LLM consensus (Claude + Qwen + DeepSeek) 검토 |

---

## ⚠️ 변경 금지 항목 (Phase 10 운영 중)

- watchlist 변경 금지
- max_positions/max_trades 상향 금지
- entry prefilter 완화 금지
- pause 해제는 dry-run 검증 완료 후에만
