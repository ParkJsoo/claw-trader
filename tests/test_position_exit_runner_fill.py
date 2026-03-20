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
    # Store a matching SELL order_meta + reverse-lookup key (set by _place_sell)
    r.hset("claw:order_meta:KR:order-sell-001", mapping={
        "symbol": "005930", "side": "SELL", "qty": "10", "limit_price": "72000",
        "exit_reason": "take_profit", "first_seen_ts": "1000000", "source": "exit_runner",
    })
    r.set("claw:exit_order:KR:005930", "order-sell-001")

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


# ---------------------------------------------------------------------------
# _check_exit — dynamic pct
# ---------------------------------------------------------------------------

from app.position_exit_runner import _check_exit, _STOP_LOSS_PCT, _TAKE_PROFIT_PCT


def test_check_exit_uses_dynamic_stop_pct():
    """pos hash의 stop_pct가 전역값보다 크면 해당 값을 사용한다."""
    avg = Decimal("100000")
    # stop_pct=0.03 → stop_price=97000; mark=97500 → no stop-loss with global 0.02 stop
    # but also no stop-loss with dynamic 0.03 stop (97500 > 97000)
    pos = {"stop_pct": "0.03", "take_pct": "0.04"}
    mark = Decimal("97500")
    result = _check_exit(avg, mark, int(time.time()), pos=pos)
    assert result is None  # 97500 > 97000, not triggered

    # mark=96900 → below dynamic stop 97000 → triggered
    mark2 = Decimal("96900")
    result2 = _check_exit(avg, mark2, int(time.time()), pos=pos)
    assert result2 is not None
    assert "stop_loss" in result2


def test_check_exit_uses_dynamic_take_pct():
    """pos hash의 take_pct가 전역값보다 크면 해당 값을 사용한다."""
    avg = Decimal("100000")
    # take_pct=0.04 → take_price=104000; global take_pct=0.02 → 102000
    pos = {"stop_pct": "0.015", "take_pct": "0.04"}
    mark = Decimal("103000")
    # with global 0.02: 103000 >= 102000 → triggered
    # with dynamic 0.04: 103000 < 104000 → not triggered
    result = _check_exit(avg, mark, int(time.time()), pos=pos)
    assert result is None  # dynamic 0.04 not reached

    mark2 = Decimal("104001")
    result2 = _check_exit(avg, mark2, int(time.time()), pos=pos)
    assert result2 is not None
    assert "take_profit" in result2


def test_check_exit_fallback_when_no_pos():
    """pos=None이면 전역 기본값을 사용한다."""
    avg = Decimal("100000")
    stop_price = avg * (1 - _STOP_LOSS_PCT)
    # mark just below global stop
    mark = stop_price - Decimal("1")
    result = _check_exit(avg, mark, int(time.time()), pos=None)
    assert result is not None
    assert "stop_loss" in result


def test_check_exit_fallback_when_pos_missing_keys():
    """pos에 stop_pct/take_pct 키가 없으면 전역 기본값 fallback."""
    avg = Decimal("100000")
    pos = {"qty": "10", "currency": "KRW"}  # no stop_pct/take_pct
    stop_price = avg * (1 - _STOP_LOSS_PCT)
    mark = stop_price - Decimal("1")
    result = _check_exit(avg, mark, int(time.time()), pos=pos)
    assert result is not None
    assert "stop_loss" in result


# ---------------------------------------------------------------------------
# _sync_positions — stop_pct/take_pct 저장 검증
# ---------------------------------------------------------------------------

def test_sync_positions_saves_signal_pct_for_new_symbol():
    """새 BUY 포지션 발견 시 claw:signal_pct 값이 position hash에 저장된다."""
    r = fakeredis.FakeRedis()
    # signal_pct 키 사전 저장 (consensus_signal_runner가 저장하는 값)
    r.hset("claw:signal_pct:KR:005930", mapping={"stop_pct": "0.0180", "take_pct": "0.0300"})

    holdings = [{"symbol": "005930", "qty": Decimal("10"), "avg_price": Decimal("70000")}]
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    pos_raw = r.hgetall("position:KR:005930")
    pos = {k.decode(): v.decode() for k, v in pos_raw.items()}
    assert pos.get("stop_pct") == "0.0180"
    assert pos.get("take_pct") == "0.0300"


def test_sync_positions_uses_default_pct_when_no_signal_pct():
    """signal_pct 키가 없으면 기본값 0.0200이 저장된다."""
    r = fakeredis.FakeRedis()
    holdings = [{"symbol": "105560", "qty": Decimal("5"), "avg_price": Decimal("50000")}]
    kis = _make_kis_mock(holdings)

    _sync_positions(r, kis, "KR")

    pos_raw = r.hgetall("position:KR:105560")
    pos = {k.decode(): v.decode() for k, v in pos_raw.items()}
    assert pos.get("stop_pct") == "0.0200"
    assert pos.get("take_pct") == "0.0200"
