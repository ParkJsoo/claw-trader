"""upbit_feed — 업비트 REST 폴링 기반 시세 수집.

mark_hist:COIN:{symbol} 에 "{ts_ms}:{price}" 형식으로 LPUSH.
mark:COIN:{symbol} 에 현재가 저장.
vol:COIN:{symbol}:{date} 에 24h 거래대금(원) 저장.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import redis

_MARK_HIST_MAX = int(os.getenv("UPBIT_MARK_HIST_MAX", "720"))
_KST_OFFSET = 9 * 3600


def _today_kst() -> str:
    ts = time.time() + _KST_OFFSET
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


class UpbitFeed:
    def __init__(self, upbit_client, r: redis.Redis) -> None:
        self.client = upbit_client
        self.r = r

    def update(self, symbols: list[str]) -> dict[str, int]:
        """symbols 시세 일괄 조회 후 Redis 저장. 에러 카운트 반환."""
        if not symbols:
            return {}

        errors: dict[str, int] = {}
        try:
            tickers = self.client.get_tickers(symbols)
        except Exception as e:
            print(f"upbit_feed: get_tickers error {e}", flush=True)
            return {"fetch_error": 1}

        now_ms = int(time.time() * 1000)
        today = _today_kst()
        pipe = self.r.pipeline()

        for t in tickers:
            symbol = t.get("market", "")
            try:
                price = Decimal(str(t["trade_price"]))
                volume_krw = float(t.get("acc_trade_price_24h", 0))

                # mark (현재가, TTL 300s — stale 가격 방지)
                pipe.set(f"mark:COIN:{symbol}", str(price), ex=300)

                # mark_hist (시계열, 최신이 index 0)
                hist_key = f"mark_hist:COIN:{symbol}"
                pipe.lpush(hist_key, f"{now_ms}:{price}")
                pipe.ltrim(hist_key, 0, _MARK_HIST_MAX - 1)

                # 24h 거래대금
                pipe.set(f"vol:COIN:{symbol}:{today}", str(volume_krw))

            except Exception as e:
                errors[symbol] = errors.get(symbol, 0) + 1
                print(f"upbit_feed: {symbol} error {e}", flush=True)

        try:
            pipe.execute()
        except Exception as e:
            print(f"upbit_feed: pipeline error {e}", flush=True)
            errors["pipeline"] = 1

        return errors
