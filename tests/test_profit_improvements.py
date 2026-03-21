"""수익 개선 기능 단위 테스트.

- trailing stop (HWM 기반)
- time_limit 수익 연장
- partial consensus (Claude EMIT + positive news)
- market regime filter
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from unittest.mock import patch, MagicMock

import fakeredis
import pytest


# ---------------------------------------------------------------------------
# Trailing Stop + Time Limit 테스트
# (position_exit_runner._check_exit 직접 테스트)
# ---------------------------------------------------------------------------

from app.position_exit_runner import _check_exit, _STOP_LOSS_PCT, _TAKE_PROFIT_PCT, _TIME_LIMIT_SEC, _TRAIL_STOP_PCT, _TIME_LIMIT_MAX_SEC


class TestTrailingStop:
    def test_no_hwm_uses_static_stop(self):
        """hwm_price=None이면 기존 static stop 동작."""
        avg = Decimal("100000")
        mark = avg * (1 - _STOP_LOSS_PCT) - 1
        result = _check_exit(avg, mark, int(time.time()), hwm_price=None)
        assert result is not None
        assert "stop_loss" in result

    def test_trailing_stop_from_hwm(self):
        """HWM 고점에서 trail_pct 이상 하락하면 trailing stop 발동.

        avg=100000, hwm=101000 → trail_stop=99485 > static_stop=98500
        mark=99000: static stop 위이지만 trail stop 아래 → trailing stop 발동
        """
        avg = Decimal("100000")
        hwm = Decimal("101000")  # +1% 상승 후 HWM
        # trail_stop = 101000 * (1 - 0.015) = 99485
        # static_stop = 100000 * (1 - 0.015) = 98500
        # effective_stop = max(98500, 99485) = 99485
        mark = Decimal("99000")  # static_stop(98500) 위지만 trail_stop(99485) 아래 → 발동
        result = _check_exit(avg, mark, int(time.time()), hwm_price=hwm)
        assert result is not None
        assert "stop_loss" in result

    def test_trailing_stop_not_triggered_above_trail(self):
        """HWM에서 trail_pct 이내 하락이면 trailing stop 미발동."""
        avg = Decimal("100000")
        hwm = Decimal("101000")
        # trail_stop = 101000 * 0.985 = 99485
        # take_price = 100000 * 1.02 = 102000 (기본값)
        mark = Decimal("100500")  # trail_stop(99485) 위, take_price(102000) 아래 → hold
        result = _check_exit(avg, mark, int(time.time()), hwm_price=hwm)
        assert result is None

    def test_trailing_stop_locks_in_profit(self):
        """가격이 avg 이상이고 HWM 기반 trail stop 위이면 hold."""
        avg = Decimal("100000")
        hwm = Decimal("102000")
        # trail_stop = 102000 * 0.985 = 100470
        mark = Decimal("101000")  # avg 이상, trail stop(100470) 이상 → hold
        result = _check_exit(avg, mark, int(time.time()), hwm_price=hwm)
        assert result is None

    def test_hwm_equals_avg_no_change(self):
        """HWM이 avg_price와 같으면(진입 직후) trailing stop 없음 → static stop만."""
        avg = Decimal("100000")
        hwm = Decimal("100000")  # HWM = avg (trail 조건 불만족: hwm > avg 아님)
        mark = avg * (1 - _STOP_LOSS_PCT) + 1  # static stop 바로 위 → hold
        result = _check_exit(avg, mark, int(time.time()), hwm_price=hwm)
        assert result is None


class TestTimeLimitExtension:
    def _old_ts(self, seconds_ago: int) -> int:
        return int(time.time()) - seconds_ago

    def test_time_limit_no_extension_when_loss(self):
        """손실 중이면 time_limit 기본 발동."""
        avg = Decimal("100000")
        mark = Decimal("99000")  # 손실
        opened_ts = self._old_ts(_TIME_LIMIT_SEC + 10)
        result = _check_exit(avg, mark, opened_ts)
        assert result is not None
        assert "time_limit" in result

    def test_time_limit_extended_when_profitable(self):
        """수익 중이면 TIME_LIMIT_SEC 초과해도 연장 (하드 max 미도달)."""
        avg = Decimal("100000")
        mark = Decimal("101000")  # 수익
        opened_ts = self._old_ts(_TIME_LIMIT_SEC + 10)  # 기본 time_limit 초과
        result = _check_exit(avg, mark, opened_ts)
        # 수익 중이고 TIME_LIMIT_MAX_SEC 미도달 → exit 없음
        assert result is None

    def test_time_limit_max_fires_even_profitable(self):
        """TIME_LIMIT_MAX_SEC 초과이면 수익이어도 강제 청산."""
        avg = Decimal("100000")
        mark = Decimal("101000")  # 수익
        opened_ts = self._old_ts(_TIME_LIMIT_MAX_SEC + 10)
        result = _check_exit(avg, mark, opened_ts)
        assert result is not None
        assert "time_limit" in result


# ---------------------------------------------------------------------------
# Partial Consensus 테스트
# ---------------------------------------------------------------------------

from app.consensus_signal_runner import _has_positive_news, run_once


class TestHasPositiveNews:
    def test_no_news_returns_false(self):
        r = fakeredis.FakeRedis()
        assert _has_positive_news(r, "KR", "005930") is False

    def test_positive_high_news_returns_true(self):
        r = fakeredis.FakeRedis()
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            r.lpush("news:symbol:KR:005930:20260318",
                    json.dumps({"sentiment": "positive", "impact": "high"}))
            assert _has_positive_news(r, "KR", "005930") is True

    def test_positive_medium_news_returns_true(self):
        r = fakeredis.FakeRedis()
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            r.lpush("news:symbol:KR:005930:20260318",
                    json.dumps({"sentiment": "positive", "impact": "medium"}))
            assert _has_positive_news(r, "KR", "005930") is True

    def test_positive_low_news_returns_false(self):
        """positive+low는 partial consensus 허용 안 함."""
        r = fakeredis.FakeRedis()
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            r.lpush("news:symbol:KR:005930:20260318",
                    json.dumps({"sentiment": "positive", "impact": "low"}))
            assert _has_positive_news(r, "KR", "005930") is False

    def test_negative_news_returns_false(self):
        r = fakeredis.FakeRedis()
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            r.lpush("news:symbol:KR:005930:20260318",
                    json.dumps({"sentiment": "negative", "impact": "high"}))
            assert _has_positive_news(r, "KR", "005930") is False


def _make_dual_eval_data(r, market, symbol, c_emit, q_emit, c_dir="LONG", q_dir="LONG",
                          price="50000", ret_5m="0.005", range_5m="0.006"):
    """fakeredis에 dual eval 결과 세팅 헬퍼."""
    ts = str(int(time.time() * 1000))
    features = json.dumps({
        "current_price": price, "ret_5m": ret_5m, "range_5m": range_5m,
    })
    r.hset(f"ai:dual:last:claude:{market}:{symbol}", mapping={
        "emit": "1" if c_emit else "0", "direction": c_dir,
        "ts_ms": ts, "features_json": features,
    })
    r.hset(f"ai:dual:last:qwen:{market}:{symbol}", mapping={
        "emit": "1" if q_emit else "0", "direction": q_dir,
        "ts_ms": ts + "1",  # slightly different to avoid seen_key collision
        "features_json": features,
    })


class TestPartialConsensus:
    def test_claude_only_emit_no_news_rejected(self):
        """Claude EMIT + Qwen HOLD + 뉴스 없음 → reject."""
        r = fakeredis.FakeRedis()
        _make_dual_eval_data(r, "KR", "005930", c_emit=True, q_emit=False)
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"), \
             patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("50000")):
            result = run_once("KR", "005930", r)
        assert result is None

    def test_claude_only_emit_with_positive_news_allowed(self):
        """Claude EMIT + Qwen HOLD + positive 뉴스 → signal 생성 (partial)."""
        r = fakeredis.FakeRedis()
        _make_dual_eval_data(r, "KR", "005930", c_emit=True, q_emit=False)
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "high"}))
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"), \
             patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("100000")):
            result = run_once("KR", "005930", r)
        assert result is not None
        assert result.get("partial_consensus") is True
        # size_cash = 100000 / 2 = 50000
        assert Decimal(result["entry"]["size_cash"]) == Decimal("50000")

    def test_full_consensus_not_affected(self):
        """Claude+Qwen 모두 EMIT이면 full consensus — 뉴스/confidence 가중 적용.
        뉴스 없음(0.8) × confidence 0.7(1.0배) = 0.8배 → 80000."""
        r = fakeredis.FakeRedis()
        _make_dual_eval_data(r, "KR", "005930", c_emit=True, q_emit=True)
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"), \
             patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("100000")):
            result = run_once("KR", "005930", r)
        assert result is not None
        assert result.get("partial_consensus") is False
        # 뉴스 없음 → news_mult=0.8, conf=0.7 → conf_mult=1.0 → 100000 * 0.8 = 80000
        assert Decimal(result["entry"]["size_cash"]) == Decimal("80000.00")


# ---------------------------------------------------------------------------
# Market Regime Filter 테스트
# ---------------------------------------------------------------------------

from app.consensus_signal_runner import _is_bearish_regime


class TestBearishRegimeFilter:
    def _set_features(self, r, market, symbol, ret_5m):
        r.hset(f"ai:dual:last:claude:{market}:{symbol}", mapping={
            "features_json": json.dumps({"ret_5m": str(ret_5m), "current_price": "50000"}),
        })

    def test_majority_bearish_returns_true(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", -0.003), ("D", 0.002), ("E", -0.004)]:
            self._set_features(r, "KR", sym, ret)
        # 4/5 = 80% bearish → True
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is True

    def test_majority_bullish_returns_false(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", 0.01), ("B", 0.005), ("C", -0.003), ("D", 0.002), ("E", 0.004)]:
            self._set_features(r, "KR", sym, ret)
        # 1/5 = 20% bearish → False
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is False

    def test_exactly_60_percent_bearish_returns_false(self):
        """60% 이하면 False (> 0.6 조건이므로 정확히 60% = False)."""
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", -0.003), ("D", 0.002), ("E", 0.004)]:
            self._set_features(r, "KR", sym, ret)
        # 3/5 = 60% → > 0.6이 아님 → False
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is False

    def test_insufficient_data_returns_false(self):
        """데이터 있는 종목이 3개 미만이면 False."""
        r = fakeredis.FakeRedis()
        self._set_features(r, "KR", "A", -0.01)
        self._set_features(r, "KR", "B", -0.005)
        assert _is_bearish_regime(r, "KR", ["A", "B"]) is False

    def test_empty_watchlist_returns_false(self):
        r = fakeredis.FakeRedis()
        assert _is_bearish_regime(r, "KR", []) is False
