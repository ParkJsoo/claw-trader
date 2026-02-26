from dotenv import load_dotenv
load_dotenv()

import os
import time

import redis

from market_data.kis_feed import KisFeed
from market_data.ibkr_feed import IbkrFeed
from market_data.updater import MarketDataUpdater
from portfolio.redis_repo import RedisPositionRepository

POLL_INTERVAL = int(os.getenv("MD_POLL_INTERVAL", "3"))


def _parse_watchlist(env_key: str) -> list[str]:
    raw = os.getenv(env_key, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not set")
    r = redis.from_url(redis_url)

    repo = RedisPositionRepository(r)
    kis_feed = KisFeed()
    ibkr_feed = IbkrFeed()
    updater = MarketDataUpdater(r, repo, kis_feed, ibkr_feed)

    watchlist = {
        "KR": _parse_watchlist("GEN_WATCHLIST_KR"),
        "US": _parse_watchlist("GEN_WATCHLIST_US"),
    }
    print(f"Market data runner started. poll_interval={POLL_INTERVAL}s watchlist={watchlist}")

    while True:
        try:
            updater.run_once(watchlist)
        except Exception as e:
            print("md_runner_error:", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
