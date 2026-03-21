"""backtest_runner — 백테스트 스케줄러.

장 마감 후 KST 16:10에 자동 실행하거나 수동 실행 가능.

기동:
    PYTHONPATH=src venv/bin/python -m scripts.backtest_runner
    PYTHONPATH=src venv/bin/python -m scripts.backtest_runner --now
"""
from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis

from app.backtester import Backtester, ParamSet
from utils.redis_helpers import load_watchlist, today_kst
from guards.notifier import send_telegram

_KST = ZoneInfo("Asia/Seoul")
_RUN_HOUR = int(os.getenv("BACKTEST_RUN_HOUR", "16"))
_RUN_MINUTE = int(os.getenv("BACKTEST_RUN_MINUTE", "10"))
_POLL_SEC = float(os.getenv("BACKTEST_POLL_SEC", "60"))

# 현재 운영 파라미터 (config에서 읽음)
_CURRENT_STOP = Decimal(os.getenv("EXIT_STOP_LOSS_PCT", "0.015"))
_CURRENT_TAKE = Decimal(os.getenv("EXIT_TAKE_PROFIT_PCT", "0.030"))
_CURRENT_TRAIL = Decimal(os.getenv("EXIT_TRAIL_STOP_PCT", "0.015"))

_KR_WATCHLIST_ENV = "GEN_WATCHLIST_KR"


def run_backtest(r, market: str = "KR") -> None:
    """백테스트 1회 실행 + Redis 저장 + TG 발송."""
    print(f"backtest_runner: starting {market}", flush=True)
    env_key = _KR_WATCHLIST_ENV if market == "KR" else "GEN_WATCHLIST_US"
    watchlist = load_watchlist(r, market, env_key)
    if not watchlist:
        print(f"backtest_runner: no watchlist for {market} — skip", flush=True)
        return

    bt = Backtester(r, market)
    results, summaries = bt.run_sweep(watchlist)

    if not summaries:
        print(f"backtest_runner: insufficient data (symbols={len(watchlist)})", flush=True)
        return

    bt.save_results(summaries)

    current_params = ParamSet(_CURRENT_STOP, _CURRENT_TAKE, _CURRENT_TRAIL)
    report = bt.format_report(summaries, current_params, len(watchlist))
    print(report, flush=True)

    try:
        send_telegram(report)
    except Exception as e:
        print(f"backtest_runner: TG send failed ({e})", flush=True)


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("backtest_runner: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 즉시 실행 모드 (--now 플래그)
    if "--now" in sys.argv:
        run_backtest(r, "KR")
        return

    print(
        f"backtest_runner: started, will run at KST {_RUN_HOUR:02d}:{_RUN_MINUTE:02d}",
        flush=True,
    )

    from datetime import datetime
    while True:
        now = datetime.now(_KST)
        sent_key = f"backtest:sent:KR:{today_kst()}"

        if (
            now.hour == _RUN_HOUR
            and now.minute == _RUN_MINUTE
            and not r.exists(sent_key)
        ):
            try:
                run_backtest(r, "KR")
            except Exception as e:
                print(f"backtest_runner: error ({e})", flush=True)
            r.set(sent_key, "1", ex=3600)

        time.sleep(_POLL_SEC)


if __name__ == "__main__":
    main()
