from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_data.updater import MarketDataUpdater


_KST = ZoneInfo("Asia/Seoul")


class _Repo:
    def __init__(self):
        self.mark_updates: list[tuple[str, str, Decimal]] = []
        self.recalc_calls: list[str] = []

    def set_mark_price(self, market: str, symbol: str, price: Decimal) -> None:
        self.mark_updates.append((market, symbol, price))

    def recalc_unrealized(self, market: str) -> None:
        self.recalc_calls.append(market)


class _Feed:
    def __init__(self, prices: dict[str, Decimal | None], reasons: dict[str, str | None]):
        self.prices = prices
        self.reasons = reasons
        self.last_error_reason = None

    def get_price(self, symbol: str):
        self.last_error_reason = self.reasons.get(symbol)
        return self.prices.get(symbol)


def test_update_market_records_feed_error_reason(monkeypatch):
    monkeypatch.setattr("market_data.updater.KR_SYMBOL_PACE_SEC", 0.0)

    r = fakeredis.FakeRedis()
    r.sadd("position_index:KR", "005930")
    repo = _Repo()
    feed = _Feed(
        prices={"005930": None, "000660": Decimal("12345")},
        reasons={"005930": "kis_price_rate_limit", "000660": None},
    )
    updater = MarketDataUpdater(r, repo, feed, None)

    updater.update_market("KR", ["000660"])

    errors = r.hgetall(f"md:error:KR:{datetime.now(_KST).strftime('%Y%m%d')}")
    decoded = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in errors.items()}

    assert decoded["kis_price_rate_limit"] == 1
    assert "price_none" not in decoded
    assert repo.mark_updates == [("KR", "000660", Decimal("12345"))]
    assert repo.recalc_calls == ["KR"]


def test_update_market_logs_slow_breakdown_for_kr(monkeypatch, capsys):
    monkeypatch.setattr("market_data.updater.KR_SYMBOL_PACE_SEC", 0.25)
    monkeypatch.setattr("market_data.updater.ELAPSED_WARN_SEC", 1.0)

    time_values = iter([100.0, 100.1, 100.2, 104.6, 104.7])
    fake_clock = SimpleNamespace(
        time=lambda: next(time_values),
        sleep=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("market_data.updater.time", fake_clock)

    r = fakeredis.FakeRedis()
    repo = _Repo()
    feed = _Feed(
        prices={"005930": Decimal("70000"), "000660": Decimal("120000")},
        reasons={"005930": None, "000660": None},
    )
    updater = MarketDataUpdater(r, repo, feed, None)

    updater.update_market("KR", ["005930", "000660"])

    captured = capsys.readouterr().out
    assert "md_slow: KR elapsed=4.60s symbols=2 updated=2 pace=0.25s work=4.35s" in captured
