from dotenv import load_dotenv
load_dotenv()

import json
import os
import redis

import time

from domain.models import Signal
from executor.core import Executor
from executor.risk import RiskConfig, RiskEngine
from strategy.engine import StrategyConfig, StrategyEngine
from exchange.kis.client import KisClient
from exchange.ibkr.client import IbkrClient
from guards.data_guard import DataGuard
from ai.advisor import AIAdvisor

def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not set")
    r = redis.from_url(redis_url)

    kis = KisClient()
    ibkr = IbkrClient()

    risk_cfg = RiskConfig()
    strategy_cfg = StrategyConfig()

    strategy = StrategyEngine(r, strategy_cfg)
    ex_kr = Executor(kis, r, "KR", risk=RiskEngine(r, risk_cfg, kis))
    ex_us = Executor(ibkr, r, "US", risk=RiskEngine(r, risk_cfg, ibkr))

    # Phase 8: DataGuard + AI Advisory (shadow mode)
    data_guard = DataGuard(r)
    advisor = AIAdvisor(r) if os.getenv("ANTHROPIC_API_KEY") else None
    if not advisor:
        print("Runner: AI advisor disabled (ANTHROPIC_API_KEY not set)")

    print("Runner started. Waiting signals...")

    while True:
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
                st = ex_us.execute_signal(signal)
            else:
                print("unknown market:", signal.market)
                continue
            print("executed:", signal.signal_id, signal.market, st.value)
        except Exception as e:
            print("execution error:", signal.signal_id, e)

if __name__ == "__main__":
    main()
