"""백테스트 프레임워크 단위 테스트."""
from __future__ import annotations

import json
from decimal import Decimal

import fakeredis
import pytest

from app.backtester import (
    ParamSet, SimResult, Backtester,
    _parse_mark_hist, simulate_one, summarize_results,
    _TIME_LIMIT_TICKS, _TIME_LIMIT_MAX_TICKS,
)


def _make_prices(base: float, changes: list[float]) -> list[tuple[int, Decimal]]:
    """변화율 시퀀스로 가격 시리즈 생성."""
    prices = [(0, Decimal(str(base)))]
    for i, pct in enumerate(changes, 1):
        prev = float(prices[-1][1])
        new_p = prev * (1 + pct)
        prices.append((i * 3000, Decimal(str(round(new_p, 2)))))
    return prices


class TestParseMakrHist:
    def test_parse_valid_entries(self):
        raw = [b"1700000000000:50000", b"1700000003000:50100"]
        result = _parse_mark_hist(raw)
        assert len(result) == 2
        assert result[0] == (1700000000000, Decimal("50000"))

    def test_skip_malformed(self):
        raw = [b"bad_entry", b"1700000000000:50000"]
        result = _parse_mark_hist(raw)
        assert len(result) == 1

    def test_sorted_ascending(self):
        raw = [b"2000:100", b"1000:90", b"3000:110"]
        result = _parse_mark_hist(raw)
        assert result[0][0] == 1000


class TestSimulateOne:
    def _params(self, stop="0.015", take="0.030", trail="0.015"):
        return ParamSet(Decimal(stop), Decimal(take), Decimal(trail))

    def test_take_profit_trigger(self):
        # 총 수익률 = (1.005)^20 - 1 ≈ 10% → take profit
        p = _make_prices(100000, [0.005] * 20)
        params = self._params(take="0.030")
        result = simulate_one(p, "TEST", params)
        assert result is not None
        assert result.exit_reason == "take_profit"
        assert result.pnl_pct > 0

    def test_stop_loss_trigger(self):
        # 연속 하락 → stop_loss
        p = _make_prices(100000, [-0.003] * 20)
        params = self._params(stop="0.015")
        result = simulate_one(p, "TEST", params)
        assert result is not None
        assert result.exit_reason in ("stop_loss", "trailing_stop")
        assert result.pnl_pct < 0

    def test_trailing_stop_locks_profit(self):
        # 상승 후 하락 → trailing stop이 static stop보다 높아서 발동
        # take=0.20 으로 멀리 설정해 take_profit이 먼저 발동하지 않도록 함
        # [0.01]*5: HWM ≈ 105101, trail_stop ≈ 103624 > static_stop(98500)
        # [-0.02]*5: 가격 급락으로 trail_stop 발동
        p = _make_prices(100000, [0.01] * 5 + [-0.02] * 5)
        params = self._params(stop="0.015", take="0.20", trail="0.015")
        result = simulate_one(p, "TEST", params)
        assert result is not None
        assert result.exit_reason == "trailing_stop"

    def test_time_limit_when_loss(self):
        # 손실 중 time_limit 초과 — 하락폭을 작게 해서 stop_loss 발동 전에 time_limit 도달
        # -0.00002 per tick * 605 ticks = -1.2% < stop_loss(1.5%) 이므로 stop 미발동
        p = _make_prices(100000, [-0.00002] * (_TIME_LIMIT_TICKS + 5))
        params = self._params(stop="0.015")
        result = simulate_one(p, "TEST", params)
        assert result is not None
        assert result.exit_reason == "time_limit"

    def test_time_limit_extended_when_profit(self):
        # 수익 중 TIME_LIMIT_TICKS 초과지만 MAX 미도달 → hold 계속 → end_of_data
        # +0.00002 per tick * 601 ticks ≈ +1.2% < take(20%) 이므로 take 미발동
        p = _make_prices(100000, [0.00002] * (_TIME_LIMIT_TICKS + 1))
        params = self._params(take="0.20")
        result = simulate_one(p, "TEST", params)
        assert result is not None
        # 수익 중이고 MAX 미도달이므로 end_of_data
        assert result.exit_reason == "end_of_data"

    def test_time_limit_max_fires_even_profitable(self):
        # 수익 중에도 MAX 초과 → time_limit
        p = _make_prices(100000, [0.0001] * (_TIME_LIMIT_MAX_TICKS + 5))
        params = self._params(take="0.20")
        result = simulate_one(p, "TEST", params)
        assert result is not None
        assert result.exit_reason == "time_limit"

    def test_insufficient_data_returns_none(self):
        p = _make_prices(100000, [0.001] * 5)
        assert simulate_one(p, "TEST", self._params()) is None


class TestSummarizeResults:
    def _result(self, params, pnl):
        return SimResult(
            param=params, symbol="A",
            entry_price=Decimal("100000"),
            exit_price=Decimal(str(100000 * (1 + pnl))),
            exit_reason="take_profit" if pnl > 0 else "stop_loss",
            pnl_pct=Decimal(str(pnl)),
            hold_ticks=10,
        )

    def test_win_rate_calculation(self):
        p = ParamSet(Decimal("0.015"), Decimal("0.030"), Decimal("0.015"))
        results = [self._result(p, 0.03), self._result(p, -0.015), self._result(p, 0.02)]
        summary = summarize_results(results, p)
        assert summary.wins == 2
        assert summary.win_rate == Decimal("0.667")

    def test_profit_factor(self):
        p = ParamSet(Decimal("0.015"), Decimal("0.030"), Decimal("0.015"))
        results = [self._result(p, 0.03), self._result(p, -0.015)]
        summary = summarize_results(results, p)
        assert summary.profit_factor == Decimal("2.00")

    def test_empty_results(self):
        p = ParamSet(Decimal("0.015"), Decimal("0.030"), Decimal("0.015"))
        summary = summarize_results([], p)
        assert summary.total == 0


class TestBacktester:
    def _setup_mark_hist(self, r, market, symbol, prices):
        key = f"mark_hist:{market}:{symbol}"
        for ts, price in prices:
            r.rpush(key, f"{ts}:{price}")

    def test_run_sweep_returns_results(self):
        r = fakeredis.FakeRedis()
        p = _make_prices(50000, [0.002] * 50)
        self._setup_mark_hist(r, "KR", "005930", p)

        bt = Backtester(r, "KR")
        results, summaries = bt.run_sweep(
            ["005930"],
            stop_pcts=[Decimal("0.015"), Decimal("0.020")],
            take_pcts=[Decimal("0.030")],
            trail_pcts=[Decimal("0.015")],
        )
        assert len(summaries) > 0
        assert all(s.total > 0 for s in summaries)

    def test_save_and_load_results(self):
        r = fakeredis.FakeRedis()
        p = _make_prices(50000, [0.002] * 50)
        self._setup_mark_hist(r, "KR", "005930", p)

        bt = Backtester(r, "KR")
        _, summaries = bt.run_sweep(
            ["005930"],
            stop_pcts=[Decimal("0.015")],
            take_pcts=[Decimal("0.030")],
            trail_pcts=[Decimal("0.015")],
        )
        bt.save_results(summaries)
        from utils.redis_helpers import today_kst
        key = f"backtest:result:KR:{today_kst()}"
        saved = json.loads(r.get(key))
        assert len(saved) > 0

    def test_format_report_includes_best_param(self):
        r = fakeredis.FakeRedis()
        p = _make_prices(50000, [0.002] * 50)
        self._setup_mark_hist(r, "KR", "005930", p)

        bt = Backtester(r, "KR")
        _, summaries = bt.run_sweep(
            ["005930"],
            stop_pcts=[Decimal("0.015")],
            take_pcts=[Decimal("0.030")],
            trail_pcts=[Decimal("0.015")],
        )
        current = ParamSet(Decimal("0.015"), Decimal("0.030"), Decimal("0.015"))
        report = bt.format_report(summaries, current, 1)
        assert "백테스트" in report
        assert "win_rate" in report

    def test_insufficient_symbols_skipped(self):
        r = fakeredis.FakeRedis()
        bt = Backtester(r, "KR")
        results, summaries = bt.run_sweep(["MISSING"])
        assert results == []
        assert summaries == []
