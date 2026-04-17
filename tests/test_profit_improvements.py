"""수익 개선 기능 단위 테스트.

- trailing stop (HWM 기반)
- time_limit 수익 연장
- claude_only consensus + news boost
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
        """한 번이라도 수익권(HWM>avg)에 진입한 포지션만 TIME_LIMIT 연장."""
        avg = Decimal("100000")
        mark = Decimal("101000")  # 수익
        opened_ts = self._old_ts(_TIME_LIMIT_SEC + 10)  # 기본 time_limit 초과
        result = _check_exit(avg, mark, opened_ts, hwm_price=Decimal("101500"))
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


def _set_live_mark_hist(r, market, symbol, latest_price="50000", past_price="48350"):
    now_ms = int(time.time() * 1000)
    latest_ts = now_ms - 1000
    past_ts = now_ms - (5 * 60 * 1000) - 1000
    key = f"mark_hist:{market}:{symbol}"
    r.delete(key)
    r.rpush(key, f"{latest_ts}:{latest_price}", f"{past_ts}:{past_price}")


class TestClaudeOnlyConsensus:
    def test_claude_emit_qwen_hold_still_allows_in_claude_only_mode(self):
        """Qwen HOLD는 현재 claude_only 실행 모드에서 진입 차단 사유가 아니다."""
        r = fakeredis.FakeRedis()
        _make_dual_eval_data(r, "KR", "005930", c_emit=True, q_emit=False)
        _set_live_mark_hist(r, "KR", "005930")
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"), \
             patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("50000")):
            result = run_once("KR", "005930", r)
        assert result is not None

    def test_positive_high_news_boosts_size_cash(self):
        """KR positive+high 뉴스는 현재 size_cash 1.5배 boost를 건다."""
        r = fakeredis.FakeRedis()
        _make_dual_eval_data(r, "KR", "005930", c_emit=True, q_emit=False)
        _set_live_mark_hist(r, "KR", "005930")
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "high"}))
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"), \
             patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("100000")):
            result = run_once("KR", "005930", r)
        assert result is not None
        assert "partial_consensus" not in result
        assert Decimal(result["entry"]["size_cash"]) == Decimal("150000.0")

    def test_full_consensus_has_no_extra_news_penalty(self):
        """현재 full consensus에서도 뉴스 없음 감산은 없다."""
        r = fakeredis.FakeRedis()
        _make_dual_eval_data(r, "KR", "005930", c_emit=True, q_emit=True)
        _set_live_mark_hist(r, "KR", "005930")
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"), \
             patch("app.consensus_signal_runner._calc_size_cash", return_value=Decimal("100000")):
            result = run_once("KR", "005930", r)
        assert result is not None
        assert "partial_consensus" not in result
        assert Decimal(result["entry"]["size_cash"]) == Decimal("100000.0")


# ---------------------------------------------------------------------------
# Market Regime Filter 테스트
# ---------------------------------------------------------------------------

from app.consensus_signal_runner import _is_bearish_regime


class TestBearishRegimeFilter:
    def _set_mark_hist(self, r, market, symbol, ret_5m):
        latest_price = Decimal("50000")
        past_price = latest_price / (Decimal("1") + Decimal(str(ret_5m)))
        _set_live_mark_hist(
            r,
            market,
            symbol,
            latest_price=str(latest_price),
            past_price=str(past_price.quantize(Decimal("0.0001"))),
        )

    def test_majority_bearish_returns_true(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", -0.003), ("D", 0.002), ("E", -0.004)]:
            self._set_mark_hist(r, "KR", sym, ret)
        # 4/5 = 80% bearish → True
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is True

    def test_majority_bullish_returns_false(self):
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", 0.01), ("B", 0.005), ("C", -0.003), ("D", 0.002), ("E", 0.004)]:
            self._set_mark_hist(r, "KR", sym, ret)
        # 1/5 = 20% bearish → False
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is False

    def test_exactly_60_percent_bearish_returns_false(self):
        """60% 이하면 False (> 0.6 조건이므로 정확히 60% = False)."""
        r = fakeredis.FakeRedis()
        for sym, ret in [("A", -0.01), ("B", -0.005), ("C", -0.003), ("D", 0.002), ("E", 0.004)]:
            self._set_mark_hist(r, "KR", sym, ret)
        # 3/5 = 60% → > 0.6이 아님 → False
        assert _is_bearish_regime(r, "KR", ["A", "B", "C", "D", "E"]) is False

    def test_insufficient_data_returns_false(self):
        """데이터 있는 종목이 3개 미만이면 False."""
        r = fakeredis.FakeRedis()
        self._set_mark_hist(r, "KR", "A", -0.01)
        self._set_mark_hist(r, "KR", "B", -0.005)
        assert _is_bearish_regime(r, "KR", ["A", "B"]) is False

    def test_empty_watchlist_returns_false(self):
        r = fakeredis.FakeRedis()
        assert _is_bearish_regime(r, "KR", []) is False
