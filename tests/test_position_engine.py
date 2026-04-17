from __future__ import annotations

import os
import sys
from decimal import Decimal

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from domain.models import FillEvent, OrderSide
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
