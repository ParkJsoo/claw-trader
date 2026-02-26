# Portfolio / Position Engine Spec v1

## 목표
- 보유 포지션 추적 (Redis 기반)
- Fill / Execution 기반 상태 전이
- 평균 매입가 계산
- Realized / Unrealized PnL
- KR / US 공통 구조
- 기존 Executor / Watcher와 호환

---

## Redis Key Schema

| Key | Type | TTL | 설명 |
|-----|------|-----|------|
| `position:{market}:{symbol}` | Hash | 7d | 포지션 상태 |
| `pnl:{market}` | Hash | - | 시장별 PnL 집계 |
| `trade:{market}:{trade_id}` | Hash | 30d | 거래 이력 |
| `claw:fill:queue` | List | - | Fill 이벤트 큐 (LPUSH/RPOP) |
| `claw:order_meta:{market}:{order_id}` | Hash | 24h | 주문 메타 (기존 확장) |
| `position_index:{market}` | Set | 7d | 해당 시장 포지션 심볼 인덱스 |

### position:{market}:{symbol} 필드
| Field | 설명 |
|-------|------|
| qty | 보유 수량 (Decimal string, +: LONG, 0: 청산) |
| avg_price | 평균 매입가 |
| realized_pnl | 해당 포지션에서 실현된 PnL (누적) |
| updated_ts | 마지막 갱신 시각 |
| currency | KRW / USD |

### pnl:{market} 필드
| Field | 설명 |
|-------|------|
| realized_pnl | 시장 전체 실현 손익 누적 |
| unrealized_pnl | 미실현 손익 (선택, 가격 피드 있을 때) |
| currency | KRW / USD |
| updated_ts | 마지막 갱신 시각 |

### trade:{market}:{trade_id} 필드
| Field | 설명 |
|-------|------|
| order_id | 원본 주문 ID |
| symbol | 종목 |
| side | BUY / SELL |
| qty | 체결 수량 |
| price | 체결가 |
| realized_pnl | 이 거래로 인한 실현 손익 (fee 차감 후) |
| ts | 체결 시각 (fill.ts, ms) |
| recorded_at_ms | 기록 시각 |
| exec_id | 브로커 실행 ID (멱등/감사) |
| fee | 수수료 |
| signal_id | 시그널 ID (있을 경우) |

---

## Fill 이벤트 흐름

```
Executor (place_order)
    → order:{market}:{order_id} = status
    → claw:order_meta:{market}:{order_id} = {symbol, side, qty, limit_price, signal_id}

OrderWatcher (status → FILLED)
    → Fill 조회 (브로커 API 또는 order_meta 폴백)
    → LPUSH claw:fill:queue {fill_json}

Portfolio Engine (Fill Consumer)
    → RPOP claw:fill:queue
    → position 업데이트 (평균가, 수량)
    → realized PnL 계산
    → trade:{market}:{id} 기록
    → pnl:{market} 갱신
```

---

## 포지션 전이 로직

### BUY Fill
- qty > 0: 신규/추가 매수
  - `new_qty = prev_qty + fill_qty`
  - `new_avg = (prev_cost + fill_qty * fill_price) / new_qty`
- realized_pnl 변화 없음 (매수는 미실현)

### SELL Fill
- qty > 0인 LONG 포지션 감소
  - `sell_qty = min(fill_qty, prev_qty)` (청산 초과 방지)
  - `realized += (fill_price - avg_price) * sell_qty`
  - `new_qty = prev_qty - sell_qty`
  - new_qty == 0이면 position 키 삭제, index에서 제거

### Cash-only 제약
- SELL 시 prev_qty < fill_qty면 `sell_qty = prev_qty` (과도 매도 방지)

---

## 호환성

- **Executor**: 주문 체결 시 order_meta 저장 (symbol, side, qty, limit_price, signal_id)
- **OrderWatcher**: FILLED 감지 시 order_meta 조회 → Fill 이벤트 생성 → claw:fill:queue에 push
- **기존 order:{market}:{order_id}**: 변경 없음
- **기존 claw:idempo**: 변경 없음

---

## 실행 모델

1. **독립 프로세스**: `position_engine.py` — Fill 큐 폴링, 포지션 갱신
2. **Watcher 내장** (v1 선택): OrderWatcher가 FILLED 시 Fill push + 동기 포지션 갱신

v1은 Watcher 내장으로 시작 (프로세스 수 최소화, 디버깅 용이).
