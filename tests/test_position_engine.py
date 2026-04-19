from __future__ import annotations

import os
import sys
from decimal import Decimal

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from domain.models import FillEvent, OrderSide
from app.coin_research import compute_trade_summary, evaluate_resume_readiness, save_signal_snapshot
from portfolio.engine import PositionEngine
from portfolio.redis_repo import RedisPositionRepository


def _fill(
    *,
    order_id: str,
    symbol: str,
    side: OrderSide,
    qty: str,
    price: str,
    exec_id: str,
    fee: str = "0",
) -> FillEvent:
    return FillEvent(
        order_id=order_id,
        market="COIN",
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        exec_id=exec_id,
        ts="1760000000000",
        fee=Decimal(fee),
        source="test",
    )


def test_coin_position_uses_krw_currency():
    r = fakeredis.FakeRedis()
    repo = RedisPositionRepository(r)
    engine = PositionEngine(repo)

    engine.apply_fill(
        _fill(
            order_id="buy-1",
            symbol="KRW-BTC",
            side=OrderSide.BUY,
            qty="0.01",
            price="100000000",
            exec_id="coin_fill_buy-1",
        )
    )

    pos = repo.get_position("COIN", "KRW-BTC")
    assert pos is not None
    assert pos.currency == "KRW"


def test_duplicate_coin_sell_is_skipped_without_dlq():
    r = fakeredis.FakeRedis()
    repo = RedisPositionRepository(r)
    engine = PositionEngine(repo)

    engine.apply_fill(
        _fill(
            order_id="buy-1",
            symbol="KRW-OPEN",
            side=OrderSide.BUY,
            qty="10",
            price="100",
            exec_id="coin_fill_buy-1",
        )
    )

    realized = engine.apply_fill(
        _fill(
            order_id="sell-1",
            symbol="KRW-OPEN",
            side=OrderSide.SELL,
            qty="10",
            price="110",
            exec_id="coin_fill_sell-1",
            fee="1",
        )
    )
    duplicate = engine.apply_fill(
        _fill(
            order_id="sell-1",
            symbol="KRW-OPEN",
            side=OrderSide.SELL,
            qty="10",
            price="110",
            exec_id="coin_fill_sell-1",
            fee="1",
        )
    )

    assert realized == Decimal("99")
    assert duplicate is None
    assert repo.get_position("COIN", "KRW-OPEN") is None
    assert repo.get_pnl("COIN")[0] == Decimal("99")
    assert r.llen("claw:fill:dlq") == 0


def test_trade_symbols_set_is_updated_when_fill_is_applied():
    r = fakeredis.FakeRedis()
    repo = RedisPositionRepository(r)
    engine = PositionEngine(repo)

    engine.apply_fill(
        _fill(
            order_id="buy-1",
            symbol="KRW-CARV",
            side=OrderSide.BUY,
            qty="10",
            price="100",
            exec_id="coin_fill_buy-1",
        )
    )
    engine.apply_fill(
        _fill(
            order_id="sell-1",
            symbol="KRW-CARV",
            side=OrderSide.SELL,
            qty="10",
            price="95",
            exec_id="coin_fill_sell-1",
        )
    )

    assert r.sismember("trade_symbols:COIN", "KRW-CARV")


def test_coin_sell_inherits_signal_id_and_records_research_trade():
    r = fakeredis.FakeRedis()
    repo = RedisPositionRepository(r)
    engine = PositionEngine(repo)

    save_signal_snapshot(
        r,
        {
            "signal_id": "sig-type-b-1",
            "ts": "2026-04-19T10:00:00+09:00",
            "market": "COIN",
            "symbol": "KRW-API3",
            "direction": "LONG",
            "entry": {"price": "500", "size_cash": "30000"},
            "stop": {"price": "485"},
            "source": "consensus_signal_runner_type_b",
            "strategy": "trend_riding",
            "claude_conf": "0.85",
            "ret_5m": 0.012,
            "change_rate_daily": 0.08,
            "vol_24h": 12000000000,
            "ob_ratio": 1.3,
            "stop_pct": "0.03",
            "take_pct": "0.15",
        },
    )

    buy_fill = FillEvent(
        order_id="buy-1",
        market="COIN",
        symbol="KRW-API3",
        side=OrderSide.BUY,
        qty=Decimal("10"),
        price=Decimal("500"),
        exec_id="coin_fill_buy-1",
        ts="1760000000000",
        signal_id="sig-type-b-1",
        fee=Decimal("0"),
        source="test",
    )
    engine.apply_fill(buy_fill)

    pos_raw = r.hgetall("position:COIN:KRW-API3")
    assert pos_raw[b"signal_id"] == b"sig-type-b-1"
    assert pos_raw[b"entry_signal_family"] == b"type_b"

    r.hset("claw:order_meta:COIN:sell-1", mapping={"exit_reason": "take_profit"})

    sell_fill = FillEvent(
        order_id="sell-1",
        market="COIN",
        symbol="KRW-API3",
        side=OrderSide.SELL,
        qty=Decimal("10"),
        price=Decimal("550"),
        exec_id="coin_fill_sell-1",
        ts="1760000600000",
        fee=Decimal("1"),
        source="watcher",
    )
    realized = engine.apply_fill(sell_fill)

    assert realized == Decimal("499")
    trade = repo.get_recent_trades("COIN", "KRW-API3", limit=1)[0]
    assert trade["signal_id"] == "sig-type-b-1"

    research = r.hgetall("research:trade:COIN:coin_fill_sell-1")
    assert research[b"signal_family"] == b"type_b"
    assert research[b"entry_strategy"] == b"trend_riding"
    assert research[b"exit_reason"] == b"take_profit"

    summary = compute_trade_summary(r)
    assert summary["overall"]["trade_count"] == 1
    assert summary["overall"]["net_pnl"] == 499.0
    assert summary["by_signal_family"]["type_b"]["trade_count"] == 1


def test_resume_readiness_requires_proven_type_b_edge():
    summary = {
        "overall": {
            "trade_count": 35,
            "win_rate": 31.4,
            "net_pnl": 4200.0,
            "profit_factor": 1.32,
            "avg_pnl": 120.0,
        },
        "by_signal_family": {
            "type_b": {
                "trade_count": 28,
                "win_rate": 32.1,
                "net_pnl": 3900.0,
                "profit_factor": 1.41,
                "avg_pnl": 139.3,
            },
            "type_a": {
                "trade_count": 7,
                "win_rate": 14.3,
                "net_pnl": -300.0,
                "profit_factor": 0.61,
                "avg_pnl": -42.8,
            },
        },
    }

    result = evaluate_resume_readiness(summary)
    assert result["ready_to_resume"] is True
    assert result["recommendation"] == "resume_candidate_type_b_only"
    assert result["evaluations"]["type_b"]["ready"] is True
    assert result["evaluations"]["type_a"]["ready"] is False


def test_resume_readiness_keeps_paused_when_sample_is_weak():
    summary = {
        "overall": {
            "trade_count": 9,
            "win_rate": 22.2,
            "net_pnl": -2500.0,
            "profit_factor": 0.41,
            "avg_pnl": -277.7,
        },
        "by_signal_family": {
            "type_b": {
                "trade_count": 9,
                "win_rate": 22.2,
                "net_pnl": -2500.0,
                "profit_factor": 0.41,
                "avg_pnl": -277.7,
            },
            "type_a": {
                "trade_count": 0,
                "win_rate": 0.0,
                "net_pnl": 0.0,
                "profit_factor": 0.0,
                "avg_pnl": 0.0,
            },
        },
    }

    result = evaluate_resume_readiness(summary)
    assert result["ready_to_resume"] is False
    assert result["recommendation"] == "keep_paused"
    assert "min_trades" in result["evaluations"]["overall"]["blockers"]
    assert "profit_factor" in result["evaluations"]["type_b"]["blockers"]
