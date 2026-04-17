"""daily_report_runner 단위 테스트."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scripts.daily_report_runner import _send_report, _sync_intraday_realized_pnl

_KST = ZoneInfo("Asia/Seoul")


def _add_trade(r, market, symbol, pnl, ts_ms):
    trade_id = f"trade-{symbol}-{ts_ms}"
    r.hset(f"trade:{market}:{trade_id}", mapping={
        "symbol": symbol,
        "side": "SELL",
        "qty": "1",
        "price": "100",
        "realized_pnl": str(pnl),
        "ts": str(ts_ms),
        "fee": "0",
        "signal_id": "test",
        "source": "test",
    })
    r.zadd(f"trade_index:{market}:{symbol}", {trade_id: ts_ms})
    r.sadd(f"trade_symbols:{market}", symbol)


def _kst_ms(date_str: str, hh: int, mm: int, ss: int = 0) -> int:
    dt = datetime.strptime(date_str, "%Y%m%d").replace(
        hour=hh,
        minute=mm,
        second=ss,
        tzinfo=_KST,
    )
    return int(dt.timestamp() * 1000)


def test_send_report_saves_coin_daily_key(monkeypatch):
    r = fakeredis.FakeRedis()
    _add_trade(r, "COIN", "KRW-BTC", Decimal("1000.5"), _kst_ms("20260405", 10, 0))
    monkeypatch.setattr("scripts.daily_report_runner.send_telegram", lambda msg: None)

    stats = _send_report(r, "COIN", "20260405")

    saved = r.hgetall("perf:daily:COIN:20260405")
    assert stats["trade_count"] == 1
    assert saved[b"trade_count"].decode() == "1"
    assert r.get("perf:report_sent:COIN:20260405") == b"1"


def test_sync_intraday_realized_pnl_updates_coin_pnl():
    r = fakeredis.FakeRedis()
    _add_trade(r, "COIN", "KRW-ETH", Decimal("1200.25"), _kst_ms("20260405", 10, 0))
    _add_trade(r, "COIN", "KRW-XRP", Decimal("-200.25"), _kst_ms("20260405", 10, 0, 1))
    r.hset("pnl:COIN", mapping={"realized_pnl": "0", "unrealized_pnl": "7", "currency": "KRW"})

    stats = _sync_intraday_realized_pnl(r, "COIN", "20260405")
    pnl = r.hgetall("pnl:COIN")

    assert Decimal(stats["net_pnl"]) == Decimal("1000.00")
    assert pnl[b"realized_pnl"].decode() == "1000.00"
    assert pnl[b"unrealized_pnl"].decode() == "7"
