from __future__ import annotations

import time

import fakeredis

from app.ai_dual_eval_runner import (
    _coin_ret1m_accel_ratio,
    _is_last_eval_stale,
    _load_last_eval_meta,
    _purge_stale_last_eval_if_needed,
    _record_scan_state,
    _ret_1m_threshold_for_market,
)


class TestStaleEvalCacheCleanup:
    def test_load_last_eval_meta_supports_fakeredis(self):
        r = fakeredis.FakeRedis()
        now_ms = int(time.time() * 1000)
        r.hset("ai:dual:last:claude:COIN:KRW-BTC", mapping={
            "emit": "0",
            "ts_ms": str(now_ms),
        })

        meta = _load_last_eval_meta(r, "COIN", "KRW-BTC")

        assert meta is not None
        assert meta["emit"] == "0"
        assert meta["ts_ms"] == now_ms

    def test_purge_stale_hold_eval_deletes_old_cache(self):
        r = fakeredis.FakeRedis()
        now_ms = int(time.time() * 1000)
        r.hset("ai:dual:last:claude:COIN:KRW-BTC", mapping={
            "emit": "0",
            "ts_ms": str(now_ms - (31 * 60 * 1000)),
        })

        deleted = _purge_stale_last_eval_if_needed(
            r,
            "COIN",
            "KRW-BTC",
            now_ms,
            stats_key="ai:dual_stats:consensus:COIN:20260421",
        )

        assert deleted is True
        assert r.exists("ai:dual:last:claude:COIN:KRW-BTC") == 0
        assert r.hget("ai:dual_stats:consensus:COIN:20260421", "cleared_stale_last_eval") == b"1"

    def test_recent_hold_eval_is_kept(self):
        r = fakeredis.FakeRedis()
        now_ms = int(time.time() * 1000)
        r.hset("ai:dual:last:claude:COIN:KRW-BTC", mapping={
            "emit": "0",
            "ts_ms": str(now_ms - (5 * 60 * 1000)),
        })

        meta = _load_last_eval_meta(r, "COIN", "KRW-BTC")

        assert meta is not None
        assert _is_last_eval_stale(meta, now_ms) is False
        assert _purge_stale_last_eval_if_needed(r, "COIN", "KRW-BTC", now_ms) is False
        assert r.exists("ai:dual:last:claude:COIN:KRW-BTC") == 1


class TestScanStateRecording:
    def test_record_scan_state_writes_last_scan_and_stats(self):
        r = fakeredis.FakeRedis()
        now_ms = int(time.time() * 1000)

        _record_scan_state(
            r,
            "COIN",
            "KRW-BTC",
            "20260421",
            status="skip_prefilter_ret5m",
            now_ms=now_ms,
            details={"ret_5m": "0.01", "range_5m": "0.02"},
        )

        row = r.hgetall("ai:dual:scan:last:COIN:KRW-BTC")
        stats = r.hgetall("ai:dual_stats:consensus:COIN:20260421")

        assert row[b"status"] == b"skip_prefilter_ret5m"
        assert row[b"ret_5m"] == b"0.01"
        assert row[b"range_5m"] == b"0.02"
        assert stats[b"scan_total"] == b"1"
        assert stats[b"scan_skip_prefilter_ret5m"] == b"1"


class TestCoinAccelHelpers:
    def test_ret_1m_threshold_for_coin_is_stricter(self):
        assert _ret_1m_threshold_for_market("COIN") >= _ret_1m_threshold_for_market("KR")

    def test_coin_accel_ratio_is_computed(self):
        assert _coin_ret1m_accel_ratio(0.02, 0.01) == 0.5

    def test_coin_accel_ratio_handles_invalid_inputs(self):
        assert _coin_ret1m_accel_ratio(None, 0.01) is None
        assert _coin_ret1m_accel_ratio(0.0, 0.01) is None
