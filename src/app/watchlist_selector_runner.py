"""watchlist_selector_runner — 동적 워치리스트 선정.

유니버스에서 뉴스 sentiment + 모멘텀 기반으로 상위 N 종목을 선정하여
Redis SET `dynamic:watchlist:{market}` 에 저장한다.

기동:
    PYTHONPATH=src venv/bin/python -m app.watchlist_selector_runner
"""
from dotenv import load_dotenv
load_dotenv()

import json
import logging
import os
import signal as _signal
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis

from utils.redis_helpers import parse_watchlist, today_kst

logger = logging.getLogger(__name__)

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

def _add_inverse_etf(market: str, selected: list[str]) -> list[str]:
    """KR이고 INVERSE_ETF_ENABLED이면 인버스 ETF 항상 포함."""
    if market != "KR" or not _INVERSE_ETF_ENABLED:
        return selected
    extra = [s for s in _INVERSE_ETF_KR if s not in selected]
    return selected + extra


def score_symbol(r, market: str, symbol: str, today: str) -> float:
    """뉴스 sentiment + 모멘텀으로 심볼 점수 계산."""
    # Phase 19: 인버스 ETF는 regime filter가 허용 여부를 결정하므로 중립 점수
    if market == "KR" and symbol in _INVERSE_ETF_KR:
        return 0.0
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

# Phase 19: 인버스 ETF
_INVERSE_ETF_KR = set(os.getenv("INVERSE_ETF_KR", "114800,251340").split(","))
_INVERSE_ETF_ENABLED = os.getenv("INVERSE_ETF_ENABLED", "true").lower() not in ("false", "0", "no")

_KR_MAX_PRICE = int(os.getenv("WATCHLIST_KR_MAX_PRICE", "150000"))  # KR 최대 매수 가능 가격 (원)


def select_watchlist(r, market: str, universe: list[str], count: int) -> list[str]:
    """유니버스에서 상위 N 종목 선정. KR은 가격 필터 적용."""
    today = today_kst()

    scored = []
    for symbol in universe:
        # KR: mark price 기준 가격 필터 (잔고로 매수 불가한 고가주 및 mark 없는 종목 제외)
        # Phase 19: 인버스 ETF는 가격 필터 제외
        if market == "KR" and symbol not in _INVERSE_ETF_KR:
            price_raw = r.get(f"mark:KR:{symbol}")
            if not price_raw:
                continue  # mark 데이터 없으면 제외 (AI 신호 불가)
            try:
                price = float(price_raw.decode() if isinstance(price_raw, bytes) else price_raw)
                if price > _KR_MAX_PRICE:
                    continue
            except (ValueError, TypeError):
                continue

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
# 동적 유니버스 선정 (거래량 + 등락률 교집합)
# ---------------------------------------------------------------------------

_DYNAMIC_VOLUME_TOP_N = int(os.getenv("WATCHLIST_DYNAMIC_VOLUME_TOP_N", "30"))
_DYNAMIC_FLUCT_TOP_N = int(os.getenv("WATCHLIST_DYNAMIC_FLUCT_TOP_N", "30"))
_DYNAMIC_FALLBACK_N = int(os.getenv("WATCHLIST_DYNAMIC_FALLBACK_N", "20"))


def select_watchlist_dynamic(r, count: int, kis_client=None) -> list[str] | None:
    """KIS API로 거래량/등락률 순위 교집합 → 동적 universe 자동 선정.

    1. 거래량 순위 상위 N개 + 등락률 순위 상위 N개 교집합 → universe
    2. 교집합이 없으면 거래량 순위 상위 FALLBACK_N개 사용
    3. KisClient 사용 불가 시 None 반환 (호출측에서 기존 로직 fallback)

    Returns: 선정된 심볼 리스트, 또는 None (fallback 필요)
    """
    kis = kis_client
    if kis is None:
        try:
            from exchange.kis.client import KisClient
            kis = KisClient()
        except Exception as e:
            print(f"watchlist_selector: KisClient unavailable ({e}) — skipping dynamic", flush=True)
            return None

    try:
        vol_items = kis.get_volume_rank(price_min=1000, price_max=50000, min_vol=100000)
        flu_items = kis.get_fluctuation_rank(price_min=1000, price_max=50000, min_rate=1.0)
    except Exception as e:
        print(f"watchlist_selector: KIS rank API error ({e}) — skipping dynamic", flush=True)
        return None

    vol_symbols = [item["symbol"] for item in vol_items[:_DYNAMIC_VOLUME_TOP_N]]
    flu_symbols = [item["symbol"] for item in flu_items[:_DYNAMIC_FLUCT_TOP_N]]

    vol_set = set(vol_symbols)
    flu_set = set(flu_symbols)
    intersection = [s for s in vol_symbols if s in flu_set]  # vol 순서 유지

    if intersection:
        universe = intersection
        print(
            f"watchlist_selector: dynamic universe intersection={len(universe)} "
            f"(vol_top={len(vol_symbols)}, flu_top={len(flu_symbols)})",
            flush=True,
        )
    else:
        universe = vol_symbols[:_DYNAMIC_FALLBACK_N]
        print(
            f"watchlist_selector: dynamic universe fallback to vol_top={len(universe)} "
            f"(no intersection between vol/flu)",
            flush=True,
        )

    if not universe:
        return None

    # KIS API에서 이미 가격 필터 적용됐으므로 mark 없어도 허용 (score만 적용)
    today = today_kst()
    scored = [(sym, score_symbol(r, "KR", sym, today)) for sym in universe]
    scored.sort(key=lambda x: -x[1])
    selected = [sym for sym, _ in scored[:count]]
    return selected


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

    universe_us = parse_watchlist("GEN_UNIVERSE_US")
    if not universe_us:
        universe_us = parse_watchlist("GEN_WATCHLIST_US")

    print(
        f"watchlist_selector: started interval_sec={_SELECT_INTERVAL_SEC} "
        f"select_count={_SELECT_COUNT} universe_kr={universe_kr} universe_us={universe_us}",
        flush=True,
    )

    _use_dynamic_kr = os.getenv("WATCHLIST_KR_DYNAMIC", "true").lower() not in ("false", "0", "no")

    # H3: KisClient 1회 생성 후 재사용 (세션 누수 방지)
    _kis_client = None
    if _use_dynamic_kr:
        try:
            from exchange.kis.client import KisClient
            _kis_client = KisClient()
        except Exception as e:
            print(f"watchlist_selector: KisClient init failed ({e}), will retry per cycle", flush=True)

    # dynamic 모드이면 universe_kr 없어도 KIS API로 선정 가능
    if not universe_kr and not universe_us and not _use_dynamic_kr:
        print("watchlist_selector: no universe defined — exiting", flush=True)
        r.delete(_LOCK_KEY)
        sys.exit(1)

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            if universe_kr or _use_dynamic_kr:
                if _use_dynamic_kr:
                    selected = select_watchlist_dynamic(r, _SELECT_COUNT, kis_client=_kis_client)
                    if selected is None:
                        # KIS 클라이언트 불가 → 기존 env var universe fallback
                        selected = select_watchlist(r, "KR", universe_kr, _SELECT_COUNT) if universe_kr else []
                        print(
                            f"watchlist_selector: KR fallback static selected={selected} "
                            f"from universe={len(universe_kr)} symbols",
                            flush=True,
                        )
                    else:
                        print(
                            f"watchlist_selector: KR dynamic selected={selected}",
                            flush=True,
                        )
                else:
                    selected = select_watchlist(r, "KR", universe_kr, _SELECT_COUNT)
                    print(
                        f"watchlist_selector: KR selected={selected} "
                        f"from universe={len(universe_kr)} symbols",
                        flush=True,
                    )
                if selected:
                    selected = _add_inverse_etf("KR", selected)
                    write_watchlist(r, "KR", selected)

            if universe_us:
                selected_us = select_watchlist(r, "US", universe_us, _SELECT_COUNT)
                write_watchlist(r, "US", selected_us)
                print(
                    f"watchlist_selector: US selected={selected_us} "
                    f"from universe={len(universe_us)} symbols",
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
