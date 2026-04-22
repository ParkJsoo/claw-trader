from __future__ import annotations

import os
import sys
from decimal import Decimal

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.coin_research import choose_resume_summary, save_pre_consensus_signal_snapshot, save_signal_snapshot
from app.coin_shadow import (
    compute_combined_shadow_summary,
    compute_pre_consensus_shadow_summary,
    compute_shadow_summary,
    evaluate_pending_pre_consensus_signals,
    evaluate_pending_signals,
)


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
            "high_price": 102,
            "near_high": 0.9804,
            "vol_24h": 12000000000,
            "ob_ratio": 1.4,
            "stop_pct": "0.03",
            "take_pct": "0.15",
            "shadow_origin": "consensus_runner_type_b_shadow_candidate",
            "shadow_stage": "post_consensus",
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
    assert row[b"entry_high_price"] == b"102"
    assert row[b"entry_near_high"] == b"0.9804"

    summary = compute_shadow_summary(r)
    assert summary["overall"]["trade_count"] == 1
    assert summary["by_signal_family"]["type_b"]["trade_count"] == 1
    assert summary["by_shadow_origin"]["consensus_runner_type_b_shadow_candidate"]["trade_count"] == 1


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


def test_pre_consensus_shadow_is_evaluated_in_separate_ledger():
    r = fakeredis.FakeRedis()

    save_pre_consensus_signal_snapshot(
        r,
        {
            "signal_id": "pre:KRW-ALT:1760007200000",
            "ts": "1760007200000",
            "market": "COIN",
            "symbol": "KRW-ALT",
            "direction": "LONG",
            "entry": {"price": "100", "size_cash": "100000"},
            "stop": {"price": "97"},
            "source": "consensus_signal_runner_pre_consensus",
            "status": "shadow_candidate",
            "strategy": "momentum_breakout",
            "signal_family": "type_a",
            "ret_5m": 0.018,
            "range_5m": 0.012,
            "ret_1m": 0.003,
            "vol_24h": 80000000000,
            "ob_ratio": 1.1,
            "stop_pct": "0.03",
            "take_pct": "0.15",
            "reject_reason": "reject_volume_no_surge",
            "shadow_stage": "pre_consensus",
            "shadow_origin": "consensus_runner_reject",
        },
    )
    _seed_mark_hist(
        r,
        "KRW-ALT",
        [
            (1760007600000, "101"),
            (1760007900000, "102"),
            (1760008200000, "104"),
            (1760008500000, "108"),
            (1760008800000, "112"),
            (1760009100000, "116"),
            (1760009400000, "118"),
            (1760009700000, "117"),
            (1760010000000, "115"),
            (1760010300000, "114"),
        ],
    )

    stats = evaluate_pending_pre_consensus_signals(r)

    assert stats["completed"] == 1
    row = r.hgetall("research:pre_shadow:COIN:pre:KRW-ALT:1760007200000")
    assert row[b"signal_id"] == b"pre:KRW-ALT:1760007200000"

    pre_summary = compute_pre_consensus_shadow_summary(r)
    combined_summary = compute_combined_shadow_summary(r)
    assert pre_summary["overall"]["trade_count"] == 1
    assert combined_summary["overall"]["trade_count"] == 1
    assert pre_summary["by_signal_family"]["type_a"]["trade_count"] == 1
    assert pre_summary["by_symbol"]["KRW-ALT"]["trade_count"] == 1
    assert pre_summary["by_reject_reason"]["reject_volume_no_surge"]["trade_count"] == 1
    assert pre_summary["by_shadow_origin"]["consensus_runner_reject"]["trade_count"] == 1


def test_shadow_summary_backfills_shadow_origin_from_snapshot():
    r = fakeredis.FakeRedis()

    save_signal_snapshot(
        r,
        {
            "signal_id": "sig-shadow-backfill",
            "ts": "1760000000000",
            "market": "COIN",
            "symbol": "KRW-BACKFILL",
            "direction": "LONG",
            "entry": {"price": "100", "size_cash": "30000"},
            "stop": {"price": "97"},
            "source": "consensus_signal_runner_type_b",
            "strategy": "trend_riding",
            "ret_5m": 0.02,
            "change_rate_daily": 0.09,
            "high_price": 104,
            "near_high": 0.9615,
            "shadow_origin": "consensus_runner_type_b_shadow_candidate",
            "shadow_stage": "post_consensus",
            "stop_pct": "0.03",
            "take_pct": "0.15",
        },
    )
    r.hset(
        "research:shadow:COIN:sig-shadow-backfill",
        mapping={
            "signal_id": "sig-shadow-backfill",
            "signal_ts_ms": "1760000000000",
            "symbol": "KRW-BACKFILL",
            "signal_family": "type_b",
            "entry_strategy": "trend_riding",
            "realized_pnl": "-100",
            "hold_sec": "60",
            "exit_reason": "stop_loss",
        },
    )
    r.zadd("research:shadow_index:COIN", {"sig-shadow-backfill": 1760000000000})

    summary = compute_shadow_summary(r)

    assert summary["by_shadow_origin"]["consensus_runner_type_b_shadow_candidate"]["trade_count"] == 1


def test_shadow_summary_infers_type_b_shadow_origin_from_entry_source():
    r = fakeredis.FakeRedis()
    r.hset(
        "research:shadow:COIN:sig-shadow-infer",
        mapping={
            "signal_id": "sig-shadow-infer",
            "signal_ts_ms": "1760000000000",
            "symbol": "KRW-INFER",
            "signal_family": "type_b",
            "entry_strategy": "trend_riding",
            "entry_source": "consensus_signal_runner_type_b",
            "realized_pnl": "-100",
            "hold_sec": "60",
            "exit_reason": "stop_loss",
        },
    )
    r.zadd("research:shadow_index:COIN", {"sig-shadow-infer": 1760000000000})

    summary = compute_shadow_summary(r)

    assert summary["by_shadow_origin"]["consensus_runner_type_b_shadow_candidate"]["trade_count"] == 1


def test_choose_resume_summary_prefers_shadow_when_live_sample_is_empty():
    trade_summary = {"overall": {"trade_count": 0}}
    shadow_summary = {"overall": {"trade_count": 12, "net_pnl": 1000}}

    selected = choose_resume_summary(trade_summary, shadow_summary, "auto")

    assert selected["selected_ledger"] == "shadow"
    assert selected["summary"] == shadow_summary


def test_choose_resume_summary_can_select_combined_shadow():
    trade_summary = {"overall": {"trade_count": 0}}
    shadow_summary = {"overall": {"trade_count": 2, "net_pnl": -50}}
    shadow_pre_summary = {"overall": {"trade_count": 8, "net_pnl": 120}}
    shadow_all_summary = {"overall": {"trade_count": 10, "net_pnl": 70}}

    selected = choose_resume_summary(
        trade_summary,
        shadow_summary,
        "shadow_all",
        shadow_pre_summary=shadow_pre_summary,
        shadow_all_summary=shadow_all_summary,
    )

    assert selected["selected_ledger"] == "shadow_all"
    assert selected["selected_sample_count"] == 10
    assert selected["summary"] == shadow_all_summary
