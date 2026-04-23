from __future__ import annotations

import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import fakeredis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.consensus_signal_runner import _run_type_b_coin
from scripts.coin_type_b_reject_report import build_report
from scripts.coin_type_b_whatif_report import build_report as build_whatif_report
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
    stats = r.hgetall(f"consensus:type_b:stats:COIN:{today_kst()}")
    assert stats[b"reject_change_rate_weak"] == b"1"
    sample_key = f"consensus:type_b:reject_samples:COIN:{today_kst()}:reject_change_rate_weak"
    sample = r.lindex(sample_key, 0)
    assert sample is not None
    parsed = __import__("json").loads(sample.decode())
    assert parsed["symbol"] == "KRW-TEST"
    assert round(parsed["change_rate"] * 100, 1) == 3.0
    assert report["summary"]["sample_insights"]["reject_change_rate_weak"]["sample_count"] == 1
    assert report["summary"]["sample_insights"]["reject_change_rate_weak"]["change_rate_pct"]["avg"] == 3.0
    threshold = report["summary"]["sample_insights"]["reject_change_rate_weak"]["threshold_context"]
    assert threshold["comparison"] == "min"
    assert threshold["threshold"] == 4.0
    assert threshold["avg_gap"] == 1.0
    assert threshold["near_threshold"]["count"] == 1
    scan_key = f"consensus:type_b:scan_samples:COIN:{today_kst()}"
    scan_row = r.lindex(scan_key, 0)
    assert scan_row is not None
    scan = __import__("json").loads(scan_row.decode())
    assert scan["status"] == "reject"
    assert scan["reason_code"] == "reject_change_rate_weak"
    assert round(scan["near_high"], 3) == 0.995


def test_type_b_reject_report_uses_consensus_log_fallback_for_samples(tmp_path: Path):
    r = fakeredis.FakeRedis()
    today = today_kst()
    r.hset(
        f"consensus:type_b:stats:COIN:{today}",
        mapping={
            "scanned": "3",
            "reject_far_from_high": "2",
            "reject_change_rate_overextended": "1",
        },
    )
    log_file = tmp_path / "consensus_signal.log"
    log_file.write_text(
        "\n".join(
            [
                f"[{today[:4]}-{today[4:6]}-{today[6:8]} 00:00:01] consensus: type_b.reject.far_from_high symbol=KRW-AAA change_rate=8.5% near_high=0.783",
                f"[{today[:4]}-{today[4:6]}-{today[6:8]} 00:00:02] consensus: type_b.reject.far_from_high symbol=KRW-BBB change_rate=7.5% near_high=0.812",
                f"[{today[:4]}-{today[4:6]}-{today[6:8]} 00:00:03] consensus: type_b.reject.change_rate_overextended symbol=KRW-CCC change_rate=18.0%",
            ]
        ),
        encoding="utf-8",
    )

    report = build_report(r, date_from=today, date_to=today, log_path=str(log_file))

    far_from_high = report["summary"]["sample_insights"]["reject_far_from_high"]
    assert far_from_high["source"] == "consensus_log"
    assert far_from_high["sample_count"] == 2
    assert far_from_high["change_rate_pct"]["avg"] == 8.0
    assert far_from_high["near_high_ratio"]["min"] == 0.783
    assert far_from_high["threshold_context"]["threshold"] == 0.97
    assert far_from_high["threshold_context"]["avg_gap"] == 0.172


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
    scan_key = f"consensus:type_b:scan_samples:COIN:{today_kst()}"
    scan_row = r.lindex(scan_key, 0)
    assert scan_row is not None
    scan = __import__("json").loads(scan_row.decode())
    assert scan["status"] == "shadow_candidate"
    assert round(scan["ret_5m"] * 100, 1) == 1.2


def test_type_b_reject_can_seed_alt_shadow_candidates_once_per_profile():
    r = fakeredis.FakeRedis()
    client = _FakeUpbitClient(
        change_rate=0.022,
        trade_price=100.0,
        high_price=108.7,
        vol_krw=4_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client), \
         patch("app.consensus_signal_runner._get_live_ret_5m", return_value=(0.003, 100.0)):
        result1 = _run_type_b_coin("KRW-ALT", r, "20260420")
        result2 = _run_type_b_coin("KRW-ALT", r, "20260420")

    assert result1 is None
    assert result2 is None

    signal_ids = [raw.decode() for raw in r.zrange("research:signal_index:COIN", 0, -1)]
    assert len(signal_ids) == 2

    rows = [r.hgetall(f"research:signal:COIN:{signal_id}") for signal_id in signal_ids]
    origins = sorted(row[b"shadow_origin"].decode() for row in rows)
    assert origins == [
        "consensus_runner_type_b_alt_shadow:alt_broad_trend_positive_5m",
        "consensus_runner_type_b_alt_shadow:alt_pullback_setup_allow_small_dip",
    ]
    assert all(row[b"status"] == b"shadow_candidate" for row in rows)
    assert all(row[b"source"] == b"consensus_signal_runner_type_b_alt_shadow" for row in rows)
    assert all(row[b"reject_reason"] == b"reject_change_rate_weak" for row in rows)


def test_type_b_alt_pullback_canary_enqueues_only_when_explicitly_live(monkeypatch):
    monkeypatch.setattr("app.consensus_signal_runner._COIN_ALT_CANARY_DAILY_CAP", 1)
    monkeypatch.setattr("app.consensus_signal_runner._COIN_ALT_CANARY_SIZE_CASH", Decimal("10000"))
    r = fakeredis.FakeRedis()
    r.hset("claw:signal_mode:COIN", mapping={"type_b_alt_pullback": "live"})
    client = _FakeUpbitClient(
        change_rate=0.022,
        trade_price=100.0,
        high_price=108.7,
        vol_krw=4_000_000_000.0,
    )

    with patch("app.consensus_signal_runner._get_client", return_value=client), \
         patch("app.consensus_signal_runner._get_live_ret_5m", return_value=(0.003, 100.0)):
        result = _run_type_b_coin("KRW-ALT", r, "20260420")

    assert result is None
    assert r.llen("claw:signal:queue") == 1
    queued = __import__("json").loads(r.lindex("claw:signal:queue", 0).decode())
    assert queued["status"] == "candidate"
    assert queued["source"] == "consensus_signal_runner_type_b_alt_canary"
    assert queued["signal_family"] == "type_b_alt_pullback"
    assert queued["canary_profile"] == "alt_pullback_setup_allow_small_dip"
    assert queued["entry"]["size_cash"] == "10000"
    assert r.get(f"consensus:coin_alt_canary_daily:COIN:alt_pullback_setup_allow_small_dip:{today_kst()}") == b"1"


def test_type_b_whatif_report_compares_relaxed_gate_profiles():
    r = fakeredis.FakeRedis()
    today = today_kst()
    key = f"consensus:type_b:scan_samples:COIN:{today}"
    r.rpush(
        key,
        __import__("json").dumps(
            {
                "symbol": "KRW-A",
                "status": "reject",
                "reason_code": "reject_change_rate_weak",
                "change_rate": 0.03,
                "near_high": 0.985,
                "ret_5m": 0.010,
                "trade_price": 100.0,
                "high_price": 101.5,
                "vol_24h": 20_000_000_000.0,
                "ob_ratio": 1.10,
            }
        ),
        __import__("json").dumps(
            {
                "symbol": "KRW-B",
                "status": "reject",
                "reason_code": "reject_far_from_high",
                "change_rate": 0.08,
                "near_high": 0.945,
                "ret_5m": 0.012,
                "trade_price": 100.0,
                "high_price": 105.8,
                "vol_24h": 20_000_000_000.0,
                "ob_ratio": 1.10,
            }
        ),
        __import__("json").dumps(
            {
                "symbol": "KRW-C",
                "status": "reject",
                "reason_code": "reject_low_vol_24h",
                "change_rate": 0.07,
                "near_high": 0.982,
                "ret_5m": 0.011,
                "trade_price": 100.0,
                "high_price": 101.8,
                "vol_24h": 8_000_000_000.0,
                "ob_ratio": 1.10,
            }
        ),
        __import__("json").dumps(
            {
                "symbol": "KRW-D",
                "status": "candidate",
                "change_rate": 0.09,
                "near_high": 0.985,
                "ret_5m": 0.014,
                "trade_price": 100.0,
                "high_price": 101.5,
                "vol_24h": 25_000_000_000.0,
                "ob_ratio": 1.12,
            }
        ),
        __import__("json").dumps(
            {
                "symbol": "KRW-E",
                "status": "reject",
                "reason_code": "reject_far_from_high",
                "change_rate": 0.022,
                "near_high": 0.92,
                "ret_5m": 0.003,
                "trade_price": 100.0,
                "high_price": 108.7,
                "vol_24h": 4_000_000_000.0,
                "ob_ratio": 0.98,
            }
        ),
        __import__("json").dumps(
            {
                "symbol": "KRW-F",
                "status": "reject",
                "reason_code": "reject_ret_5m_weak",
                "change_rate": 0.018,
                "near_high": 0.89,
                "ret_5m": -0.001,
                "trade_price": 100.0,
                "high_price": 112.4,
                "vol_24h": 3_500_000_000.0,
                "ob_ratio": None,
            }
        ),
    )

    report = build_whatif_report(r, date_from=today, date_to=today)
    scenarios = {row["name"]: row for row in report["summary"]["scenario_reports"]}

    assert report["summary"]["scan_sample_count"] == 6
    assert scenarios["baseline_current"]["pre_ai_pass_count"] == 1
    assert scenarios["baseline_current"]["pass_symbols"] == ["KRW-D"]
    assert scenarios["relax_change_rate_3pct"]["pre_ai_pass_count"] == 2
    assert scenarios["relax_change_rate_3pct"]["delta_vs_baseline"] == 1
    assert scenarios["relax_change_rate_3pct"]["newly_unblocked_from"][0]["reason"] == "reject_change_rate_weak"
    assert scenarios["relax_near_high_0_95"]["pre_ai_pass_count"] == 1
    assert scenarios["relax_combo_3pct_0_95_7b"]["pre_ai_pass_count"] == 3
    assert scenarios["alt_pullback_continuation"]["pre_ai_pass_count"] == 4
    assert scenarios["alt_pullback_continuation"]["delta_vs_baseline"] == 3
    assert scenarios["alt_pullback_continuation"]["pass_symbols"] == ["KRW-A", "KRW-B", "KRW-C", "KRW-D"]
    assert scenarios["alt_broad_trend_positive_5m"]["pre_ai_pass_count"] == 5
    assert scenarios["alt_pullback_setup_allow_small_dip"]["pre_ai_pass_count"] == 6
