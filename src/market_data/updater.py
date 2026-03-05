from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from redis import Redis

from market_data.kis_feed import KisFeed
from market_data.ibkr_feed import IbkrFeed
from portfolio.redis_repo import RedisPositionRepository

_KST = ZoneInfo("Asia/Seoul")

SYMBOL_COUNT_WARN = 20   # 심볼 수 이 이상이면 경고
ELAPSED_WARN_SEC = 1.0   # 갱신 소요 이 이상이면 경고


class MarketDataUpdater:
    """
    보유 포지션 심볼에 대해 현재가를 폴링하여 mark 가격 갱신.
    position_index:{market} 기반으로 활성 심볼만 조회 (불필요한 API 호출 최소화).
    갱신 후 recalc_unrealized 호출로 unrealized PnL 자동 갱신.
    에러 카운터: md:error:{market}:{YYYYMMDD} (HASH, TTL 7d) — 관측성
    """

    def __init__(
        self,
        redis: Redis,
        repo: RedisPositionRepository,
        kis_feed: KisFeed,
        ibkr_feed: IbkrFeed,
    ):
        self.redis = redis
        self.repo = repo
        self.kis_feed = kis_feed
        self.ibkr_feed = ibkr_feed

    def _get_active_symbols(self, market: str, extra: list[str] | None = None) -> list[str]:
        key = f"position_index:{market}"
        members = self.redis.smembers(key)
        symbols = {m.decode() for m in members} if members else set()
        if extra:
            symbols.update(extra)
        return list(symbols)

    def _record_errors(self, market: str, errors: dict[str, int]) -> None:
        """에러 카운터 Redis HASH 기록 (TTL 7d)."""
        if not errors:
            return
        today = datetime.now(_KST).strftime("%Y%m%d")
        key = f"md:error:{market}:{today}"
        pipe = self.redis.pipeline()
        for reason, cnt in errors.items():
            pipe.hincrby(key, reason, cnt)
        pipe.expire(key, 7 * 86400)
        pipe.execute()

    def update_market(self, market: str, extra_symbols: list[str] | None = None) -> None:
        """
        시장별 보유 심볼 현재가 갱신 + unrealized PnL 재계산.
        - 보유 포지션 없으면 no-op
        - 심볼 수 SYMBOL_COUNT_WARN 이상이면 경고
        - ELAPSED_WARN_SEC 초과 시 경고
        - 에러 카운터 Redis 기록
        """
        symbols = self._get_active_symbols(market, extra_symbols)
        if not symbols:
            return

        if len(symbols) >= SYMBOL_COUNT_WARN:
            print(f"md_warn: {market} symbol_count={len(symbols)} >= {SYMBOL_COUNT_WARN}")

        feed = self.kis_feed if market == "KR" else self.ibkr_feed
        t0 = time.time()
        updated = 0
        errors: dict[str, int] = {}

        for symbol in symbols:
            try:
                price = feed.get_price(symbol)
                if price is not None:
                    self.repo.set_mark_price(market, symbol, price)
                    updated += 1
                    ts_now = int(time.time() * 1000)
                    hist_key = f"mark_hist:{market}:{symbol}"
                    pipe = self.redis.pipeline()
                    pipe.lpush(hist_key, f"{ts_now}:{price}")
                    pipe.ltrim(hist_key, 0, 299)
                    pipe.expire(hist_key, 2 * 86400)
                    pipe.execute()
                else:
                    # Delayed Frozen 모드(market_data_type=4)는 price_none 카운트 제외 — 노이즈 방지
                    if not (market == "US" and getattr(feed, "market_data_type", None) == 4):
                        errors["price_none"] = errors.get("price_none", 0) + 1
            except Exception as e:
                reason = type(e).__name__
                errors[reason] = errors.get(reason, 0) + 1
                print(f"md_update_error: {market}:{symbol} {e}")

        elapsed = time.time() - t0
        if elapsed > ELAPSED_WARN_SEC:
            print(f"md_slow: {market} elapsed={elapsed:.2f}s symbols={len(symbols)}")

        self._record_errors(market, errors)

        if updated > 0:
            self.repo.recalc_unrealized(market)
            ts_ms = str(int(time.time() * 1000))
            self.redis.set(f"md:last_update:{market}", ts_ms)

    def run_once(self, watchlist: dict[str, list[str]] | None = None) -> None:
        """KR + US 시장 순서대로 갱신. watchlist 심볼도 포함."""
        wl = watchlist or {}
        self.update_market("KR", wl.get("KR"))
        self.update_market("US", wl.get("US"))
