"""Phase 19 하락장 대응 단위 테스트."""
import json
from unittest.mock import patch
import fakeredis
import pytest

from app.consensus_signal_runner import _get_regime, _is_bearish_regime
from app.hedge_runner import _has_long_positions, _avg_market_ret, run_once


class TestGetRegime:
    def _set_ret(self, r, market, symbol, ret_5m):
        r.hset(f"ai:dual:last:claude:{market}:{symbol}", mapping={
            "features_json": json.dumps({"ret_5m": str(ret_5m), "current_price": "50000"}),
        })

    def test_majority_bearish_returns_bearish(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", -0.003), ("D", 0.002), ("E", -0.004)]:
            self._set_ret(r, "KR", sym, ret)
        assert _get_regime(r, "KR", ["A", "B", "C", "D", "E"]) == "bearish"

    def test_majority_bullish_returns_bullish(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", 0.01), ("B", 0.005), ("C", 0.003), ("D", 0.002), ("E", 0.004)]:
            self._set_ret(r, "KR", sym, ret)
        assert _get_regime(r, "KR", ["A", "B", "C", "D", "E"]) == "bullish"

    def test_mixed_returns_neutral(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", 0.003), ("D", 0.002), ("E", 0.004)]:
            self._set_ret(r, "KR", sym, ret)
        # 2/5=40% bearish → neutral
        assert _get_regime(r, "KR", ["A", "B", "C", "D", "E"]) == "neutral"

    def test_backward_compat_is_bearish_regime(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", -0.003), ("D", -0.007), ("E", -0.004)]:
            self._set_ret(r, "KR", sym, ret)
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is True

    def test_empty_watchlist_returns_neutral(self):
        r = fakeredis.FakeRedis()
        assert _get_regime(r, "KR", []) == "neutral"

    def test_insufficient_data_returns_neutral(self):
        r = fakeredis.FakeRedis()
        self._set_ret(r, "KR", "A", -0.01)
        self._set_ret(r, "KR", "B", -0.01)
        # only 2 data points → neutral
        assert _get_regime(r, "KR", ["A", "B"]) == "neutral"


class TestHasLongPositions:
    def test_no_positions_returns_false(self):
        r = fakeredis.FakeRedis()
        assert _has_long_positions(r, "KR") is False

    def test_inverse_etf_only_returns_false(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "114800")
        r.hset("position:KR:114800", mapping={"qty": "10", "avg_price": "5000"})
        assert _has_long_positions(r, "KR") is False

    def test_long_position_returns_true(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "005930")
        r.hset("position:KR:005930", mapping={"qty": "5", "avg_price": "70000"})
        assert _has_long_positions(r, "KR") is True

    def test_mixed_positions_returns_true(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "114800", "005930")
        r.hset("position:KR:114800", mapping={"qty": "10", "avg_price": "5000"})
        r.hset("position:KR:005930", mapping={"qty": "5", "avg_price": "70000"})
        assert _has_long_positions(r, "KR") is True


class TestAvgMarketRet:
    def _set_features(self, r, market, symbol, ret_5m):
        r.hset(f"ai:dual:last:claude:{market}:{symbol}", mapping={
            "features_json": json.dumps({"ret_5m": str(ret_5m), "current_price": "50000"}),
        })

    def test_insufficient_data_returns_none(self):
        r = fakeredis.FakeRedis()
        with patch("utils.redis_helpers.load_watchlist", return_value=["A", "B"]):
            self._set_features(r, "KR", "A", -0.01)
            result = _avg_market_ret(r, "KR")
        assert result is None

    def test_returns_average_ret(self):
        r = fakeredis.FakeRedis()
        with patch("utils.redis_helpers.load_watchlist", return_value=["A", "B", "C", "D"]):
            for sym, ret in [("A", -0.02), ("B", -0.01), ("C", -0.005), ("D", -0.005)]:
                self._set_features(r, "KR", sym, ret)
            result = _avg_market_ret(r, "KR")
        assert result is not None
        assert result == pytest.approx(-0.01)

    def test_inverse_etf_excluded_from_avg(self):
        r = fakeredis.FakeRedis()
        # 114800 (inverse ETF) has high positive ret but should be excluded
        with patch("utils.redis_helpers.load_watchlist", return_value=["A", "B", "C", "114800"]):
            for sym, ret in [("A", -0.02), ("B", -0.01), ("C", -0.005), ("114800", 0.05)]:
                self._set_features(r, "KR", sym, ret)
            result = _avg_market_ret(r, "KR")
        assert result is not None
        assert result == pytest.approx(-0.035 / 3)


class TestRunOnce:
    def test_no_trigger_when_no_positions(self):
        r = fakeredis.FakeRedis()
        result = run_once(r, "KR")
        assert result is False

    def test_no_trigger_when_lock_exists(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "005930")
        r.hset("position:KR:005930", mapping={"qty": "5", "avg_price": "70000"})
        r.set("claw:hedge:lock:KR", "1")
        result = run_once(r, "KR")
        assert result is False

    def test_no_trigger_when_ret_not_low_enough(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "005930")
        r.hset("position:KR:005930", mapping={"qty": "5", "avg_price": "70000"})
        r.set("mark:KR:114800", "4980")
        with patch("utils.redis_helpers.load_watchlist", return_value=["A", "B", "C"]):
            for sym, ret in [("A", -0.005), ("B", -0.003), ("C", -0.002)]:
                r.hset(f"ai:dual:last:claude:KR:{sym}", mapping={
                    "features_json": json.dumps({"ret_5m": str(ret)})
                })
            result = run_once(r, "KR")
        assert result is False

    def test_trigger_when_conditions_met(self):
        r = fakeredis.FakeRedis()
        # LONG position
        r.sadd("position_index:KR", "005930")
        r.hset("position:KR:005930", mapping={"qty": "5", "avg_price": "70000"})
        # Mark price for hedge symbol
        r.set("mark:KR:114800", "4980")

        with patch("utils.redis_helpers.load_watchlist", return_value=["A", "B", "C", "D"]), \
             patch("guards.notifier.send_telegram", side_effect=Exception("no tg")):
            # avg ret < -1%
            for sym, ret in [("A", -0.02), ("B", -0.015), ("C", -0.012), ("D", -0.018)]:
                r.hset(f"ai:dual:last:claude:KR:{sym}", mapping={
                    "features_json": json.dumps({"ret_5m": str(ret)})
                })
            result = run_once(r, "KR")
        assert result is True
        # 신호가 큐에 들어갔는지 확인
        assert r.llen("signal:KR") == 1
        # 락이 설정됐는지 확인
        assert r.exists("claw:hedge:lock:KR")

    def test_no_trigger_when_hedge_already_held(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "005930")
        r.hset("position:KR:005930", mapping={"qty": "5", "avg_price": "70000"})
        # Already holding inverse ETF
        r.hset("position:KR:114800", mapping={"qty": "10", "avg_price": "4900"})
        r.set("mark:KR:114800", "4980")

        with patch("utils.redis_helpers.load_watchlist", return_value=["A", "B", "C", "D"]):
            for sym, ret in [("A", -0.02), ("B", -0.015), ("C", -0.012), ("D", -0.018)]:
                r.hset(f"ai:dual:last:claude:KR:{sym}", mapping={
                    "features_json": json.dumps({"ret_5m": str(ret)})
                })
            result = run_once(r, "KR")
        assert result is False
