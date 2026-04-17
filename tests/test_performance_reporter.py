"""performance_reporter 단위 테스트."""
from __future__ import annotations

import json
import time
from decimal import Decimal

import fakeredis
import pytest

from app.performance_reporter import PerformanceReporter


def _add_trade(r, market, symbol, side, pnl, ts_ms=None):
    """테스트용 trade 데이터 삽입 헬퍼."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    trade_id = f"trade-{symbol}-{ts_ms}"
    r.hset(f"trade:{market}:{trade_id}", mapping={
        "symbol": symbol,
        "side": side,
        "qty": "10",
        "price": "50000",
        "realized_pnl": str(pnl),
        "ts": str(ts_ms),
        "fee": "0",
        "signal_id": "test",
        "source": "test",
    })
    r.zadd(f"trade_index:{market}:{symbol}", {trade_id: ts_ms})
    r.sadd(f"trade_symbols:{market}", symbol)  # position_engine과 동일하게 SET 관리
    return trade_id


def _today_ms():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from utils.redis_helpers import today_kst
    from unittest.mock import patch
    return int(datetime.now(ZoneInfo("Asia/Seoul")).replace(hour=10).timestamp() * 1000)


class TestComputeDailyStats:
    def test_no_trades_returns_zeros(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)
        stats = reporter.compute_daily_stats("KR", "20260318")
        assert stats["trade_count"] == 0
        assert stats["win_rate"] == 0.0

    def test_win_rate_calculation(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        _add_trade(r, "KR", "005930", "SELL", Decimal("1000"), base_ms)
        _add_trade(r, "KR", "005930", "SELL", Decimal("2000"), base_ms + 1000)
        _add_trade(r, "KR", "000660", "SELL", Decimal("-500"), base_ms + 2000)

        stats = reporter.compute_daily_stats("KR", "20260318")
        assert stats["trade_count"] == 3
        assert stats["win_count"] == 2
        assert stats["loss_count"] == 1
        assert abs(stats["win_rate"] - 66.7) < 0.1

    def test_net_pnl_calculation(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        _add_trade(r, "KR", "005930", "SELL", Decimal("3000"), base_ms)
        _add_trade(r, "KR", "000660", "SELL", Decimal("-1000"), base_ms + 1000)

        stats = reporter.compute_daily_stats("KR", "20260318")
        assert Decimal(stats["net_pnl"]) == Decimal("2000")
        assert Decimal(stats["gross_profit"]) == Decimal("3000")
        assert Decimal(stats["gross_loss"]) == Decimal("1000")

    def test_profit_factor(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        _add_trade(r, "KR", "005930", "SELL", Decimal("3000"), base_ms)
        _add_trade(r, "KR", "000660", "SELL", Decimal("-1000"), base_ms + 1000)

        stats = reporter.compute_daily_stats("KR", "20260318")
        assert abs(stats["profit_factor"] - 3.0) < 0.01

    def test_buy_trades_excluded(self):
        """BUY 체결은 성과 통계에서 제외."""
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        _add_trade(r, "KR", "005930", "BUY", Decimal("0"), base_ms)
        _add_trade(r, "KR", "005930", "SELL", Decimal("1000"), base_ms + 1000)

        stats = reporter.compute_daily_stats("KR", "20260318")
        assert stats["trade_count"] == 1  # SELL만 집계

    def test_max_drawdown(self):
        """누적 PnL 최대 낙폭 계산."""
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        # +1000, -500, +200 → peak=1000, dd=500
        _add_trade(r, "KR", "005930", "SELL", Decimal("1000"), base_ms)
        _add_trade(r, "KR", "000660", "SELL", Decimal("-500"), base_ms + 1000)
        _add_trade(r, "KR", "035720", "SELL", Decimal("200"), base_ms + 2000)

        stats = reporter.compute_daily_stats("KR", "20260318")
        assert Decimal(stats["max_drawdown"]) == Decimal("500")

    def test_save_and_get(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)
        stats = {"date": "20260318", "market": "KR", "trade_count": "3", "win_rate": "66.7"}
        reporter.save_daily_stats("KR", "20260318", stats)
        loaded = reporter.get_daily_stats("KR", "20260318")
        assert loaded["trade_count"] == "3"
        assert loaded["win_rate"] == "66.7"

    def test_sync_realized_pnl_updates_market_pnl(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        _add_trade(r, "COIN", "KRW-BTC", "SELL", Decimal("3000.12"), base_ms)
        _add_trade(r, "COIN", "KRW-ETH", "SELL", Decimal("-1000.01"), base_ms + 1000)
        r.hset("pnl:COIN", mapping={"realized_pnl": "999", "unrealized_pnl": "321", "currency": "KRW"})

        stats = reporter.sync_realized_pnl("COIN", "20260318")
        pnl = r.hgetall("pnl:COIN")

        assert Decimal(stats["net_pnl"]) == Decimal("2000.11")
        assert Decimal(pnl[b"realized_pnl"].decode()) == Decimal("2000.11")
        assert pnl[b"unrealized_pnl"].decode() == "321"
        assert pnl[b"currency"].decode() == "KRW"

    def test_scan_trade_index_fallback_captures_symbols_missing_from_trade_symbols_set(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        base_ms = int(datetime(2026, 4, 17, 20, 15, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)

        trade_id = f"trade-KRW-KAT-{base_ms}"
        r.hset(f"trade:COIN:{trade_id}", mapping={
            "symbol": "KRW-KAT",
            "side": "SELL",
            "qty": "10",
            "price": "13.6",
            "realized_pnl": "-557.74",
            "ts": str(base_ms),
            "fee": "0",
            "signal_id": "test",
            "source": "test",
        })
        r.zadd("trade_index:COIN:KRW-KAT", {trade_id: base_ms})
        # trade_symbols:COIN intentionally left empty

        stats = reporter.compute_daily_stats("COIN", "20260417")

        assert stats["trade_count"] == 1
        assert Decimal(stats["net_pnl"]) == Decimal("-557.74")


class TestFormatReport:
    def test_no_trades_message(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)
        stats = {"date": "20260318", "trade_count": "0"}
        msg = reporter.format_report("KR", stats)
        assert "체결 없음" in msg

    def test_format_with_trades(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)
        stats = {
            "date": "20260318", "market": "KR",
            "trade_count": "3", "win_count": "2", "loss_count": "1",
            "win_rate": "66.7", "net_pnl": "2000",
            "profit_factor": "3.0", "avg_rr": "2.5",
            "best_trade_symbol": "005930", "best_trade_pnl": "1500",
            "worst_trade_symbol": "000660", "worst_trade_pnl": "-500",
            "max_drawdown": "500",
        }
        msg = reporter.format_report("KR", stats)
        assert "66.7%" in msg
        assert "2000" in msg
        assert "005930" in msg

    def test_coin_report_uses_krw_label(self):
        r = fakeredis.FakeRedis()
        reporter = PerformanceReporter(r)
        stats = {
            "date": "20260318", "market": "COIN",
            "trade_count": "1", "win_count": "1", "loss_count": "0",
            "win_rate": "100.0", "net_pnl": "1234.56",
            "profit_factor": "999.0", "avg_rr": "0",
            "best_trade_symbol": "KRW-BTC", "best_trade_pnl": "1234.56",
            "worst_trade_symbol": "KRW-BTC", "worst_trade_pnl": "1234.56",
            "max_drawdown": "0",
        }
        msg = reporter.format_report("COIN", stats)
        assert "1234.56 원" in msg
