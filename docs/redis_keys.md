# Redis Key Design (v3)

## Control / Security
- `claw:ratelimit:{user_id}` — TG 봇 rate limit
- `claw:pin_attempts:{user_id}` — PIN 인증 실패 카운터
- `claw:pause:global` — 전역 일시정지 (자정 KST TTL 자동 만료)
- `claw:pause:reason` — 마지막 pause 트리거 reason (SET NX, 첫 발동만)
- `claw:pause:meta` — 마지막 pause 트리거 meta (SET NX)
- `claw:killswitch:{market}` — 킬스위치 발동 상세 JSON (reason, meta, ts) — SET NX EX 86400

## Runtime Config (Phase 18)
- `claw:config:{market}` — 런타임 파라미터 Hash (stop_pct, take_pct, trail_pct, size_cash_pct, max_concurrent). TG `/claw set`으로 변경, position_exit_runner 매 폴링마다 읽기.

## Orders
- `order:{market}:{order_id}` — 주문 상태 (SUBMITTED, FILLED, CANCELED, REJECTED)
- `claw:order_meta:{market}:{order_id}` — 주문 메타 (first_seen_ts, symbol, side, qty, limit_price, signal_id)

## Reject / Idempotency
- `claw:reject:{market}:{id}` — 전략 거부 이유 Hash (reason, source, market, symbol, ts_ms)
- `claw:idempo:{market}:{signal_id}` — 실행 멱등성 잠금 (SET NX)

## Portfolio / Position Engine
- `position:{market}:{symbol}` — 포지션 Hash (qty, avg_price, realized_pnl, updated_ts ms, currency, stop_pct, take_pct)
- `position_index:{market}` — open 포지션 심볼 Set
- `mark:{market}:{symbol}` — 현재가 STRING (MarketDataUpdater 갱신, unrealized 계산용, TTL 7d)
- `pnl:{market}` — 시장별 PnL Hash (realized_pnl, unrealized_pnl, currency, updated_ts ms)
- `trade:{market}:{trade_id}` — 거래 이력 Hash (order_id, symbol, side, qty, price, realized_pnl, ts, recorded_at_ms, exec_id, fee, signal_id, source)
- `trade_dedupe:{market}:{trade_id}` — 멱등용 (SET NX, TTL 30d)
- `trade_index:{market}:{symbol}` — ZSET (score=ts_ms, member=trade_id, TTL 30d)
- `trade_symbols:{market}` — 거래 이력 추적 Set (TTL 90d)
- `claw:fill:queue` — Fill 이벤트 큐 (LPUSH/RPOP)
- `claw:fill:dlq` — 실패 Fill DLQ

## Position Exit Runner (Phase 15)
- `claw:trail_hwm:{market}:{symbol}` — Trailing stop 고점(HWM) STRING. BUY fill 시 avg_price로 초기화, 매 폴링마다 max(prev, mark)로 갱신, SELL fill 시 삭제. TTL 7d.
- `claw:buy_pending:{market}:{symbol}` — BUY 주문 제출 후 잔고 반영 대기 플래그. TTL 120s. SELL race condition 방지.
- `claw:exit_lock:{market}:{symbol}` — SELL 주문 중복 방지 (SET NX TTL 60s)
- `claw:signal_pct:{market}:{symbol}` — per-signal 동적 stop/take_pct Hash (stop_pct, take_pct, range_5m, ts). TTL 24h. consensus_signal_runner가 저장, position_exit_runner가 참조.

## Streak / Capital Adjustment (Phase 18)
- `claw:streak:{market}` — 연속 수익/손실 카운터 STRING (양수=수익, 음수=손실, TTL 7d). Lua 원자적 업데이트.

## Daily Cap / Reset (Phase 18)
- `claw:daily_reset:{market}:{YYYYMMDD}` — 08:55 KST daily cap 리셋 완료 플래그 (TTL=자정까지)

## Hedge Runner (Phase 19)
- `claw:hedge_trigger:{market}` — 헤지 재발동 방지 플래그 (TTL 3600s = 1시간)

## Market Data
- `md:error:{market}:{YYYYMMDD}` — 가격 갱신 에러 카운터 Hash (reason → count, TTL 7d)
- `md:last_update:{market}` — 마지막 성공 갱신 ts_ms STRING
- `mark_hist:{market}:{symbol}` — 최근 mark 가격 히스토리 LIST ("ts_ms:price", 최대 1000개, TTL 7d)
- `vol:KR:{symbol}:{YYYYMMDD}` — 당일 누적 거래량 (acml_vol, TTL 25h)

## AI Dual Eval (Phase 10+)
- `ai:dual:last:claude:{market}:{symbol}` — Claude 최근 평가 Hash (ts_ms, result, confidence, reason)
- `ai:dual:last:qwen:{market}:{symbol}` — Qwen 최근 평가 Hash (ts_ms, result, confidence, reason)

## AI Signal Generator
- `gen:cooldown:{market}:{symbol}` — 생성기 심볼별 쿨다운 (SET NX TTL GEN_COOLDOWN_SEC, 기본 300s)
- `gen:daily_emit:{market}:{YYYYMMDD}` — 생성기 일일 발행 카운터 (INCR, TTL 3d)
- `ai:gen:{market}:{signal_id}` — 생성기 감사 로그 Hash (TTL 7d)
- `ai:gen_index:{market}:{YYYYMMDD}` — 발행 인덱스 ZSET (score=ts_ms, TTL 7d)
- `ai:gen_stats:{market}:{YYYYMMDD}` — 생성기 통계 Hash (TTL 7d)

## AI Advisory
- `ai:advice:{market}:{signal_id}` — AI 추천 Hash (TTL 30d)
- `ai:advice_index:{market}:{YYYYMMDD}` — ZSET (TTL 30d)
- `ai:advice_stats:{market}:{YYYYMMDD}` — AI 추천 통계 Hash (TTL 30d)

## Strategy Engine
- `strategy:dedupe:{market}:{signal_id}` — 신호 중복 처리 방지 (SET NX, TTL 7d)
- `strategy:cooldown:{market}:{symbol}` — 종목별 마지막 통과 ts_ms (SET EX cooldown_sec, 기본 300s)
- `strategy:daily_count:{market}:{YYYYMMDD}` — 시장별 일일 처리 신호 수 (INCR, TTL 3d)
- `strategy:pass_count:{market}:{YYYYMMDD}` — 일별 통과 신호 수 (INCR, TTL 7d)
- `strategy:reject_count:{market}:{YYYYMMDD}` — 일별 거부 신호 수 by reason (Hash HINCRBY, TTL 7d)

## Watchlist / Regime
- `dynamic:watchlist:{market}` — 동적 워치리스트 Set (watchlist_selector_runner 갱신, 장중 1h/장외 6h 주기)
- `watchlist:exclude:{market}` — 운영 제외 심볼 Set. 계좌 권한/브로커 제약으로 거래 불가한 종목을 즉시 차단할 때 사용 (예: `233740`).
- `ret5m:{market}:{symbol}` — 종목별 5분 수익률 (regime filter 계산용)

## Performance / Reporting (Phase 16)
- `perf:daily:{market}:{YYYYMMDD}` — 성과 통계 Hash (win_rate, profit_factor, avg_rr, max_drawdown, total_trades 등)
- `perf:report_sent:{market}:{YYYYMMDD}` — 당일 TG 리포트 발송 완료 플래그 (TTL 20h)

## Backtest (Phase 16+)
- `backtest:result:{market}:{YYYYMMDD}` — 파라미터 스윕 결과 JSON LIST (상위 20개, TTL 90d)
- `backtest:sent:{market}:{YYYYMMDD}` — 당일 백테스트 발송 완료 플래그 (TTL 1h)

## Execution Funnel (Phase 11)
- `execution_funnel:{market}:{YYYYMMDD}` — Hash (candidate/strategy_reject/risk_reject/broker_reject/execution_error/executed 카운터, TTL 7d)
