from __future__ import annotations

import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.consensus_signal_runner import _run_type_b_coin
from scripts.coin_type_b_reject_report import build_report
from utils.redis_helpers import today_kst


class _FakeUpbitClient:
    def __init__(self, *, change_rate: float, trade_price: float, high_price: float, vol_krw: float):
        self._ticker = {
            "signed_change_rate": change_rate,
            "trade_price": trade_price,
            "high_price": high_price,
            "acc_trade_price_24h": vol_krw,
        }

    def get_ticker(self, symbol: str):
        return self._ticker


class _FakeAnthropic:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text="EMIT|LONG|0.82|trend intact")])


def test_type_b_rejects_overextended_ret_5m_before_ai():
    r = fakeredis.FakeRedis()
    client = _FakeUpbitClient(
        change_rate=0.10,
        trade_price=100.0,
        high_price=101.0,
        vol_krw=20_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client), \
         patch("app.consensus_signal_runner._get_live_ret_5m", return_value=(0.031, 100.0)):
        result = _run_type_b_coin("KRW-TEST", r, "20260420")

    assert result is None
    stats = r.hgetall(f"consensus:type_b:stats:COIN:{today_kst()}")
    assert stats[b"scanned"] == b"1"
    assert stats[b"reject_ret_5m_overextended"] == b"1"


def test_type_b_rejects_missing_orderbook_confirmation():
    r = fakeredis.FakeRedis()
    client = _FakeUpbitClient(
        change_rate=0.09,
        trade_price=100.0,
        high_price=100.5,
        vol_krw=20_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client), \
         patch("app.consensus_signal_runner._get_live_ret_5m", return_value=(0.015, 100.0)):
        result = _run_type_b_coin("KRW-TEST", r, "20260420")

    assert result is None
    stats = r.hgetall(f"consensus:type_b:stats:COIN:{today_kst()}")
    assert stats[b"reject_ob_ratio_missing"] == b"1"


def test_type_b_rejects_low_change_rate_and_reports_bottleneck():
    r = fakeredis.FakeRedis()
    client = _FakeUpbitClient(
        change_rate=0.03,
        trade_price=100.0,
        high_price=100.5,
        vol_krw=20_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client):
        result = _run_type_b_coin("KRW-TEST", r, "20260420")

    assert result is None

    report = build_report(r, date_from=today_kst(), date_to=today_kst())
    assert report["summary"]["scanned"] == 1
    assert report["summary"]["reject_total"] == 1
    assert report["summary"]["top_rejects"][0]["reason"] == "reject_change_rate_weak"


def test_type_b_allows_when_not_overextended_and_orderbook_is_strong():
    r = fakeredis.FakeRedis()
    r.hset("orderbook:COIN:KRW-TEST", mapping={"ob_ratio": "1.12"})
    client = _FakeUpbitClient(
        change_rate=0.08,
        trade_price=100.0,
        high_price=101.0,
        vol_krw=20_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client), \
         patch("app.consensus_signal_runner._get_live_ret_5m", return_value=(0.012, 100.0)), \
         patch("app.consensus_signal_runner._get_anthropic_client", return_value=_FakeAnthropic()), \
         patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("30000")), \
         patch("ai.providers.base.build_type_b_prompt", return_value="prompt"), \
         patch("ai.providers.base.parse_decision_response", return_value=(True, "LONG", 0.82, "trend intact")):
        result = _run_type_b_coin("KRW-TEST", r, "20260420")

    assert result is not None
    assert result["signal_family"] == "type_b"
    assert result["symbol"] == "KRW-TEST"


def test_type_b_shadow_saves_snapshot_without_queue():
    r = fakeredis.FakeRedis()
    r.hset("claw:signal_mode:COIN", mapping={"type_b": "shadow"})
    r.hset("orderbook:COIN:KRW-TEST", mapping={"ob_ratio": "1.12"})
    client = _FakeUpbitClient(
        change_rate=0.08,
        trade_price=100.0,
        high_price=101.0,
        vol_krw=20_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client), \
         patch("app.consensus_signal_runner._get_live_ret_5m", return_value=(0.012, 100.0)), \
         patch("app.consensus_signal_runner._get_anthropic_client", return_value=_FakeAnthropic()), \
         patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("30000")), \
         patch("ai.providers.base.build_type_b_prompt", return_value="prompt"), \
         patch("ai.providers.base.parse_decision_response", return_value=(True, "LONG", 0.82, "trend intact")):
        result = _run_type_b_coin("KRW-TEST", r, "20260420")

    assert result is not None
    assert result["status"] == "shadow_candidate"
    assert r.llen("claw:signal:queue") == 0
    assert r.exists(f"research:signal:COIN:{result['signal_id']}") == 1
    stats = r.hgetall(f"consensus:type_b:stats:COIN:{today_kst()}")
    assert stats[b"scanned"] == b"1"
    assert stats[b"shadow_candidate"] == b"1"
