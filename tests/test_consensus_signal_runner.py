"""consensus_signal_runner 단위 테스트."""
from __future__ import annotations

import json
from decimal import Decimal

import fakeredis
import pytest

from app.consensus_signal_runner import normalize_kr_price_tick, run_once


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
              features_json: str | None = None):
    fj = features_json or _make_features()
    r.hset(f"ai:dual:last:claude:{market}:{symbol}", mapping={
        "emit": c_emit, "direction": c_dir, "features_json": fj,
    })
    r.hset(f"ai:dual:last:qwen:{market}:{symbol}", mapping={
        "emit": q_emit, "direction": q_dir, "features_json": fj,
    })


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

        result = run_once("KR", "005930", r)

        assert result is not None
        assert result["status"] == "candidate"
        assert result["source"] == "consensus_signal_runner"
        assert result["symbol"] == "005930"
        assert result["market"] == "KR"
        assert result["direction"] == "LONG"
        assert result["claude_emit"] == 1
        assert result["qwen_emit"] == 1

        # queue에 push 됐는지
        raw = r.rpop("claw:signal:queue")
        assert raw is not None
        queued = json.loads(raw)
        assert queued["signal_id"] == result["signal_id"]

    def test_entry_price_equals_current_price(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(current_price="70000"))

        result = run_once("KR", "005930", r)

        assert result["entry"]["price"] == "70000"
        # 1주: size_cash == price
        assert result["entry"]["size_cash"] == "70000"

    def test_stop_price_is_normalized(self):
        r = fakeredis.FakeRedis()
        # 70000 * 0.98 = 68600 → tick=100 → 68600 (이미 딱 떨어짐)
        _set_dual(r, "KR", "005930", features_json=_make_features(current_price="70000"))

        result = run_once("KR", "005930", r)

        assert result["stop"]["price"] == "68600"

    def test_audit_saved(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930")

        result = run_once("KR", "005930", r)

        audit_keys = r.keys("consensus:audit:KR:*")
        assert len(audit_keys) == 1

    def test_stats_incremented(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930")

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

    def test_consensus_failed_qwen_no_emit(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", q_emit="0")

        assert run_once("KR", "005930", r) is None

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert b"reject_consensus_failed" in stats

    def test_consensus_failed_claude_no_emit(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_emit="0")

        assert run_once("KR", "005930", r) is None

    def test_direction_mismatch(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_dir="LONG", q_dir="EXIT")

        assert run_once("KR", "005930", r) is None

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert b"reject_direction_mismatch" in stats

    def test_direction_not_long_rejected(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", c_dir="EXIT", q_dir="EXIT")

        assert run_once("KR", "005930", r) is None

    def test_prefilter_ret_5m_zero(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(ret_5m=0.0))

        assert run_once("KR", "005930", r) is None

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert b"reject_prefilter_ret_5m" in stats

    def test_prefilter_ret_5m_negative(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(ret_5m=-0.001))

        assert run_once("KR", "005930", r) is None

    def test_prefilter_range_5m_too_small(self):
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(range_5m=0.003))

        assert run_once("KR", "005930", r) is None

        from utils.redis_helpers import today_kst
        stats = r.hgetall(f"consensus:stats:KR:{today_kst()}")
        assert b"reject_prefilter_range_5m" in stats

    def test_prefilter_range_5m_exactly_004_rejected(self):
        # > 0.004 이어야 하므로 정확히 0.004는 거부
        r = fakeredis.FakeRedis()
        _set_dual(r, "KR", "005930", features_json=_make_features(range_5m=0.004))

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


# ---------------------------------------------------------------------------
# run_once — dedup (duplicate signal storm 방지)
# ---------------------------------------------------------------------------

class TestRunOnceDedup:
    def test_same_eval_result_not_pushed_twice(self):
        """동일 ts_ms로 두 번 poll 시 두 번째는 skip."""
        r = fakeredis.FakeRedis()
        # ts_ms 고정
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": "1700000000000",
            "features_json": _make_features(),
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": "1700000000000",
            "features_json": _make_features(),
        })

        result1 = run_once("KR", "005930", r)
        result2 = run_once("KR", "005930", r)  # 동일 ts_ms → skip

        assert result1 is not None
        assert result2 is None
        assert r.llen("claw:signal:queue") == 1  # 1건만 push

    def test_new_eval_result_is_pushed(self):
        """ts_ms가 바뀌면 새 신호 push."""
        r = fakeredis.FakeRedis()
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": "1700000000000",
            "features_json": _make_features(),
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": "1700000000000",
            "features_json": _make_features(),
        })

        run_once("KR", "005930", r)

        # symbol cooldown 해제 (Phase 11: cooldown 내에서는 재emit 차단)
        r.delete("consensus:symbol_cooldown:KR:005930")

        # ts_ms 갱신 (새 eval 결과) + cooldown 해제 → push 가능
        r.hset("ai:dual:last:claude:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": "1700000120000",
            "features_json": _make_features(),
        })
        r.hset("ai:dual:last:qwen:KR:005930", mapping={
            "emit": "1", "direction": "LONG",
            "ts_ms": "1700000120000",
            "features_json": _make_features(),
        })

        result2 = run_once("KR", "005930", r)

        assert result2 is not None
        assert r.llen("claw:signal:queue") == 2  # 2건 push
