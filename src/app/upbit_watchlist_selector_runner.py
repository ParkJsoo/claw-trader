"""upbit_watchlist_selector_runner — 업비트 코인 동적 워치리스트 선정.

24/7 운영. 10분마다 거래대금 상위 코인 선정 + 90초마다 급등 스캔으로 즉시 편입.
Redis SET `dynamic:watchlist:COIN` 에 저장.

기동:
    PYTHONPATH=src venv/bin/python -m app.upbit_watchlist_selector_runner
"""
from dotenv import load_dotenv
load_dotenv()

import builtins as _builtins
import os
import signal as _signal
import sys
import time
from datetime import datetime

# 모든 print에 타임스탬프 자동 prefix
_orig_print = _builtins.print
def print(*args, sep=' ', end='\n', file=None, flush=False):  # noqa: A001
    _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args and isinstance(args[0], str):
        _orig_print(f"[{_ts}] {args[0]}", *args[1:], sep=sep, end=end, file=file, flush=flush)
    else:
        _orig_print(f"[{_ts}]", *args, sep=sep, end=end, file=file, flush=flush)

import redis

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_LOCK_KEY = "upbit:watchlist:selector:lock"
_LOCK_TTL = 120

_SELECT_INTERVAL_SEC = float(os.getenv("UPBIT_WATCHLIST_INTERVAL_SEC", "600"))  # 10분
_SURGE_INTERVAL_SEC = float(os.getenv("UPBIT_SURGE_INTERVAL_SEC", "90"))       # 급등 스캔 90초
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


def scan_and_add_surge(r: redis.Redis, upbit_client) -> None:
    """급등 종목만 빠르게 스캔해서 워치리스트에 즉시 추가 (기존 항목 유지)."""
    candidates = upbit_client.get_volume_rank(top_n=_VOL_TOP_N)
    current = {s.decode() if isinstance(s, bytes) else s for s in r.smembers(_WL_KEY)}

    new_symbols: list[str] = []
    for item in sorted(candidates, key=lambda x: -x["change_rate"]):
        symbol = item["symbol"]
        if symbol in _SYMBOL_BLACKLIST or symbol in current:
            continue
        if item["change_rate"] < _SURGE_RATE:
            break
        if item["volume_krw"] < _SURGE_MIN_VOL_KRW:
            continue
        new_symbols.append(symbol)
        if len(new_symbols) >= _SURGE_MAX_ADD:
            break

    if new_symbols:
        pipe = r.pipeline(transaction=True)
        pipe.sadd(_WL_KEY, *new_symbols)
        pipe.expire(_WL_KEY, _WL_TTL)
        pipe.execute()
        _log(f"surge_instant_added={new_symbols} (rate>={_SURGE_RATE:.0%})")


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

    _log(f"started interval_sec={_SELECT_INTERVAL_SEC} surge_interval_sec={_SURGE_INTERVAL_SEC} select_count={_SELECT_COUNT} min_vol_krw={_MIN_VOL_KRW/1e8:.0f}억 surge_rate={_SURGE_RATE:.0%} surge_min_vol={_SURGE_MIN_VOL_KRW/1e8:.0f}억")

    last_full_select = 0.0

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            now = time.time()

            if now - last_full_select >= _SELECT_INTERVAL_SEC:
                # 전체 선정 (거래대금 기반 + 급등 스캔, 워치리스트 교체)
                try:
                    selected = select_watchlist(upbit_client)
                    if selected:
                        write_watchlist(r, selected)
                        _log(f"selected={selected} total={len(selected)}")
                    else:
                        _log("no candidates — watchlist unchanged")
                    last_full_select = time.time()
                except Exception as e:
                    _log(f"full_select error: {e}")
            else:
                # 급등 스캔만 (기존 워치리스트 유지하며 즉시 추가)
                try:
                    scan_and_add_surge(r, upbit_client)
                except Exception as e:
                    _log(f"surge_scan error: {e}")

            remaining = _SURGE_INTERVAL_SEC
            while remaining > 0:
                sleep_chunk = min(15.0, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk
                r.expire(_LOCK_KEY, _LOCK_TTL)

    finally:
        r.delete(_LOCK_KEY)
        _log("lock released")


if __name__ == "__main__":
    main()
