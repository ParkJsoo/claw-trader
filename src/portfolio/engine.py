"""
Position Engine — Fill 기반 포지션 전이, 평균가, Realized PnL 계산.
"""
from __future__ import annotations

from decimal import Decimal

from domain.models import FillEvent, OrderSide
from portfolio.redis_repo import RedisPositionRepository


class PositionEngine:
    """
    Fill 이벤트를 적용하여 포지션/거래/PnL을 갱신.
    Cash-only 모델: SELL 시 기존 포지션 이상 매도 불가.
    """

    def __init__(self, repo: RedisPositionRepository):
        self.repo = repo

    def _currency(self, market: str) -> str:
        return "KRW" if market == "KR" else "USD"

    def apply_fill(self, fill: FillEvent) -> None:
        """
        Fill 적용: 포지션 갱신, 거래 기록, PnL 갱신.
        멱등: trade_id 기반 중복 시 스킵.
        """
        trade_id = fill.exec_id or fill.trade_id()
        market = fill.market
        symbol = fill.symbol
        currency = self._currency(market)

        # 멱등 기록 (이미 존재하면 스킵)
        if fill.side == OrderSide.BUY:
            inserted = self.repo.record_trade(trade_id, fill, Decimal("0"))
        else:
            pos = self.repo.get_position(market, symbol)
            prev_qty = pos.qty if pos else Decimal("0")
            prev_avg = pos.avg_price if pos else Decimal("0")
            sell_qty = min(fill.qty, prev_qty)
            if sell_qty <= 0:
                self.repo.push_fill_dlq(fill, reason="sell_without_position")
                return
            fee = getattr(fill, "fee", Decimal("0"))
            realized_delta = (fill.price - prev_avg) * sell_qty - fee
            inserted = self.repo.record_trade(trade_id, fill, realized_delta)
        if not inserted:
            return  # duplicate fill skip

        pos = self.repo.get_position(market, symbol)
        prev_qty = pos.qty if pos else Decimal("0")
        prev_avg = pos.avg_price if pos else Decimal("0")
        prev_realized = pos.realized_pnl if pos else Decimal("0")

        if fill.side == OrderSide.BUY:
            self._apply_buy(fill, market, symbol, currency, prev_qty, prev_avg, prev_realized)
        else:
            self._apply_sell(fill, market, symbol, currency, prev_qty, prev_avg, prev_realized)

        self.repo.set_mark_price(fill.market, fill.symbol, fill.price)
        self.repo.recalc_unrealized(fill.market)

    def _apply_buy(
        self,
        fill: FillEvent,
        market: str,
        symbol: str,
        currency: str,
        prev_qty: Decimal,
        prev_avg: Decimal,
        prev_realized: Decimal,
    ) -> None:
        new_qty = prev_qty + fill.qty
        if prev_qty == 0:
            new_avg = fill.price
        else:
            prev_cost = prev_qty * prev_avg
            fill_cost = fill.qty * fill.price
            new_avg = (prev_cost + fill_cost) / new_qty

        self.repo.save_position(
            market=market,
            symbol=symbol,
            qty=new_qty,
            avg_price=new_avg,
            realized_pnl=prev_realized,
            currency=currency,
        )

    def _apply_sell(
        self,
        fill: FillEvent,
        market: str,
        symbol: str,
        currency: str,
        prev_qty: Decimal,
        prev_avg: Decimal,
        prev_realized: Decimal,
    ) -> None:
        # Cash-only: 매도 수량을 보유 수량으로 제한
        sell_qty = min(fill.qty, prev_qty)
        if sell_qty <= 0:
            return

        fee = getattr(fill, "fee", Decimal("0"))
        realized_delta = (fill.price - prev_avg) * sell_qty - fee
        new_realized = prev_realized + realized_delta
        new_qty = prev_qty - sell_qty

        self.repo.save_position(
            market=market,
            symbol=symbol,
            qty=new_qty,
            avg_price=prev_avg if new_qty > 0 else Decimal("0"),
            realized_pnl=new_realized,
            currency=currency,
        )
        self.repo.update_pnl(market, realized_delta)
