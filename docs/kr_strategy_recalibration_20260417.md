# KR Strategy Recalibration Plan

Date: 2026-04-17
Scope: KR only
Status: Ready for implementation

## Objective

Re-establish KR as a measurable, tradable strategy by separating:

1. Infrastructure failure
2. Entry-gate over-filtering
3. Exit-parameter mismatch

The current KR stack does not need a brand-new strategy. It needs stable market/execution plumbing, clearer gate semantics, and tighter exit settings that match the actual scalp entry logic.

## Current State

### Live performance snapshot

- Recent realized performance from live trade history:
  - 2026-04-09: 2 trades, net `-933 KRW`
  - 2026-04-13: 4 trades, net `+1213 KRW`
  - 2026-04-14: 5 trades, net `-1792 KRW`
  - 2026-04-15 to 2026-04-17: 0 closed trades
- Recent aggregate: 11 trades, 3 wins, 8 losses, win rate `27.3%`, net `-1512 KRW`

### Live pipeline snapshot on 2026-04-17

- `ai:dual_call_count:KR:20260417 = 170`
- `ai:dual_stats:claude:KR:20260417 = emit 8 / no_emit 161`
- `consensus:stats:KR:20260417`
  - `reject_bad_news = 318`
  - `reject_prefilter_ret_5m = 47`
  - `reject_no_live_price = 5`
- `execution_funnel:KR:20260417 = {}`

Interpretation:

- KR is currently stopping before execution.
- The strategy is not failing at the broker stage today because it rarely reaches the broker stage.
- The label `reject_bad_news` is misleading. In practice it often means "Claude veto / momentum decayed / late entry" rather than literal negative news.

### Infrastructure state

- KIS price and token errors are still recurring in market-data logs.
- KIS cancel failures are still recurring in order-watcher logs.
- KR holdings sync fallback is still recurring in exit logs.
- On 2026-04-17 alone, `sync_error_fallback_cache market=KR` appeared 51 times in `position_exit.log`.

Interpretation:

- KR strategy quality cannot be judged cleanly while KIS pricing, cancel, and holdings sync remain unstable.
- Even after entry gating is improved, execution lifecycle risk remains material.

### Live config drift

- Runtime truth is `config/supervisord.conf`, not the env file or README.
- Live runtime currently uses:
  - `EXECUTION_MODE=claude_only`
  - `MB_MIN_SURGE_5M_KR=0.020`
  - `EXIT_STOP_LOSS_PCT=0.025`
  - `EXIT_TAKE_PROFIT_PCT=0.05`
  - `EXIT_TRAIL_STOP_PCT=0.020`
  - `EXIT_TIME_LIMIT_SEC=900`
- The README and older docs still describe older assumptions such as dual LLM evaluation and different KR runtime behavior.

## Diagnosis

### What to keep

- Keep `claude_only` for KR.
- Keep the current "momentum breakout + AI veto" architecture.
- Keep stale-tick and stale-eval protection.
- Keep cooldown, daily stop, stop-count, and symbol-cap controls.

### What is wrong

1. KR is currently over-filtered before execution.
2. `reject_bad_news` hides the real reason mix and makes tuning difficult.
3. KR exits are too loose for the current entry style.
4. Runtime config, env config, and documentation are no longer aligned.
5. KR infrastructure instability prevents clean evaluation.

## Strategy Decision

Do not replace the KR strategy.

Instead:

1. Treat KR as a recovery and recalibration project.
2. Freeze "new alpha idea" work until KR execution becomes reliable again.
3. Re-tune exits before relaxing entries further.

## Recommended Runtime Profile

### Phase 1: Stabilize and measure

Keep entry logic broadly as-is during the first stabilization window:

- `EXECUTION_MODE=claude_only`
- `MB_MIN_SURGE_5M_KR=0.020`
- `CONSENSUS_POLL_SEC=30`
- `UNIVERSE_SELECT_COUNT=12`

But change KR exit defaults to a tighter scalp profile:

- `EXIT_STOP_LOSS_PCT=0.015`
- `EXIT_TAKE_PROFIT_PCT=0.030`
- `EXIT_TRAIL_STOP_PCT=0.015`
- `EXIT_TIME_LIMIT_SEC=900`
- `KR_TRAIL_ONLY_TRIGGER_PCT=0.020`

Why this profile:

- Current KR entry is already tuned for early short-horizon momentum.
- A `+5%` take profit is too wide relative to the present gate strictness.
- Backtest logs repeatedly favor `1% stop / 2% to 4% take / 1% to 2% trail`.
- `1.5 / 3.0 / 1.5` is a safer first production step than jumping directly to `1 / 2 / 1`.

### Phase 2: Visibility fix

Rename or split the current `reject_bad_news` bucket into separate reasons:

- `reject_claude_veto`
- `reject_late_entry`
- `reject_momentum_decay`
- `reject_market_close`

Do not tune KR entry thresholds further until this visibility exists.

### Phase 3: Controlled entry relaxation, only if needed

Only after KIS stability improves and Phase 1/2 metrics are clean:

- Test whether KR needs a softer entry path:
  - keep `MB_MIN_SURGE_5M_KR=0.020`, or
  - add a narrower alternate path with lower surge threshold but stricter live `ret_1m` confirmation

Do not widen KR watchlists or increase daily caps before this.

## Rollout Order

1. Make `supervisord.conf` the single source of truth.
2. Update docs and env defaults to match runtime.
3. Apply tighter KR exit profile.
4. Add reject-reason visibility split for KR.
5. Observe for at least 20 executed KR trades or 5 trading days.
6. Only then decide whether KR entry gates need relaxation.

## Success Criteria

Infrastructure:

- KIS price/cancel/holdings failures fall to a low, non-spamming level.
- KR can complete order lifecycle without repeated fallback loops.

Strategy:

- KR reaches execution again on normal market days.
- At least 20 executed trades collected after stabilization.
- Win rate improves above the current `27.3%` baseline.
- Profit factor improves above the current `0.60` baseline.
- Average hold time remains consistent with scalp behavior.

## Stop Conditions

Pause KR parameter tuning if any of the following persists:

- repeated KIS price failure bursts
- repeated cancel failure bursts
- repeated holdings-sync fallback loops
- execution funnel stays near zero after the infra layer is stable

If KR still cannot execute after infra recovery and reject-visibility fixes, then entry logic redesign becomes justified.

## Immediate Next Actions

1. Align runtime, env, and docs to one KR truth set.
2. Tighten KR exits to `1.5% / 3.0% / 1.5%`.
3. Split `reject_bad_news` into meaningful KR veto categories.
4. Run KR in observation mode for 20 executed trades.
5. Re-evaluate whether entry gates still need to be loosened.
