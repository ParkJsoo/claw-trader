from __future__ import annotations

import os
import sys
from datetime import datetime

import fakeredis
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.coin_shadow_runner import (
    _build_type_b_runtime_watch_alerts,
    _build_type_b_shadow_alerts,
    _maybe_notify_type_b_runtime_watch,
    _maybe_notify_type_b_shadow_progress,
)

_KST = ZoneInfo("Asia/Seoul")


def _summary_with_type_b(trade_count: int, *, win_rate: float, net_pnl: float, profit_factor: float, avg_pnl: float) -> dict:
    return {
        "overall": {},
        "by_signal_family": {
            "type_b": {
                "trade_count": trade_count,
                "win_rate": win_rate,
                "net_pnl": net_pnl,
                "profit_factor": profit_factor,
                "avg_pnl": avg_pnl,
            }
        },
    }


def test_build_type_b_shadow_alerts_includes_count_milestones(monkeypatch):
    monkeypatch.setenv("COIN_TYPE_B_ALERT_TRADE_COUNTS", "10,20")

    alerts = _build_type_b_shadow_alerts(
        _summary_with_type_b(20, win_rate=30.0, net_pnl=1500.0, profit_factor=1.3, avg_pnl=75.0),
        today="20260421",
    )

    assert "count:10" in alerts
    assert "count:20" in alerts
    assert "Type B shadow 20 trades reached" in alerts["count:20"]


def test_build_type_b_shadow_alerts_includes_ready_when_type_b_passes():
    alerts = _build_type_b_shadow_alerts(
        _summary_with_type_b(25, win_rate=35.0, net_pnl=2500.0, profit_factor=1.4, avg_pnl=100.0),
        today="20260421",
    )

    assert "ready" in alerts
    assert "ready candidate" in alerts["ready"]


def test_maybe_notify_type_b_shadow_progress_sends_once(monkeypatch):
    r = fakeredis.FakeRedis()
    today = "20260421"
    score = int(datetime(2026, 4, 21, 12, 0, tzinfo=_KST).timestamp() * 1000)
    r.hset(
        "research:shadow:COIN:sig-1",
        mapping={
            "signal_id": "sig-1",
            "date": today,
            "signal_ts_ms": str(score),
            "signal_family": "type_b",
            "realized_pnl": "100.0",
            "hold_sec": "60",
            "exit_reason": "stagnant_exit",
        },
    )
    r.zadd("research:shadow_index:COIN", {"sig-1": score})
    monkeypatch.setenv("COIN_TYPE_B_ALERT_TRADE_COUNTS", "1")

    sent: list[str] = []
    monkeypatch.setattr("app.coin_shadow_runner.send_telegram", lambda msg: sent.append(msg) or True)

    tags1 = _maybe_notify_type_b_shadow_progress(r, today=today)
    tags2 = _maybe_notify_type_b_shadow_progress(r, today=today)

    assert tags1 == ["count:1"]
    assert tags2 == []
    assert len(sent) == 1


def test_build_type_b_runtime_watch_alerts_reports_first_candidate():
    r = fakeredis.FakeRedis()
    r.hset(
        "consensus:type_b:stats:COIN:20260422",
        mapping={
            "scanned": "120",
            "candidate": "1",
            "reject_change_rate_weak": "80",
            "reject_far_from_high": "20",
        },
    )

    alerts = _build_type_b_runtime_watch_alerts(r, today="20260422")

    assert "first_candidate" in alerts
    assert "candidate detected" in alerts["first_candidate"]
    assert "candidate=1" in alerts["first_candidate"]


def test_build_type_b_runtime_watch_alerts_reports_stuck_threshold(monkeypatch):
    r = fakeredis.FakeRedis()
    r.hset(
        "consensus:type_b:stats:COIN:20260422",
        mapping={
            "scanned": "650",
            "candidate": "0",
            "shadow_candidate": "0",
            "reject_change_rate_weak": "400",
            "reject_far_from_high": "120",
        },
    )
    monkeypatch.setenv("COIN_TYPE_B_WATCH_SCAN_THRESHOLDS", "500,1000")

    alerts = _build_type_b_runtime_watch_alerts(r, today="20260422")

    assert "stuck:500" in alerts
    assert "still blocked after 500 scans" in alerts["stuck:500"]
    assert "reject_change_rate_weak=400" in alerts["stuck:500"]
    assert "stuck:1000" not in alerts


def test_maybe_notify_type_b_runtime_watch_sends_once(monkeypatch):
    r = fakeredis.FakeRedis()
    today = "20260422"
    r.hset(
        f"consensus:type_b:stats:COIN:{today}",
        mapping={
            "scanned": "700",
            "candidate": "0",
            "shadow_candidate": "0",
            "reject_change_rate_weak": "500",
        },
    )
    monkeypatch.setenv("COIN_TYPE_B_WATCH_SCAN_THRESHOLDS", "500")

    sent: list[str] = []
    monkeypatch.setattr("app.coin_shadow_runner.send_telegram", lambda msg: sent.append(msg) or True)

    tags1 = _maybe_notify_type_b_runtime_watch(r, today=today)
    tags2 = _maybe_notify_type_b_runtime_watch(r, today=today)

    assert tags1 == ["stuck:500"]
    assert tags2 == []
    assert len(sent) == 1
