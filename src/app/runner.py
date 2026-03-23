from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import redis

import time
from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

# 재시작 시 큐에 남은 오래된 신호 skip (기본 5분)
_SIGNAL_MAX_AGE_SEC = int(os.getenv("SIGNAL_MAX_AGE_SEC", "300"))


def _record_funnel(r, market: str, event: str) -> None:
    """Execution funnel 일별 집계 (Phase 11: candidate→strategy→risk→executed)."""
    today = datetime.now(_KST).strftime("%Y%m%d")
    key = f"execution_funnel:{market}:{today}"
    try:
        r.hincrby(key, event, 1)
        r.expire(key, 7 * 86400)
    except Exception as e:
        print(f"funnel_error: {event} {e}", flush=True)

from domain.models import Signal, OrderStatus
from executor.core import Executor
from executor.risk import RiskConfig, RiskEngine
from strategy.engine import StrategyConfig, StrategyEngine
from exchange.kis.client import KisClient
from exchange.ibkr.client import IbkrClient
from guards.data_guard import DataGuard
from ai.advisor import AIAdvisor

_RUNNER_LOCK_KEY = "app:runner:lock"
_RUNNER_LOCK_TTL = 120


def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("runner: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)
    r = redis.from_url(redis_url)

    # 프로세스 락 (중복 실행 방지)
    if not r.set(_RUNNER_LOCK_KEY, "1", nx=True, ex=_RUNNER_LOCK_TTL):
        print("runner: already running (lock exists) - exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_RUNNER_LOCK_KEY)
        print("runner: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    kis = KisClient()
    ibkr = None
    if os.getenv("IBKR_ACCOUNT_ID"):
        try:
            ibkr = IbkrClient()
        except Exception as e:
            print(f"runner: IBKR init failed ({e}) — US market disabled", flush=True)

    # Phase 10 config — env var override (재기동 시 반영)
    from strategy.engine import MarketStrategyConfig
    from executor.risk import MarketRiskConfig
    from decimal import Decimal as _D

    strategy_cfg = StrategyConfig(
        kr=MarketStrategyConfig(
            cooldown_sec=int(os.getenv("STRATEGY_KR_COOLDOWN_SEC", "300")),
            daily_cap=int(os.getenv("STRATEGY_KR_DAILY_CAP", "20")),
        ),
    )
    risk_cfg = RiskConfig(
        kr=MarketRiskConfig(
            max_concurrent_positions=int(os.getenv("RISK_KR_MAX_CONCURRENT", "5")),
            daily_loss_limit=_D(os.getenv("RISK_KR_DAILY_LOSS_LIMIT", "-500000")),
            allocation_cap_pct=_D(os.getenv("RISK_KR_ALLOCATION_CAP_PCT", "0.20")),
        ),
    )

    strategy = StrategyEngine(r, strategy_cfg)
    ex_kr = Executor(kis, r, "KR", risk=RiskEngine(r, risk_cfg, kis))

    # 시작 시 적용값 출력 (장중 설정 오류 방지)
    print(
        f"runner: config "
        f"kr_cooldown={strategy_cfg.kr.cooldown_sec}s "
        f"kr_daily_cap={strategy_cfg.kr.daily_cap} "
        f"kr_max_concurrent={risk_cfg.kr.max_concurrent_positions} "
        f"kr_daily_loss_limit={risk_cfg.kr.daily_loss_limit}",
        flush=True,
    )
    ex_us = Executor(ibkr, r, "US", risk=RiskEngine(r, risk_cfg, ibkr)) if ibkr else None

    # Phase 8: DataGuard + AI Advisory (shadow mode)
    data_guard = DataGuard(r)
    advisor = AIAdvisor(r) if os.getenv("ANTHROPIC_API_KEY") else None
    if not advisor:
        print("Runner: AI advisor disabled (ANTHROPIC_API_KEY not set)")

    print("Runner started. Waiting signals...", flush=True)

    try:
        while True:
            r.expire(_RUNNER_LOCK_KEY, _RUNNER_LOCK_TTL)

            # pause 시 불필요한 처리 스킵 (RiskEngine에서도 체크되나, 여기서 조기 차단)
            pause_val = r.get("claw:pause:global")
            if pause_val and (pause_val.decode() if isinstance(pause_val, bytes) else pause_val).lower() in ("true", "1"):
                time.sleep(5)
                continue

            item = r.brpop("claw:signal:queue", timeout=5)
            if not item:
                continue

            _, raw = item
            try:
                data = json.loads(raw)
                signal = Signal.model_validate(data)
            except Exception as e:
                print("invalid signal:", e, raw)
                continue

            # 오래된 신호 skip: 재시작 시 큐에 쌓인 신호 재처리 방지
            try:
                sig_age = (datetime.now(_KST) - datetime.fromisoformat(signal.ts)).total_seconds()
                if sig_age > _SIGNAL_MAX_AGE_SEC:
                    print(f"stale_signal: {signal.signal_id} symbol={signal.symbol} age={sig_age:.0f}s — skip", flush=True)
                    continue
            except Exception:
                pass

            try:
                # Phase 8: DataGuard — stale market data 감지 (v1: warn only)
                guard = data_guard.check(signal.market)
                if not guard.allow:
                    print("data_guard_block:", signal.signal_id, guard.reason, guard.meta)
                    continue
                if guard.severity == "WARN":
                    print("data_guard_warn:", signal.market, guard.reason, guard.meta)

                # Phase 6: 신호 품질 필터 (쿨다운/중복/일일캡)
                s_decision = strategy.check(signal)
                if not s_decision.allow:
                    rk = f"claw:reject:{signal.market}:{signal.signal_id}"
                    mapping = {k: str(v) for k, v in s_decision.meta.items()}
                    mapping.update({
                        "reason": s_decision.reason,
                        "source": "strategy",
                        "market": signal.market,
                        "symbol": signal.symbol,
                        "ts_ms": str(int(time.time() * 1000)),
                    })
                    r.hset(rk, mapping=mapping)
                    r.expire(rk, 86400)
                    _record_funnel(r, signal.market, f"strategy_reject:{s_decision.reason}")
                    print("strategy_reject:", signal.signal_id, s_decision.reason)
                    continue

                # Phase 8: AI Advisory (shadow mode — 실패해도 파이프라인 영향 없음)
                if advisor:
                    try:
                        adv = advisor.advise(signal, s_decision.reason)
                        print("ai_advisory:", signal.signal_id, adv.recommend, adv.confidence, adv.reason)
                    except Exception as e:
                        print("advisor_error:", signal.signal_id, e)

                if signal.market == "KR":
                    st = ex_kr.execute_signal(signal)
                elif signal.market == "US":
                    if ex_us is None:
                        print("runner: US executor not configured (IBKR_ACCOUNT_ID not set)")
                        continue
                    st = ex_us.execute_signal(signal)
                else:
                    print("unknown market:", signal.market)
                    continue
                if st == OrderStatus.RISK_REJECTED:
                    _record_funnel(r, signal.market, "risk_reject")
                elif st == OrderStatus.REJECTED:
                    _record_funnel(r, signal.market, "broker_reject")
                elif st == OrderStatus.ERROR:
                    _record_funnel(r, signal.market, "execution_error")
                else:
                    _record_funnel(r, signal.market, "executed")
                print("executed:", signal.signal_id, signal.market, st.value)
            except Exception as e:
                print("execution error:", signal.signal_id, e)
    finally:
        r.delete(_RUNNER_LOCK_KEY)
        print("runner: lock released", flush=True)

if __name__ == "__main__":
    main()
