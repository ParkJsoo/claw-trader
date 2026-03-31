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

_SELECT_INTERVAL_SEC = float(os.getenv("UPBIT_WATCHLIST_INTERVAL_SEC", "600"))  # 10분
_SELECT_COUNT = int(os.getenv("UPBIT_WATCHLIST_COUNT", "15"))
_WL_KEY = "dynamic:watchlist:COIN"
_WL_TTL = 1200  # 20분 (interval보다 2배 — 선정 실패해도 기존 유지)

# 거래대금 기준 후보 풀
_VOL_TOP_N = int(os.getenv("UPBIT_VOL_TOP_N", "999"))

# 최소 거래대금 (원) — 유동성 필터
_MIN_VOL_KRW = float(os.getenv("UPBIT_MIN_VOL_KRW", "5000000000"))  # 50억

# 스테이블코인 / 거래 불가 블랙리스트
_SYMBOL_BLACKLIST: set[str] = {
    "KRW-USDT", "KRW-BUSD", "KRW-USDC", "KRW-DAI", "KRW-TUSD",
}

# 급등 스캔 — 거래대금 기준 외 종목 편입
_SURGE_RATE = float(os.getenv("UPBIT_SURGE_RATE", "0.05"))          # 당일 5% 이상
_SURGE_MIN_VOL_KRW = float(os.getenv("UPBIT_SURGE_MIN_VOL_KRW", "1000000000"))  # 10억
_SURGE_MAX_ADD = int(os.getenv("UPBIT_SURGE_MAX_ADD", "10"))         # 최대 추가 10개


def _log(msg: str) -> None:
    print(f"upbit_watchlist: {msg}", flush=True)


def select_watchlist(upbit_client) -> list[str]:
    """거래대금 상위 N개 + 당일 급등 종목을 합산하여 워치리스트 선정."""
    candidates = upbit_client.get_volume_rank(top_n=_VOL_TOP_N)

    # 거래대금 기준 상위 N개
    vol_selected: list[str] = []
    for item in candidates:
        symbol = item["symbol"]
        if symbol in _SYMBOL_BLACKLIST:
            continue
        if item["volume_krw"] < _MIN_VOL_KRW:
            continue
        vol_selected.append(symbol)
        if len(vol_selected) >= _SELECT_COUNT:
            break

    # 급등 종목 추가 편입 (거래대금 기준 미편입 종목 중)
    vol_set = set(vol_selected)
    surge_added: list[str] = []
    for item in sorted(candidates, key=lambda x: -x["change_rate"]):
        symbol = item["symbol"]
        if symbol in _SYMBOL_BLACKLIST or symbol in vol_set:
            continue
        if item["change_rate"] < _SURGE_RATE:
            break
        if item["volume_krw"] < _SURGE_MIN_VOL_KRW:
            continue
        surge_added.append(symbol)
        if len(surge_added) >= _SURGE_MAX_ADD:
            break

    if surge_added:
        _log(f"surge_added={surge_added} (rate>={_SURGE_RATE:.0%})")

    return vol_selected + surge_added


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

    _log(f"started interval_sec={_SELECT_INTERVAL_SEC} select_count={_SELECT_COUNT} min_vol_krw={_MIN_VOL_KRW/1e8:.0f}억 surge_rate={_SURGE_RATE:.0%} surge_min_vol={_SURGE_MIN_VOL_KRW/1e8:.0f}억")

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            try:
                selected = select_watchlist(upbit_client)
                if selected:
                    write_watchlist(r, selected)
                    _log(f"selected={selected} total={len(selected)}")
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
