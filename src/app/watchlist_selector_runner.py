"""watchlist_selector_runner — 동적 워치리스트 선정.

유니버스에서 뉴스 sentiment + 모멘텀 기반으로 상위 N 종목을 선정하여
Redis SET `dynamic:watchlist:{market}` 에 저장한다.

기동:
    PYTHONPATH=src venv/bin/python -m app.watchlist_selector_runner
"""
from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis

from utils.redis_helpers import parse_watchlist, today_kst

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "watchlist:selector:lock"
_LOCK_TTL = 600

_SELECT_INTERVAL_SEC = float(os.getenv("WATCHLIST_SELECT_INTERVAL_SEC", "21600"))  # 6시간
_SELECT_COUNT = int(os.getenv("UNIVERSE_SELECT_COUNT", "8"))
_WL_TTL = 8 * 3600  # 8시간

# 뉴스 sentiment/impact 점수 매핑
_SCORE_MAP = {
    ("positive", "high"): 2,
    ("positive", "medium"): 1,
    ("positive", "low"): 0,
    ("negative", "high"): -2,
    ("negative", "medium"): -1,
    ("negative", "low"): 0,
    ("neutral", "high"): 0,
    ("neutral", "medium"): 0,
    ("neutral", "low"): 0,
}


# ---------------------------------------------------------------------------
# 점수 계산
# ---------------------------------------------------------------------------

def score_symbol(r, market: str, symbol: str, today: str) -> float:
    """뉴스 sentiment + 모멘텀으로 심볼 점수 계산."""
    score = 0.0

    # 1. 뉴스 점수 (오늘 + 어제)
    for date_str in _get_dates(today):
        news_key = f"news:symbol:{market}:{symbol}:{date_str}"
        items = r.lrange(news_key, 0, 9)  # 최대 10건
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                sentiment = d.get("sentiment", "neutral").lower()
                impact = d.get("impact", "medium").lower()
                score += _SCORE_MAP.get((sentiment, impact), 0)
            except Exception:
                continue

    # 2. 모멘텀 점수 (mark 데이터에서 최신 가격 변동)
    mark_key = f"mark:{market}:{symbol}"
    try:
        mark_data = r.hgetall(mark_key)
        if mark_data:
            # ret_5m 이 있으면 모멘텀 보너스
            ret_5m_raw = mark_data.get(b"ret_5m") or mark_data.get("ret_5m")
            if ret_5m_raw:
                ret_5m = float(ret_5m_raw.decode() if isinstance(ret_5m_raw, bytes) else ret_5m_raw)
                if ret_5m > 0:
                    score += 1.0  # 양의 모멘텀 보너스
    except Exception:
        pass

    return score


def _get_dates(today: str) -> list[str]:
    """오늘과 어제 날짜 반환."""
    try:
        dt = datetime.strptime(today, "%Y%m%d")
        yesterday = (dt - timedelta(days=1)).strftime("%Y%m%d")
        return [today, yesterday]
    except ValueError:
        return [today]


# ---------------------------------------------------------------------------
# 선정 로직
# ---------------------------------------------------------------------------

def select_watchlist(r, market: str, universe: list[str], count: int) -> list[str]:
    """유니버스에서 상위 N 종목 선정."""
    today = today_kst()

    scored = []
    for symbol in universe:
        s = score_symbol(r, market, symbol, today)
        scored.append((symbol, s))

    # 점수 내림차순, 동점이면 원래 순서 유지
    scored.sort(key=lambda x: -x[1])

    selected = [sym for sym, _ in scored[:count]]
    return selected


def write_watchlist(r, market: str, symbols: list[str]) -> None:
    """Redis SET에 동적 워치리스트 저장."""
    redis_key = f"dynamic:watchlist:{market}"
    pipe = r.pipeline()
    pipe.delete(redis_key)
    if symbols:
        pipe.sadd(redis_key, *symbols)
    pipe.expire(redis_key, _WL_TTL)
    pipe.execute()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("watchlist_selector: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 프로세스 락
    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("watchlist_selector: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("watchlist_selector: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    universe_kr = parse_watchlist("GEN_UNIVERSE_KR")
    if not universe_kr:
        # fallback: 기존 워치리스트를 유니버스로 사용
        universe_kr = parse_watchlist("GEN_WATCHLIST_KR")

    if not universe_kr:
        print("watchlist_selector: no universe defined — exiting", flush=True)
        r.delete(_LOCK_KEY)
        sys.exit(1)

    print(
        f"watchlist_selector: started interval_sec={_SELECT_INTERVAL_SEC} "
        f"select_count={_SELECT_COUNT} universe_kr={universe_kr}",
        flush=True,
    )

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            selected = select_watchlist(r, "KR", universe_kr, _SELECT_COUNT)
            write_watchlist(r, "KR", selected)

            print(
                f"watchlist_selector: KR selected={selected} "
                f"from universe={len(universe_kr)} symbols",
                flush=True,
            )

            # 다음 선정까지 대기 (30초 단위로 lock 갱신)
            remaining = _SELECT_INTERVAL_SEC
            while remaining > 0:
                sleep_chunk = min(30.0, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk
                r.expire(_LOCK_KEY, _LOCK_TTL)

    finally:
        r.delete(_LOCK_KEY)
        print("watchlist_selector: lock released", flush=True)


if __name__ == "__main__":
    main()
