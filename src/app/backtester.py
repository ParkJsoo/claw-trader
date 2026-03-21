"""backtester — mark_hist 기반 파라미터 스윕 시뮬레이터.

mark_hist:{market}:{symbol}의 가격 시리즈에서
stop_pct / take_pct / trail_pct 파라미터 조합별 가상 체결 시뮬레이션.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
from itertools import product

from redis import Redis

from utils.redis_helpers import today_kst


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class ParamSet:
    stop_pct: Decimal
    take_pct: Decimal
    trail_pct: Decimal

    def label(self) -> str:
        return f"S{self.stop_pct*100:.1f}_T{self.take_pct*100:.1f}_TR{self.trail_pct*100:.1f}"


@dataclass
class SimResult:
    param: ParamSet
    symbol: str
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str  # "stop_loss" | "take_profit" | "trailing_stop" | "time_limit" | "end_of_data"
    pnl_pct: Decimal
    hold_ticks: int

    @property
    def is_win(self) -> bool:
        return self.pnl_pct > 0


@dataclass
class SweepSummary:
    param: ParamSet
    total: int
    wins: int
    gross_profit: Decimal
    gross_loss: Decimal
    avg_pnl_pct: Decimal
    win_rate: Decimal
    profit_factor: Decimal
    exit_reasons: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 핵심 시뮬레이터
# ---------------------------------------------------------------------------

_TIME_LIMIT_TICKS = 600   # 30분 @ 3s 폴링
_TIME_LIMIT_MAX_TICKS = 1200  # 60분 (수익 중 연장)


def _parse_mark_hist(raw_list: list[bytes]) -> list[tuple[int, Decimal]]:
    """mark_hist LIST → [(ts_ms, price), ...] 오름차순."""
    result = []
    for raw in raw_list:
        try:
            s = raw.decode() if isinstance(raw, bytes) else raw
            ts_str, price_str = s.split(":", 1)
            result.append((int(ts_str), Decimal(price_str)))
        except Exception:
            continue
    result.sort(key=lambda x: x[0])
    return result


def simulate_one(
    prices: list[tuple[int, Decimal]],
    symbol: str,
    params: ParamSet,
) -> Optional[SimResult]:
    """단일 가격 시리즈 + 파라미터 조합 시뮬레이션.

    prices: [(ts_ms, price), ...] 오름차순.
    최소 10개 미만이면 None 반환 (데이터 부족).
    """
    if len(prices) < 10:
        return None

    entry_price = prices[0][1]
    if entry_price <= 0:
        return None

    stop_floor = entry_price * (1 - params.stop_pct)
    take_price = entry_price * (1 + params.take_pct)

    hwm = entry_price

    for tick_idx, (ts_ms, mark) in enumerate(prices[1:], start=1):
        # HWM 갱신
        if mark > hwm:
            hwm = mark

        # Effective stop = max(static stop, trailing stop from HWM)
        trail_stop = hwm * (1 - params.trail_pct)
        effective_stop = max(stop_floor, trail_stop)

        # Stop loss (trailing 포함)
        if mark <= effective_stop:
            reason = "trailing_stop" if trail_stop > stop_floor else "stop_loss"
            pnl_pct = (mark - entry_price) / entry_price
            return SimResult(
                param=params, symbol=symbol,
                entry_price=entry_price, exit_price=mark,
                exit_reason=reason,
                pnl_pct=pnl_pct.quantize(Decimal("0.00001")),
                hold_ticks=tick_idx,
            )

        # Take profit
        if mark >= take_price:
            pnl_pct = (mark - entry_price) / entry_price
            return SimResult(
                param=params, symbol=symbol,
                entry_price=entry_price, exit_price=mark,
                exit_reason="take_profit",
                pnl_pct=pnl_pct.quantize(Decimal("0.00001")),
                hold_ticks=tick_idx,
            )

        # Time limit
        is_profitable = mark > entry_price
        if tick_idx >= _TIME_LIMIT_TICKS:
            if not is_profitable or tick_idx >= _TIME_LIMIT_MAX_TICKS:
                pnl_pct = (mark - entry_price) / entry_price
                return SimResult(
                    param=params, symbol=symbol,
                    entry_price=entry_price, exit_price=mark,
                    exit_reason="time_limit",
                    pnl_pct=pnl_pct.quantize(Decimal("0.00001")),
                    hold_ticks=tick_idx,
                )

    # 데이터 소진 (미청산)
    last_price = prices[-1][1]
    pnl_pct = (last_price - entry_price) / entry_price
    return SimResult(
        param=params, symbol=symbol,
        entry_price=entry_price, exit_price=last_price,
        exit_reason="end_of_data",
        pnl_pct=pnl_pct.quantize(Decimal("0.00001")),
        hold_ticks=len(prices) - 1,
    )


def summarize_results(results: list[SimResult], param: ParamSet) -> SweepSummary:
    """파라미터 조합별 결과 집계."""
    subset = [r for r in results if r.param.label() == param.label()]
    if not subset:
        return SweepSummary(
            param=param, total=0, wins=0,
            gross_profit=Decimal("0"), gross_loss=Decimal("0"),
            avg_pnl_pct=Decimal("0"), win_rate=Decimal("0"),
            profit_factor=Decimal("0"),
        )

    wins = [r for r in subset if r.pnl_pct > 0]
    losses = [r for r in subset if r.pnl_pct <= 0]

    gross_profit = sum((r.pnl_pct for r in wins), Decimal("0"))
    gross_loss = abs(sum((r.pnl_pct for r in losses), Decimal("0")))
    avg_pnl_pct = sum((r.pnl_pct for r in subset), Decimal("0")) / len(subset)
    win_rate = Decimal(len(wins)) / Decimal(len(subset))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else Decimal("99")

    exit_reasons: dict[str, int] = {}
    for r in subset:
        exit_reasons[r.exit_reason] = exit_reasons.get(r.exit_reason, 0) + 1

    return SweepSummary(
        param=param,
        total=len(subset),
        wins=len(wins),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        avg_pnl_pct=avg_pnl_pct.quantize(Decimal("0.00001")),
        win_rate=win_rate.quantize(Decimal("0.001")),
        profit_factor=profit_factor.quantize(Decimal("0.01")),
        exit_reasons=exit_reasons,
    )


# ---------------------------------------------------------------------------
# 스윕 실행기
# ---------------------------------------------------------------------------

# 기본 파라미터 그리드
_DEFAULT_STOP_PCTS = [Decimal("0.010"), Decimal("0.015"), Decimal("0.020")]
_DEFAULT_TAKE_PCTS = [Decimal("0.020"), Decimal("0.030"), Decimal("0.040")]
_DEFAULT_TRAIL_PCTS = [Decimal("0.010"), Decimal("0.015"), Decimal("0.020")]


class Backtester:
    def __init__(self, r: Redis, market: str):
        self.r = r
        self.market = market

    def load_prices(self, symbol: str) -> list[tuple[int, Decimal]]:
        """mark_hist에서 가격 시리즈 로드."""
        raw_list = self.r.lrange(f"mark_hist:{self.market}:{symbol}", 0, -1)
        return _parse_mark_hist(raw_list)

    def run_sweep(
        self,
        symbols: list[str],
        stop_pcts: Optional[list[Decimal]] = None,
        take_pcts: Optional[list[Decimal]] = None,
        trail_pcts: Optional[list[Decimal]] = None,
    ) -> tuple[list[SimResult], list[SweepSummary]]:
        """모든 심볼 × 파라미터 조합 시뮬레이션.

        Returns: (results, summaries) — summaries는 profit_factor 내림차순 정렬.
        """
        stop_pcts = stop_pcts or _DEFAULT_STOP_PCTS
        take_pcts = take_pcts or _DEFAULT_TAKE_PCTS
        trail_pcts = trail_pcts or _DEFAULT_TRAIL_PCTS

        param_sets = [
            ParamSet(s, t, tr)
            for s, t, tr in product(stop_pcts, take_pcts, trail_pcts)
        ]

        all_results: list[SimResult] = []
        skipped = 0

        for symbol in symbols:
            prices = self.load_prices(symbol)
            if len(prices) < 10:
                skipped += 1
                continue

            for params in param_sets:
                result = simulate_one(prices, symbol, params)
                if result:
                    all_results.append(result)

        summaries = [summarize_results(all_results, p) for p in param_sets]
        summaries = [s for s in summaries if s.total > 0]
        summaries.sort(key=lambda s: s.profit_factor, reverse=True)

        return all_results, summaries

    def save_results(self, summaries: list[SweepSummary], ttl: int = 90 * 86400) -> None:
        """결과를 Redis에 저장. backtest:result:{market}:{date}"""
        today = today_kst()
        key = f"backtest:result:{self.market}:{today}"
        payload = []
        for s in summaries[:20]:  # 상위 20개만 저장
            payload.append({
                "param": s.param.label(),
                "stop_pct": str(s.param.stop_pct),
                "take_pct": str(s.param.take_pct),
                "trail_pct": str(s.param.trail_pct),
                "total": s.total,
                "wins": s.wins,
                "win_rate": str(s.win_rate),
                "profit_factor": str(s.profit_factor),
                "avg_pnl_pct": str(s.avg_pnl_pct),
                "exit_reasons": s.exit_reasons,
            })
        self.r.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl)

    def format_report(
        self,
        summaries: list[SweepSummary],
        current_params: ParamSet,
        symbols_count: int,
    ) -> str:
        """TG용 리포트 포맷."""
        if not summaries:
            return f"[CLAW] 백테스트: 데이터 부족 (심볼 {symbols_count}개)"

        best = summaries[0]
        # 현재 파라미터와 가장 가까운 조합 찾기
        current_label = current_params.label()
        current_summary = next(
            (s for s in summaries if s.param.label() == current_label), None
        )

        today = today_kst()
        lines = [
            f"[CLAW] 백테스트 결과 ({self.market}, {today})",
            f"심볼 {symbols_count}개 | 시뮬레이션 {best.total}건/파라미터",
            "",
            "최적 파라미터:",
            f"  stop={best.param.stop_pct*100:.1f}% / take={best.param.take_pct*100:.1f}% / trail={best.param.trail_pct*100:.1f}%",
            f"  win_rate={best.win_rate*100:.1f}% | profit_factor={best.profit_factor}",
            f"  avg_pnl={best.avg_pnl_pct*100:.3f}%",
        ]

        if current_summary:
            lines += [
                "",
                "현재 파라미터:",
                f"  stop={current_params.stop_pct*100:.1f}% / take={current_params.take_pct*100:.1f}% / trail={current_params.trail_pct*100:.1f}%",
                f"  win_rate={current_summary.win_rate*100:.1f}% | profit_factor={current_summary.profit_factor}",
                f"  rank={summaries.index(current_summary)+1}/{len(summaries)}위",
            ]

        # 상위 3개
        lines += ["", "상위 3개:"]
        for i, s in enumerate(summaries[:3], 1):
            lines.append(
                f"  {i}. stop={s.param.stop_pct*100:.1f}%/take={s.param.take_pct*100:.1f}%/trail={s.param.trail_pct*100:.1f}%"
                f" -> WR={s.win_rate*100:.0f}% PF={s.profit_factor}"
            )

        lines.append("\n⚠️ mark_hist 첫 데이터 진입 가정. 실제 진입 가격과 차이 있을 수 있음.")
        return "\n".join(lines)
