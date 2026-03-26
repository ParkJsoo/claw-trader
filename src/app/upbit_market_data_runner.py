"""upbit_market_data_runner — 업비트 시세 수집 프로세스.

dynamic:watchlist:COIN 워치리스트 기반으로 30초마다 시세 폴링.

기동:
    PYTHONPATH=src venv/bin/python -m app.upbit_market_data_runner
"""
from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
import sys
import time

import redis

from exchange.upbit.client import UpbitClient
from market_data.upbit_feed import UpbitFeed

_LOCK_KEY = "upbit:md:runner:lock"
_LOCK_TTL = 120
_POLL_SEC = float(os.getenv("UPBIT_MD_POLL_SEC", "30"))
_WL_KEY = "dynamic:watchlist:COIN"


def _log(msg: str) -> None:
    print(f"upbit_md: {msg}", flush=True)


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        _log("REDIS_URL not set — exiting")
        sys.exit(1)

    r = redis.from_url(redis_url)

    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        _log("already running (lock exists) — exiting")
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        _log("SIGTERM received, lock released")
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    client = UpbitClient()
    feed = UpbitFeed(client, r)

    _log(f"started poll_sec={_POLL_SEC}")

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            # 워치리스트 로드
            symbols = [s.decode() if isinstance(s, bytes) else s
                       for s in r.smembers(_WL_KEY)]

            if not symbols:
                _log("watchlist empty — skipping")
            else:
                t0 = time.time()
                errors = feed.update(symbols)
                elapsed = time.time() - t0
                _log(f"updated symbols={len(symbols)} elapsed={elapsed:.2f}s errors={errors or 0}")

            time.sleep(_POLL_SEC)

    finally:
        r.delete(_LOCK_KEY)
        _log("lock released")


if __name__ == "__main__":
    main()
