# 🚀 Live Transition Checklist

> 3/3 IBKR 입금 후 실전 전환 절차. 순서대로 진행할 것.

---

## 📋 STEP 1 — IBKR 입금 확인

```bash
# 1. IBKR 포털 확인 (우선)
# https://portal.ibkr.com → Account Management → Cash

# 2. API로 확인 (프로세스 실행 중일 때)
cd /Users/henry_oc/develop/claw-trader/src
python -m scripts.ibkr_healthcheck
```

확인 항목:
- [ ] Cash balance > 0
- [ ] available_cash 정상 수신 (ACCOUNT_SNAPSHOT_ERROR 없음)

---

## 📋 STEP 2 — IBKR 라이브 데이터 전환

### 2-1. IBKR 포털에서 Market Data 구독 확인
- [ ] IBKR 포털 → Market Data Subscriptions
- [ ] US 주식 실시간 데이터 활성화 확인

### 2-2. reqMarketDataType 변경

파일: `src/market_data/ibkr_feed.py`

```python
# 변경 전 (Delayed Frozen)
self.ib.reqMarketDataType(4)

# 변경 후 (Live)
self.ib.reqMarketDataType(1)
# 또는 해당 줄 제거 (기본값 1)
```

### 2-3. 롤백 방법 (라이브 데이터 오류 시)

라이브 전환 후 IBKR 가격 수신 불가 / 오류 급증 시 즉시 되돌리기:

```python
# ibkr_feed.py — 원래대로 복구
self.ib.reqMarketDataType(4)  # Delayed Frozen 재적용
```

롤백 조건:
- US MD stale (md:last_update:US > 60s)
- IBKR 연결 오류 반복
- Telegram AUTO-PAUSE 알림 수신

롤백 후: 프로세스 재기동 → MD 신선도 확인 → 원인 파악 후 재시도

---

## 📋 STEP 3 — 프로세스 재기동

```bash
# 기존 프로세스 종료 후 순서대로 재기동
cd /Users/henry_oc/develop/claw-trader/src
python -m app.runner
python -m app.market_data_runner
python -m scripts.order_watcher
python -m app.signal_generator_runner
```

MD 신선도 확인:
```bash
docker exec claw-redis redis-cli -a henry0308 GET md:last_update:US
# → 현재 시각 기준 10초 이내여야 함
```

---

## 📋 STEP 4 — pause 해제

```bash
docker exec claw-redis redis-cli -a henry0308 SET claw:pause:global false
docker exec claw-redis redis-cli -a henry0308 GET claw:pause:global
# → "false" 확인
```

---

## 📋 STEP 5 — KR 소액 실전 테스트 (첫 실전)

### 시나리오

| 항목 | 값 |
|------|----|
| 종목 | 삼성전자 (005930) |
| 방향 | LONG |
| 주문 방식 | 시장가 |
| 수량 | 1주 |
| 최대 보유 시간 | 5분 |
| 청산 | 수동 EXIT 신호 push |

### 실행 방법

```bash
# 신호 수동 push (KR LONG)
cd /Users/henry_oc/develop/claw-trader/src
python -m scripts.push_signal_kr

# 5분 후 EXIT
# push_signal_kr에서 direction=EXIT로 재실행
```

### 검증 체크리스트

- [ ] 주문 생성 정상 (KIS order_id 반환)
- [ ] order_watcher가 체결 감지
- [ ] position_index:KR에 005930 추가됨
- [ ] position:KR:005930 qty > 0
- [ ] mark:{KR}:{005930} 가격 갱신 중
- [ ] pnl:KR unrealized 값 존재
- [ ] EXIT 후 qty = 0, realized PnL 기록

```bash
# 포지션 확인
docker exec claw-redis redis-cli -a henry0308 HGETALL position:KR:005930
docker exec claw-redis redis-cli -a henry0308 HGETALL pnl:KR

# 포지션 목록
docker exec claw-redis redis-cli -a henry0308 SMEMBERS position_index:KR
```

---

## 📋 STEP 6 — US 소액 실전 테스트

### 시나리오

| 항목 | 값 |
|------|----|
| 종목 | AAPL |
| 방향 | LONG |
| 주문 방식 | 지정가 (현재가 기준) |
| 수량 | 1주 |
| 최대 보유 시간 | 5분 |
| 청산 | 수동 EXIT 신호 push |

### 실행 방법

```bash
# 신호 수동 push (US LONG)
cd /Users/henry_oc/develop/claw-trader/src
python -m scripts.push_signal
```

### 검증 체크리스트

- [ ] IBKR 주문 정상 접수
- [ ] order_watcher 체결 감지
- [ ] position:US:AAPL qty > 0
- [ ] unrealized PnL 실시간 갱신
- [ ] EXIT 후 realized PnL 기록

---

## 📋 STEP 7 — 실전 후 상태 점검

```bash
# 전체 Redis 상태
docker exec claw-redis redis-cli -a henry0308 GET claw:pause:global
docker exec claw-redis redis-cli -a henry0308 GET md:last_update:KR
docker exec claw-redis redis-cli -a henry0308 GET md:last_update:US
docker exec claw-redis redis-cli -a henry0308 TTL gen:runner:lock

# AI 통계 (pause=false 후 신호 생성 시작됨)
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:gen_stats:KR:$(date +%Y%m%d)
docker exec claw-redis redis-cli -a henry0308 HGETALL ai:gen_stats:US:$(date +%Y%m%d)
docker exec claw-redis redis-cli -a henry0308 GET ai:call_count:KR:$(date +%Y%m%d)
docker exec claw-redis redis-cli -a henry0308 GET ai:call_count:US:$(date +%Y%m%d)
```

---

## ⚠️ 리스크 한도 설정 (확정값)

| 항목 | KR | US |
|------|----|----|
| 1회 최대 주문 금액 | 10,000원 (GEN_MAX_SIZE_CASH_KR) | $10 (GEN_MAX_SIZE_CASH_US) |
| 킬스위치 임계값 | -500,000원 realized PnL | -$500 realized PnL |
| 일일 최대 발행 수 | 5회 (GEN_DAILY_EMIT_CAP) | 5회 |
| AI 호출 일일 캡 | 1,000회 (GEN_DAILY_CALL_CAP) | 1,000회 |

> 초기 운영 안정화 전까지 위 값 유지. 전략 변경 이벤트로 간주하므로 무인 운영 중 변경 금지.

---

## 🔥 실패 시 의심 순서

1. **RiskEngine** — pause 상태? killswitch 발동? allocation_cap 초과?
2. **Executor** — API 권한? 잔고 부족? 네트워크?
3. **order_watcher** — fill 이벤트 수신? TTL 내 체결?
4. **Redis 상태** — position_index 일관성? pnl 키 존재?

```bash
# reject 로그 확인
docker exec claw-redis redis-cli -a henry0308 LRANGE claw:reject:log 0 9
```

---

## ✅ 성공 기준 (첫 실전)

1. 주문 1회 체결 성공 (KIS/IBKR order_id 반환)
2. order_watcher fill 반영 → position_index 업데이트
3. unrealized → realized PnL 갱신 확인

이 3가지 통과 = 실전 파이프라인 검증 완료.

---

**작성일:** 2026-03-02
**대상 Phase:** 8 v4 → Live 전환
