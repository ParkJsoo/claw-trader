# COIN Strategy Split and Measurement Plan

Date: 2026-04-17
Scope: COIN only
Status: Ready for implementation

## Objective

Turn COIN from a mixed, hard-to-measure runtime into an explicit two-track system:

1. Type B as the primary production strategy
2. Type A as a secondary canary or shadow strategy

The current COIN setup does not need a brand-new idea first. It needs source-of-truth cleanup, metric cleanup, and strategy separation.

## Current State

### Live performance snapshot from trade history

Recent closed-trade stats computed directly from live trade indexes:

- 2026-04-09: 2 trades, net `-331.47 KRW`
- 2026-04-10: 1 trade, net `+1547.28 KRW`
- 2026-04-12: 2 trades, net `+586.62 KRW`
- 2026-04-13: 1 trade, net `-41.66 KRW`
- 2026-04-14: 1 trade, net `+1333.94 KRW`
- 2026-04-15: 1 trade, net `-1102.42 KRW`
- 2026-04-16: 1 trade, net `-936.63 KRW`
- 2026-04-17: 0 closed trades

Recent aggregate:

- 9 trades
- 3 wins / 6 losses
- win rate `33.3%`
- net `+1055.67 KRW`
- profit factor `1.36`

### Measurement problems

- `pnl:COIN.realized_pnl = +3903.14 KRW`
- `perf:daily:COIN:*` keys are not being saved at all
- `daily_report_runner` resets COIN daily state at midnight but does not generate COIN daily reports

Interpretation:

- COIN is tradable, but current performance storage is not trustworthy enough for tuning.
- COIN daily performance must be computed from trade indexes until reporting is fixed.

### Live pipeline snapshot on 2026-04-17

- `execution_funnel:COIN:20260417 = executed 21`
- `ai:dual_call_count:COIN:20260417 = 1363`
- `ai:dual_stats:claude:COIN:20260417 = emit 5 / no_emit 1358`
- `consensus:stats:COIN:20260417`
  - `reject_low_vol_24h = 2530`
  - `reject_bad_news = 942`
  - `reject_symbol_daily_cap = 140`
  - `reject_daily_stop = 89`

Interpretation:

- Execution is active.
- Claude-based Type A is almost inactive.
- Type B is driving real production behavior.

### Type A vs Type B reality

- On 2026-04-17:
  - `type_b.pass.signal_created = 21`
  - `runner COIN SUBMITTED = 21`
  - Claude emit count is only 5
- This means production COIN behavior is dominated by Type B, not Type A.

## Diagnosis

### What to keep

- Keep the two-strategy concept for COIN.
- Keep Type B trend-riding logic.
- Keep cooldown, daily stop, symbol cap, and liquidity guards.
- Keep real-time COIN-specific exit handling.

### What is wrong

1. Type A and Type B are mixed operationally and analytically.
2. Performance accounting is inconsistent.
3. Exit logic is split across two active components.
4. Runtime take-profit settings are drifting from documented defaults.
5. Type A is not earning its current production footprint.

## Strategy Decision

Do not invent a third COIN strategy now.

Instead:

1. Promote Type B to the clearly labeled production strategy.
2. Demote Type A to canary or shadow mode until it proves value.
3. Fix measurement before doing deeper COIN tuning.

## Recommended Runtime Profile

### Production profile: Type B

Make Type B the default production COIN strategy and standardize its exit profile:

- `COIN_EXIT_STOP_LOSS_PCT=0.030`
- `COIN_EXIT_TAKE_PROFIT_PCT=0.150`
- `COIN_EXIT_TRAIL_STOP_PCT=0.040`
- `COIN_EXIT_TRAIL_STOP_TIGHT_PCT=0.030`
- `COIN_EXIT_TRAIL_TIGHT_TRIGGER=0.050`
- `COIN_EXIT_TIME_LIMIT_SEC=3600`
- `COIN_EXIT_TIME_LIMIT_MAX_SEC=14400`
- `COIN_EARLY_EXIT_SEC=600`
- `COIN_EARLY_EXIT_PCT=0.010`

Why this profile:

- It matches the declared "big mover ride" intent better than the current live `take_pct=0.300`.
- It is closer to the documented strategy than the present runtime drift.
- It preserves upside via trailing logic instead of requiring a fixed +30% target.

### Canary profile: Type A

Reduce Type A from production expectation to controlled validation:

- no production PnL target
- separate signal counters
- separate executed-trade counters
- separate closed-trade and PnL tracking

Type A should either:

- prove incremental value over Type B, or
- be disabled from production

## Source-of-Truth Cleanup

### One exit owner

Choose one primary COIN exit engine.

Recommended:

- primary: `upbit_ws_exit_monitor`
- fallback only: `position_exit_runner` for recovery scenarios, not normal production COIN exits

Reason:

- COIN is 24/7 and highly path-dependent.
- The websocket exit monitor is the natural real-time owner.
- Split ownership makes fills, PnL, and debugging inconsistent.

### One runtime config truth

Use `supervisord.conf` as the only runtime truth.

Then align:

1. env files
2. docs
3. monitoring assumptions
4. Telegram/reporting language

## Measurement Fixes Required Before More Tuning

1. Save `perf:daily:COIN:*` daily keys.
2. Make `pnl:COIN` and trade-derived daily PnL reconcilable.
3. Tag trades and signals by strategy source:
   - `type_a`
   - `type_b`
4. Split dashboards and reports by strategy source.

Until this is done, COIN tuning should be limited to obvious runtime drift fixes, not fine-grained alpha tuning.

## Rollout Order

1. Fix COIN daily reporting and PnL reconciliation.
2. Assign a single primary COIN exit owner.
3. Standardize live COIN runtime config to the production profile above.
4. Tag Type A and Type B separately across signal, execution, and closed-trade layers.
5. Keep Type B in production.
6. Move Type A to canary or shadow.

## Success Criteria

Measurement:

- `perf:daily:COIN:*` exists and matches trade-derived stats.
- `pnl:COIN` reconciles with closed trades.
- Type A and Type B can be evaluated independently.

Strategy:

- Type B remains profitable or stable on a 30-trade sample.
- Runtime take-profit drift is removed.
- Duplicate fill / sell-without-position issues stop appearing in normal operation.

## Stop Conditions

Pause COIN strategy tuning if any of the following persists:

- `perf:daily:COIN:*` remains absent
- `pnl:COIN` and trade-derived results diverge materially
- exit ownership remains split in production

If Type B loses edge after measurement cleanup, then strategy redesign becomes justified. Not before.

## Immediate Next Actions

1. Treat Type B as the production strategy in docs and ops.
2. Fix COIN performance reporting and reconciliation.
3. Set live COIN take profit back to `0.150` unless new measured evidence supports another value.
4. Make `upbit_ws_exit_monitor` the primary COIN exit owner.
5. Evaluate Type A only after strategy-source-separated metrics exist.
