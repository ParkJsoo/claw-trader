"""consensus_signal_runner 단위 테스트."""
from __future__ import annotations

import json
import time
from decimal import Decimal
from unittest.mock import patch

import fakeredis
import pytest

from app.consensus_signal_runner import _classify_claude_veto, _get_live_ret_5m, normalize_kr_price_tick, run_once


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_features(
    current_price: str = "70000",
    ret_5m: float = 0.003,
    range_5m: float = 0.005,
) -> str:
    return json.dumps({
        "current_price": current_price,
        "ret_5m": str(ret_5m),
        "range_5m": str(range_5m),
    })


def _set_dual(r, market: str, symbol: str,
              c_emit="1", q_emit="1",
              c_dir="LONG", q_dir="LONG",
              features_json: str | None = None,
              ts_ms: str | None = None):
    fj = features_json or _make_features()
    ts_ms = ts_ms or str(int(time.time() * 1000))
    r.hset(f"ai:dual:last:claude:{market}:{symbol}", mapping={
        "emit": c_emit, "direction": c_dir, "features_json": fj, "ts_ms": ts_ms,
    })
    r.hset(f"ai:dual:last:qwen:{market}:{symbol}", mapping={
        "emit": q_emit, "direction": q_dir, "features_json": fj, "ts_ms": ts_ms,
    })


def _set_live_mark_hist(r, market: str, symbol: str,
                        latest_price: str = "70000",
                        past_price: str = "67700"):
    now_ms = int(time.time() * 1000)
    latest_ts = now_ms - 1000
    past_ts = now_ms - (5 * 60 * 1000) - 1000
    key = f"mark_hist:{market}:{symbol}"
    r.delete(key)
    r.rpush(key, f"{latest_ts}:{latest_price}", f"{past_ts}:{past_price}")


def _set_dense_mark_hist(
    r,
    market: str,
    symbol: str,
    *,
    latest_price: str = "70000",
    past_price: str = "67700",
    step_sec: int = 4,
    total_points: int = 100,
):
    """최근 30개로는 5분 전 가격이 보이지 않도록 조밀한 mark_hist 생성."""
    now_ms = int(time.time() * 1000)
    latest = Decimal(latest_price)
    past = Decimal(past_price)
    key = f"mark_hist:{market}:{symbol}"
    r.delete(key)

    for idx in range(total_points):
        age_ms = 1000 + idx * step_sec * 1000
        ts_ms = now_ms - age_ms
        price = latest if age_ms < 5 * 60 * 1000 else past
        r.rpush(key, f"{ts_ms}:{price}")


# ---------------------------------------------------------------------------
# normalize_kr_price_tick
# ---------------------------------------------------------------------------

class TestNormalizeKrPriceTick:
    def test_below_1000(self):
        assert normalize_kr_price_tick(Decimal("999")) == Decimal("999")

    def test_1000_to_4999_tick_5(self):
        # 1003 → 1000 (내림)
        assert normalize_kr_price_tick(Decimal("1003")) == Decimal("1000")

    def test_5000_to_9999_tick_10(self):
        assert normalize_kr_price_tick(Decimal("5007")) == Decimal("5000")

    def test_10000_to_49999_tick_50(self):
        assert normalize_kr_price_tick(Decimal("10030")) == Decimal("10000")

    def test_50000_to_99999_tick_100(self):
        assert normalize_kr_price_tick(Decimal("70080")) == Decimal("70000")

    def test_100000_to_499999_tick_500(self):
        assert normalize_kr_price_tick(Decimal("100300")) == Decimal("100000")

    def test_500000_plus_tick_1000(self):
        assert normalize_kr_price_tick(Decimal("500700")) == Decimal("500000")


# ---------------------------------------------------------------------------
# run_once — happy path
# ---------------------------------------------------------------------------

class TestRunOnceHappyPath:
    def test_candidate_created_and_queued(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930")
        _set_live_mark_hist(r, "KR", "005930")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            result = run_once("KR", "005930", r)

        assert result is not None
        assert result["status"] == "candidate"
        assert result["source"] == "consensus_signal_runner"
        assert result["symbol"] == "005930"
        assert result["market"] == "KR"
        assert result["direction"] == "LONG"
        assert result["claude_emit"] == 1

        # queue에 push 됐는지
        raw = r.rpop("claw:signal:queue")
        assert raw is not None
        queued = json.loads(raw)
        assert queued["signal_id"] == result["signal_id"]

    def test_entry_price_equals_current_price(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(current_price="70000"))
        _set_live_mark_hist(r, "KR", "005930", latest_price="70000", past_price="67700")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            result = run_once("KR", "005930", r)

        assert Decimal(result["entry"]["price"]) == Decimal("70000")
        # 1주: size_cash == price
        assert Decimal(result["entry"]["size_cash"]) == Decimal("70000")

    def test_stop_price_is_normalized(self):
        r = fakeredis.FakeRedis()
        # range_5m=0.005 → stop_pct=max(0.015, 0.005*1.2)=0.015
        # stop_raw = 70000 * 0.985 = 68950 → tick=100 → 68900
        _set_dual(r, "KR", "005930", features_json=_make_features(current_price="70000"))
        _set_live_mark_hist(r, "KR", "005930", latest_price="70000", past_price="67700")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            result = run_once("KR", "005930", r)

        assert result["stop"]["price"] == "68900"

    def test_audit_saved(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930")
        _set_live_mark_hist(r, "KR", "005930")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            result = run_once("KR", "005930", r)

        audit_keys = r.keys("consensus:audit:KR:*")
        assert len(audit_keys) == 1

    def test_stats_incremented(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930")
        _set_live_mark_hist(r, "KR", "005930")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            run_once("KR", "005930", r)

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert int(stats[b"candidate"]) == 1


# ---------------------------------------------------------------------------
# run_once — reject cases
# ---------------------------------------------------------------------------

class TestRunOnceReject:
    def test_no_data_returns_none(self):
        r = fakeredis.FakeRedis()
        assert run_once("KR", "005930", r) is None

    def test_qwen_no_emit_does_not_block_claude_only_mode(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", q_emit="0")
        _set_live_mark_hist(r, "KR", "005930")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            assert run_once("KR", "005930", r) is not None

    def test_consensus_failed_claude_no_emit(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_emit="0")

        assert run_once("KR", "005930", r) is None

    def test_qwen_direction_mismatch_does_not_block_claude_only_mode(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_dir="LONG", q_dir="EXIT")
        _set_live_mark_hist(r, "KR", "005930")

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            assert run_once("KR", "005930", r) is not None

    def test_direction_not_long_rejected(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_dir="EXIT", q_dir="EXIT")

        assert run_once("KR", "005930", r) is None

    def test_prefilter_ret_5m_zero(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(ret_5m=0.0))
        _set_live_mark_hist(r, "KR", "005930", latest_price="70000", past_price="70000")

        assert run_once("KR", "005930", r) is None

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert b"reject_prefilter_ret_5m" in stats

    def test_prefilter_ret_5m_negative(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(ret_5m=-0.001))
        _set_live_mark_hist(r, "KR", "005930", latest_price="69000", past_price="70000")

        assert run_once("KR", "005930", r) is None

    def test_prefilter_range_5m_too_small(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(range_5m=0.003))
        _set_live_mark_hist(r, "KR", "005930")

        assert run_once("KR", "005930", r) is None

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert b"reject_prefilter_range_5m" in stats

    def test_prefilter_range_5m_exactly_004_rejected(self):
        # > 0.004 이어야 하므로 정확히 0.004는 거부
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(range_5m=0.004))
        _set_live_mark_hist(r, "KR", "005930")

        assert run_once("KR", "005930", r) is None

    def test_invalid_features_json(self):
        r = fakeredis.FakeRedis()
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG", "features_json": "not-json",
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG", "features_json": "not-json",
        })

        assert run_once("KR", "005930", r) is None

    def test_missing_current_price(self):
        r = fakeredis.FakeRedis()
        fj = json.dumps({"ret_5m": "0.003", "range_5m": "0.005"})
        _set_dual(r, "KR", "005930", features_json=fj)

        assert run_once("KR", "005930", r) is None

    def test_nothing_enqueued_on_reject(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_emit="0")

        run_once("KR", "005930", r)

        assert r.llen("claw:signal:queue") == 0

    def test_get_live_ret_5m_handles_dense_mark_hist(self):
        r = fakeredis.FakeRedis()
        _set_dense_mark_hist(r, "KR", "005930")

        live = _get_live_ret_5m(r, "KR", "005930")

        assert live is not None
        ret_5m, latest_price = live
        assert latest_price == 70000.0
        assert ret_5m == pytest.approx((70000.0 - 67700.0) / 67700.0, rel=1e-4)


# ---------------------------------------------------------------------------
# run_once — dedup (duplicate signal storm 방지)
# ---------------------------------------------------------------------------

class TestRunOnceDedup:
    def test_same_eval_result_not_pushed_twice(self):
        """동일 ts_ms로 두 번 poll 시 두 번째는 skip."""
        r = fakeredis.FakeRedis()
        _set_live_mark_hist(r, "KR", "005930")
        fresh_ts_ms = str(int(time.time() * 1000))
        # ts_ms 고정
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": fresh_ts_ms,
            "features_json": _make_features(),
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": fresh_ts_ms,
            "features_json": _make_features(),
        })

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            result1 = run_once("KR", "005930", r)
            result2 = run_once("KR", "005930", r)  # 동일 ts_ms → skip

        assert result1 is not None
        assert result2 is None
        assert r.llen("claw:signal:queue") == 1  # 1건만 push

    def test_new_eval_result_is_pushed(self):
        """ts_ms가 바뀌면 새 신호 push."""
        r = fakeredis.FakeRedis()
        _set_live_mark_hist(r, "KR", "005930")
        first_ts_ms = str(int(time.time() * 1000))
        second_ts_ms = str(int(time.time() * 1000) + 120000)
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": first_ts_ms,
            "features_json": _make_features(),
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": first_ts_ms,
            "features_json": _make_features(),
        })

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            run_once("KR", "005930", r)

        # symbol cooldown 해제 (Phase 11: cooldown 내에서는 재emit 차단)
        r.delete("consensus:symbol_cooldown:KR:005930")

        # ts_ms 갱신 (새 eval 결과) + cooldown 해제 → push 가능
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": second_ts_ms,
            "features_json": _make_features(),
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": second_ts_ms,
            "features_json": _make_features(),
        })

        with patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("70000")):
            result2 = run_once("KR", "005930", r)

        assert result2 is not None
        assert r.llen("claw:signal:queue") == 2  # 2건 push


class TestClaudeVetoClassification:
    def test_market_close_reason(self):
        code, label = _classify_claude_veto("Market closed soon, skip entry.")
        assert code == "reject_market_close"
        assert label == "market_close"

    def test_late_entry_reason(self):
        code, label = _classify_claude_veto("Too late entry after breakout.")
        assert code == "reject_late_entry"
        assert label == "late_entry"

    def test_momentum_decay_reason(self):
        code, label = _classify_claude_veto("Momentum decay already visible.")
        assert code == "reject_momentum_decay"
        assert label == "momentum_decay"

    def test_generic_reason_falls_back(self):
        code, label = _classify_claude_veto("Risk reward no longer attractive.")
        assert code == "reject_claude_veto"
        assert label == "claude_veto"
