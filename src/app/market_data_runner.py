from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
import sys
import time
import traceback

import redis

from market_data.kis_feed import KisFeed
from market_data.ibkr_feed import IbkrFeed
from market_data.updater import MarketDataUpdater
from portfolio.redis_repo import RedisPositionRepository
from utils.redis_helpers import parse_watchlist, load_watchlist

POLL_INTERVAL = int(os.getenv("MD_POLL_INTERVAL", "3"))

_MD_LOCK_KEY = "md:runner:lock"
_MD_LOCK_TTL = max(60, POLL_INTERVAL * 10 + 30)  # poll interval의 10배 + 여유


_parse_watchlist = parse_watchlist


def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("md_runner: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)
    r = redis.from_url(redis_url)

    # 프로세스 락 (중복 실행 방지)
    if not r.set(_MD_LOCK_KEY, "1", nx=True, ex=_MD_LOCK_TTL):
        print("md_runner: already running (lock exists) - exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_MD_LOCK_KEY)
        print("md_runner: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    repo = RedisPositionRepository(r)
    kis_feed = KisFeed()
    ibkr_feed = IbkrFeed()
    updater = MarketDataUpdater(r, repo, kis_feed, ibkr_feed)

    watchlist = {
        "KR": load_watchlist(r, "KR", "GEN_WATCHLIST_KR"),
        "US": load_watchlist(r, "US", "GEN_WATCHLIST_US"),
    }
    print(f"md_runner: started poll_interval={POLL_INTERVAL}s watchlist={watchlist}", flush=True)

    _wl_refresh_counter = 0
    _WL_REFRESH_EVERY = 20  # 매 20 폴링(~60초)마다 워치리스트 갱신

    try:
        while True:
            r.expire(_MD_LOCK_KEY, _MD_LOCK_TTL)

            # 주기적 동적 워치리스트 갱신
            _wl_refresh_counter += 1
            if _wl_refresh_counter >= _WL_REFRESH_EVERY:
                _wl_refresh_counter = 0
                new_wl = {
                    "KR": load_watchlist(r, "KR", "GEN_WATCHLIST_KR"),
                    "US": load_watchlist(r, "US", "GEN_WATCHLIST_US"),
                }
                if new_wl != watchlist:
                    print(f"md_runner: watchlist updated {watchlist} -> {new_wl}", flush=True)
                    watchlist = new_wl

            try:
                updater.run_once(watchlist)
            except Exception:
                traceback.print_exc()
            time.sleep(POLL_INTERVAL)
    finally:
        r.delete(_MD_LOCK_KEY)
        print("md_runner: lock released", flush=True)


if __name__ == "__main__":
    main()
