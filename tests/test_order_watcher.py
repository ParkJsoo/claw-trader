from __future__ import annotations

import os
import sys

import fakeredis
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.order_watcher import OrderWatcher, WatcherConfig


def _make_watcher(r) -> OrderWatcher:
    watcher = OrderWatcher.__new__(OrderWatcher)
    watcher.cfg = WatcherConfig(redis_url="redis://test")
    watcher.r = r
    watcher.ibkr = None
    watcher.kis = None
    watcher.upbit = None
    watcher.position_engine = None
    return watcher


def test_reconcile_kr_buy_in_holdings_marks_filled():
    r = fakeredis.FakeRedis()
    r.hset("claw:order_meta:KR:order-1", mapping={
        "symbol": "005930",
        "side": "BUY",
        "qty": "1",
        "limit_price": "70000",
        "first_seen_ts": "1776733000",
    })
    r.hset("position:KR:005930", "qty", "1")
    watcher = _make_watcher(r)

    assert watcher._reconcile_kr_submitted_order("order-1", age_sec=120) == "FILLED"


def test_reconcile_kr_buy_with_later_sell_meta_marks_filled():
    r = fakeredis.FakeRedis()
    r.hset("claw:order_meta:KR:buy-1", mapping={
        "symbol": "005930",
        "side": "BUY",
        "qty": "1",
        "limit_price": "70000",
        "first_seen_ts": "1776733000",
    })
    r.hset("claw:order_meta:KR:sell-1", mapping={
        "symbol": "005930",
        "side": "SELL",
        "qty": "1",
        "limit_price": "71000",
        "first_seen_ts": "1776733600",
    })
    watcher = _make_watcher(r)

    assert watcher._reconcile_kr_submitted_order("buy-1", age_sec=120) == "FILLED"


def test_reconcile_kr_sell_not_in_holdings_marks_filled():
    r = fakeredis.FakeRedis()
    r.hset("claw:order_meta:KR:sell-1", mapping={
        "symbol": "005930",
        "side": "SELL",
        "qty": "1",
        "limit_price": "71000",
        "first_seen_ts": "1776733600",
    })
    watcher = _make_watcher(r)

    assert watcher._reconcile_kr_submitted_order("sell-1", age_sec=120) == "FILLED"


def test_reconcile_kr_meta_missing_old_order_marks_canceled(monkeypatch):
    monkeypatch.setattr("app.order_watcher._KR_GHOST_ORDER_STALE_SEC", 10)
    r = fakeredis.FakeRedis()
    watcher = _make_watcher(r)

    assert watcher._reconcile_kr_submitted_order("ghost-1", age_sec=11) == "CANCELED"


def test_reconcile_kr_meta_missing_recent_order_stays_none(monkeypatch):
    monkeypatch.setattr("app.order_watcher._KR_GHOST_ORDER_STALE_SEC", 10)
    r = fakeredis.FakeRedis()
    watcher = _make_watcher(r)

    assert watcher._reconcile_kr_submitted_order("ghost-1", age_sec=9) is None


def test_reconcile_kr_incomplete_meta_old_order_marks_canceled(monkeypatch):
    monkeypatch.setattr("app.order_watcher._KR_GHOST_ORDER_STALE_SEC", 10)
    r = fakeredis.FakeRedis()
    r.hset("claw:order_meta:KR:ghost-2", mapping={"first_seen_ts": "1776648800"})
    watcher = _make_watcher(r)

    assert watcher._reconcile_kr_submitted_order("ghost-2", age_sec=11) == "CANCELED"
