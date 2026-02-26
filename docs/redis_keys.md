# Redis Key Design (v2)

## Control / Security
- claw:ratelimit:{user_id}
- claw:pin_attempts:{user_id}
- claw:pause:global

## Orders
- order:{market}:{order_id} — 주문 상태 (SUBMITTED, FILLED, CANCELED, REJECTED)
- claw:order_meta:{market}:{order_id} — 주문 메타 (first_seen_ts, symbol, side, qty, limit_price, signal_id)

## Reject / Idempotency
- claw:reject:{market}:{id}
- claw:idempo:{market}:{signal_id}

## Portfolio / Position Engine (PHASE 4)
- position:{market}:{symbol} — 포지션 Hash (qty, avg_price, realized_pnl, updated_ts ms, currency)
- position_index:{market} — Set of symbols with open positions
- mark:{market}:{symbol} — 임시 마크가 (STRING, unrealized 계산용)
- pnl:{market} — 시장별 PnL Hash (realized_pnl, unrealized_pnl, currency, updated_ts ms)
- trade:{market}:{trade_id} — 거래 이력 Hash
- trade_dedupe:{market}:{trade_id} — 멱등용 (SET NX)
- trade_index:{market}:{symbol} — ZSET (score=ts_ms, member=trade_id)
- claw:fill:queue — Fill 이벤트 큐 (LPUSH/RPOP)
- claw:fill:dlq — 실패 Fill DLQ

## Risk Engine (PHASE 5)
- claw:pause:reason — 마지막 pause 트리거 reason 문자열 (SET NX, 첫 발동만 기록)
- claw:pause:meta — 마지막 pause 트리거 meta (SET NX, 첫 발동만 기록, 레거시: hset 방식도 허용)
- claw:killswitch:{market} — 킬스위치 발동 상세 JSON (reason, meta, ts) — SET NX EX 86400

## Market Data (PHASE 7)
- mark:{market}:{symbol} — 현재가 STRING (portfolio 기존 키 재사용, MarketDataUpdater가 갱신)
- md:error:{market}:{YYYYMMDD} — 가격 갱신 에러 카운터 HASH (reason → count, TTL 7d, KST 기준)
- md:last_update:{market} — 마지막 성공 갱신 ts_ms STRING (모니터링/AI Layer 헬스 게이트용)

## AI Advisory (PHASE 8)
- ai:advice:{market}:{signal_id} — AI 추천 HASH (ts_ms, recommend, confidence, reason, symbol, direction, strategy_reason) — TTL 30d
- ai:advice_index:{market}:{YYYYMMDD} — ZSET (score=ts_ms, member=signal_id, TTL 30d)
- ai:advice_stats:{market}:{YYYYMMDD} — AI 추천 통계 HASH (recommend별 카운터, TTL 30d)

## Market Data History (PHASE 8 v2)
- mark_hist:{market}:{symbol} — 최근 mark 가격 히스토리 LIST ("ts_ms:price", 최대 300개, TTL 2d)

## AI Signal Generator (PHASE 8 v2)
- gen:cooldown:{market}:{symbol} — 생성기 심볼별 쿨다운 (SET NX TTL GEN_COOLDOWN_SEC, 기본 300s)
- gen:daily_emit:{market}:{YYYYMMDD} — 생성기 일일 발행 카운터 (INCR, TTL 3d)
- ai:gen:{market}:{signal_id} — 생성기 감사 로그 HASH (ts_ms, symbol, direction, size_cash, emit, emit_blocked, block_reason, features_json, raw_response, reason, model, provider) — TTL 7d
- ai:gen_index:{market}:{YYYYMMDD} — 생성기 발행 인덱스 ZSET (score=ts_ms, member=signal_id, TTL 7d)
- ai:gen_stats:{market}:{YYYYMMDD} — 생성기 통계 HASH (generated/no_emit/skip_cold_start/skip_cooldown/skip_daily_cap/error_* 카운터, TTL 7d)

## Strategy Engine (PHASE 6)
- strategy:dedupe:{market}:{signal_id} — 신호 중복 처리 방지 (SET NX, TTL 7d)
- strategy:cooldown:{market}:{symbol} — 종목별 마지막 통과 ts_ms (SET EX cooldown_sec)
- strategy:daily_count:{market}:{YYYYMMDD} — 시장별 일일 처리 신호 수 (INCR, TTL 3d)
- strategy:pass_count:{market}:{YYYYMMDD} — 일별 통과 신호 수 (INCR, TTL 7d)
- strategy:reject_count:{market}:{YYYYMMDD} — 일별 거부 신호 수 by reason (HASH HINCRBY, TTL 7d)
