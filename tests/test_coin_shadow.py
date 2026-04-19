from __future__ import annotations

import os
import sys
from decimal import Decimal

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.coin_research import choose_resume_summary, save_signal_snapshot
from app.coin_shadow import compute_shadow_summary, evaluate_pending_signals


def _seed_mark_hist(r, symbol: str, prices: list[tuple[int, str]]) -> None:
    key = f"mark_hist:COIN:{symbol}"
    for ts_ms, price in prices:
        r.rpush(key, f"{ts_ms}:{price}")


def test_shadow_evaluation_records_take_profit_result():
    r = fakeredis.FakeRedis()

    save_signal_snapshot(
        r,
        {
            "signal_id": "sig-shadow-1",
            "ts": "1760000000000",
            "market": "COIN",
            "symbol": "KRW-TEST",
            "direction": "LONG",
            "entry": {"price": "100", "size_cash": "30000"},
            "stop": {"price": "97"},
            "source": "consensus_signal_runner_type_b",
            "strategy": "trend_riding",
            "ret_5m": 0.012,
            "change_rate_daily": 0.08,
            "vol_24h": 12000000000,
            "ob_ratio": 1.4,
            "stop_pct": "0.03",
            "take_pct": "0.15",
        },
    )
    _seed_mark_hist(
        r,
        "KRW-TEST",
        [
            (1760000400000, "101"),
            (1760000700000, "105"),
            (1760001000000, "110"),
            (1760001300000, "112"),
            (1760001600000, "115"),
            (1760001900000, "116"),
            (1760002200000, "117"),
            (1760002500000, "118"),
            (1760002800000, "116"),
            (1760003100000, "115"),
        ],
    )

    stats = evaluate_pending_signals(r)

    assert stats["completed"] == 1
    row = r.hgetall("research:shadow:COIN:sig-shadow-1")
    assert row[b"exit_reason"] == b"take_profit"
    assert Decimal(row[b"realized_pnl"].decode()) > Decimal("0")

    summary = compute_shadow_summary(r)
    assert summary["overall"]["trade_count"] == 1
    assert summary["by_signal_family"]["type_b"]["trade_count"] == 1


def test_shadow_evaluation_waits_for_more_history_when_signal_is_still_open():
    r = fakeredis.FakeRedis()

    save_signal_snapshot(
        r,
        {
            "signal_id": "sig-shadow-2",
            "ts": "1760003600000",
            "market": "COIN",
            "symbol": "KRW-FLAT",
            "direction": "LONG",
            "entry": {"price": "100", "size_cash": "30000"},
            "stop": {"price": "97"},
            "source": "consensus_signal_runner",
            "strategy": "momentum_breakout",
            "ret_5m": 0.022,
            "range_5m": 0.01,
            "stop_pct": "0.03",
            "take_pct": "0.15",
        },
    )
    _seed_mark_hist(
        r,
        "KRW-FLAT",
        [
            (1760004000000, "100.4"),
            (1760004300000, "100.8"),
            (1760004600000, "101.1"),
            (1760004900000, "100.9"),
            (1760005200000, "101.0"),
            (1760005500000, "100.8"),
            (1760005800000, "100.9"),
            (1760006100000, "100.7"),
            (1760006400000, "100.8"),
            (1760006700000, "100.9"),
        ],
    )

    stats = evaluate_pending_signals(r)

    assert stats["pending"] == 1
    assert not r.exists("research:shadow:COIN:sig-shadow-2")


def test_choose_resume_summary_prefers_shadow_when_live_sample_is_empty():
    trade_summary = {"overall": {"trade_count": 0}}
    shadow_summary = {"overall": {"trade_count": 12, "net_pnl": 1000}}

    selected = choose_resume_summary(trade_summary, shadow_summary, "auto")

    assert selected["selected_ledger"] == "shadow"
    assert selected["summary"] == shadow_summary
