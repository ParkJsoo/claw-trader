"""upbit_watchlist_selector_runner — 업비트 코인 동적 워치리스트 선정.

24/7 운영. 1시간마다 거래대금 상위 코인 중 mean reversion 후보 선정.
Redis SET `dynamic:watchlist:COIN` 에 저장.

기동:
    PYTHONPATH=src venv/bin/python -m app.upbit_watchlist_selector_runner
"""
from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
import sys
import time

import redis

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_LOCK_KEY = "upbit:watchlist:selector:lock"
_LOCK_TTL = 120

_SELECT_INTERVAL_SEC = float(os.getenv("UPBIT_WATCHLIST_INTERVAL_SEC", "3600"))  # 1시간
_SELECT_COUNT = int(os.getenv("UPBIT_WATCHLIST_COUNT", "15"))
_WL_KEY = "dynamic:watchlist:COIN"
_WL_TTL = 2 * 3600  # 2시간 (interval보다 2배 — 선정 실패해도 기존 유지)

# 거래대금 기준 후보 풀
_VOL_TOP_N = int(os.getenv("UPBIT_VOL_TOP_N", "50"))

# 최소 거래대금 (원) — 유동성 필터
_MIN_VOL_KRW = float(os.getenv("UPBIT_MIN_VOL_KRW", "10000000000"))  # 100억

# 스테이블코인 / 거래 불가 블랙리스트
_SYMBOL_BLACKLIST: set[str] = {
    "KRW-USDT", "KRW-BUSD", "KRW-USDC", "KRW-DAI", "KRW-TUSD",
}


def _log(msg: str) -> None:
    print(f"upbit_watchlist: {msg}", flush=True)


def select_watchlist(upbit_client) -> list[str]:
    """거래대금 상위 코인 중 블랙리스트 제외 후 상위 N개 선정."""
    candidates = upbit_client.get_volume_rank(top_n=_VOL_TOP_N)

    selected = []
    for item in candidates:
        symbol = item["symbol"]
        if symbol in _SYMBOL_BLACKLIST:
            continue
        if item["volume_krw"] < _MIN_VOL_KRW:
            continue
        selected.append(symbol)
        if len(selected) >= _SELECT_COUNT:
            break

    return selected


def write_watchlist(r: redis.Redis, symbols: list[str]) -> None:
    # transaction=True (기본값): MULTI/EXEC로 DELETE+SADD 원자적 수행
    pipe = r.pipeline(transaction=True)
    pipe.delete(_WL_KEY)
    if symbols:
        pipe.sadd(_WL_KEY, *symbols)
        pipe.expire(_WL_KEY, _WL_TTL)
    pipe.execute()


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

    try:
        from exchange.upbit.client import UpbitClient
        upbit_client = UpbitClient()
    except Exception as e:
        _log(f"UpbitClient init failed: {e} — exiting")
        r.delete(_LOCK_KEY)
        sys.exit(1)

    _log(f"started interval_sec={_SELECT_INTERVAL_SEC} select_count={_SELECT_COUNT} min_vol_krw={_MIN_VOL_KRW/1e8:.0f}억")

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            try:
                selected = select_watchlist(upbit_client)
                if selected:
                    write_watchlist(r, selected)
                    _log(f"selected={selected}")
                else:
                    _log("no candidates — watchlist unchanged")
            except Exception as e:
                _log(f"error: {e}")

            remaining = _SELECT_INTERVAL_SEC
            while remaining > 0:
                sleep_chunk = min(30.0, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk
                r.expire(_LOCK_KEY, _LOCK_TTL)

    finally:
        r.delete(_LOCK_KEY)
        _log("lock released")


if __name__ == "__main__":
    main()
