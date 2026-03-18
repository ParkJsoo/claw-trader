"""position_exit_runner KR fill detection 단위 테스트."""
from __future__ import annotations

import json
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.position_exit_runner import _push_fill_event, _sync_positions


# ---------------------------------------------------------------------------
# _push_fill_event
# ---------------------------------------------------------------------------

def test_push_fill_event_basic():
    r = fakeredis.FakeRedis()
    pushed = _push_fill_event(r, "005930", "SELL", Decimal("10"), Decimal("75000"), "order-001")
    assert pushed is True
    raw = r.rpop("claw:fill:queue")
    assert raw is not None
    data = json.loads(raw)
    assert data["symbol"] == "005930"
    assert data["side"] == "SELL"
    assert data["qty"] == "10"
    assert data["price"] == "75000"
    assert data["exec_id"] == "kr_fill_order-001"
    assert data["market"] == "KR"
    assert data["source"] == "position_exit_runner"


def test_push_fill_event_idempotent():
    r = fakeredis.FakeRedis()
    first = _push_fill_event(r, "005930", "SELL", Decimal("10"), Decimal("75000"), "order-001")
    second = _push_fill_event(r, "005930", "SELL", Decimal("10"), Decimal("75000"), "order-001")
    assert first is True
    assert second is False
    # Only one item in queue
    assert r.llen("claw:fill:queue") == 1


def test_push_fill_buy():
    r = fakeredis.FakeRedis()
    pushed = _push_fill_event(r, "105560", "BUY", Decimal("5"), Decimal("50000"), "buy-abc")
    assert pushed is True
    raw = r.rpop("claw:fill:queue")
    data = json.loads(raw)
    assert data["side"] == "BUY"
    assert data["symbol"] == "105560"


# ---------------------------------------------------------------------------
# _sync_positions — BUY fill detection
# ---------------------------------------------------------------------------

def _make_kis_mock(holdings):
    kis = MagicMock()
    kis.get_kr_holdings.return_value = holdings
    return kis


def test_sync_positions_buy_fill_on_new_symbol():
    r = fakeredis.FakeRedis()
    # No existing positions in Redis
    holdings = [{"symbol": "005930", "qty": Decimal("10"), "avg_price": Decimal("70000")}]
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    assert r.llen("claw:fill:queue") == 1
    raw = r.rpop("claw:fill:queue")
    data = json.loads(raw)
    assert data["side"] == "BUY"
    assert data["symbol"] == "005930"
    assert data["qty"] == "10"
    assert data["price"] == "70000"


def test_sync_positions_no_buy_fill_for_existing_symbol():
    r = fakeredis.FakeRedis()
    # Pre-populate Redis with existing position
    r.hset("position:KR:005930", mapping={
        "qty": "10", "avg_price": "70000", "opened_ts": "1000000", "updated_ts": "1000000", "currency": "KRW"
    })
    r.sadd("position_index:KR", "005930")

    holdings = [{"symbol": "005930", "qty": Decimal("10"), "avg_price": Decimal("70000")}]
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    # No new BUY fill — symbol was already in Redis
    assert r.llen("claw:fill:queue") == 0


# ---------------------------------------------------------------------------
# _sync_positions — SELL fill detection
# ---------------------------------------------------------------------------

def test_sync_positions_sell_fill_on_removed_symbol():
    r = fakeredis.FakeRedis()
    # Position exists in Redis but not in KIS holdings (sold)
    r.hset("position:KR:005930", mapping={
        "qty": "10", "avg_price": "70000", "opened_ts": "1000000", "updated_ts": "1000000", "currency": "KRW"
    })
    r.sadd("position_index:KR", "005930")
    # Store a matching SELL order_meta
    r.hset("claw:order_meta:KR:order-sell-001", mapping={
        "symbol": "005930", "side": "SELL", "qty": "10", "limit_price": "72000",
        "exit_reason": "take_profit", "first_seen_ts": "1000000", "source": "exit_runner",
    })

    holdings = []  # empty — position was sold
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    assert r.llen("claw:fill:queue") == 1
    raw = r.rpop("claw:fill:queue")
    data = json.loads(raw)
    assert data["side"] == "SELL"
    assert data["symbol"] == "005930"
    assert data["qty"] == "10"
    assert data["price"] == "72000"  # from order_meta limit_price
    assert "order-sell-001" in data["exec_id"]


def test_sync_positions_sell_fill_no_order_meta_uses_avg_price():
    r = fakeredis.FakeRedis()
    r.hset("position:KR:105560", mapping={
        "qty": "5", "avg_price": "50000", "opened_ts": "1000000", "updated_ts": "1000000", "currency": "KRW"
    })
    r.sadd("position_index:KR", "105560")

    holdings = []
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    assert r.llen("claw:fill:queue") == 1
    raw = r.rpop("claw:fill:queue")
    data = json.loads(raw)
    assert data["side"] == "SELL"
    assert data["symbol"] == "105560"
    assert data["price"] == "50000"  # fallback to avg_price


def test_sync_positions_sell_fill_zero_qty_no_push():
    """qty=0인 포지션이 사라지면 fill push 안 함."""
    r = fakeredis.FakeRedis()
    r.hset("position:KR:005930", mapping={
        "qty": "0", "avg_price": "70000", "opened_ts": "1000000", "updated_ts": "1000000", "currency": "KRW"
    })
    r.sadd("position_index:KR", "005930")

    holdings = []
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    assert r.llen("claw:fill:queue") == 0


def test_sync_positions_position_deleted_after_sell_fill():
    """SELL fill push 후 Redis 포지션이 삭제되어야 함."""
    r = fakeredis.FakeRedis()
    r.hset("position:KR:005930", mapping={
        "qty": "10", "avg_price": "70000", "opened_ts": "1000000", "updated_ts": "1000000", "currency": "KRW"
    })
    r.sadd("position_index:KR", "005930")

    holdings = []
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    assert not r.exists("position:KR:005930")
    assert not r.sismember("position_index:KR", "005930")
