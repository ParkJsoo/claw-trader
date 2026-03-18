from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Dict, List, Optional

from ib_insync import IB, Stock, MarketOrder, LimitOrder, util, Trade

from exchange.base import ExchangeClient
from domain.models import (
    PlaceOrderRequest,
    PlaceOrderResult,
    OrderStatus,
    AccountSnapshot,
    OrderType,
    OrderSide,
)


class IbkrClient(ExchangeClient):
    def __init__(self):
        self.host = os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = int(os.getenv("IBKR_PORT", "4001"))
        self.client_id = int(os.getenv("IBKR_CLIENT_ID", "11"))
        self.account_id = os.getenv("IBKR_ACCOUNT_ID")
        self.currency = os.getenv("IBKR_CURRENCY", "USD")

        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not set")

        self.ib = IB()
        self._trade_cache: Dict[str, Trade] = {}

    def _connect(self) -> bool:
        """
        연결 성공 여부를 명확히 반환한다.
        - 성공: True
        - 실패: False
        """
        if self.ib.isConnected():
            return True

        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=2)
            return self.ib.isConnected()
        except Exception:
            return False

    def ping(self) -> bool:
        return self._connect()

    def get_account_snapshot(self) -> AccountSnapshot:
        if not self._connect():
            # 연결 실패면 0 스냅샷(리스크 게이트가 자연스럽게 차단)
            return AccountSnapshot(
                equity=Decimal("0"),
                cash=Decimal("0"),
                available_cash=Decimal("0"),
                currency=self.currency,
            )

        summary = self.ib.accountSummary(self.account_id)

        def get_value(tag: str) -> Decimal:
            for item in summary:
                if item.tag == tag and item.currency == self.currency:
                    try:
                        return Decimal(item.value)
                    except Exception:
                        return Decimal("0")
            return Decimal("0")

        equity = get_value("NetLiquidation")
        cash = get_value("TotalCashValue")
        available = get_value("AvailableFunds")

        return AccountSnapshot(
            equity=equity,
            cash=cash,
            available_cash=available,
            currency=self.currency,
        )

    def place_order(self, request: PlaceOrderRequest) -> PlaceOrderResult:
        if not self._connect():
            return PlaceOrderResult(
                order_id="CONNECT_FAILED",
                status=OrderStatus.REJECTED,
                raw={"reason": "ibkr_connect_failed"},
            )

        if request.qty <= 0:
            return PlaceOrderResult(
                order_id="INVALID_QTY",
                status=OrderStatus.REJECTED,
                raw={"reason": "qty <= 0"},
            )

        contract = Stock(request.symbol, "SMART", self.currency)
        self.ib.qualifyContracts(contract)

        action = "BUY" if request.side == OrderSide.BUY else "SELL"

        if request.order_type == OrderType.MARKET:
            order = MarketOrder(action, float(request.qty))
        else:
            if request.limit_price is None:
                return PlaceOrderResult(
                    order_id="INVALID_PRICE",
                    status=OrderStatus.REJECTED,
                    raw={"reason": "limit_price missing"},
                )

            order = LimitOrder(
                action,
                float(request.qty),
                float(request.limit_price),
            )

        order.orderRef = request.client_order_id

        trade = self.ib.placeOrder(contract, order)
        util.sleep(0.5)

        order_id = str(trade.order.orderId)
        self._trade_cache[order_id] = trade

        st = (trade.orderStatus.status or "").lower()
        if st in ("submitted", "presubmitted"):
            status = OrderStatus.SUBMITTED
        elif st == "filled":
            status = OrderStatus.FILLED
        elif st == "cancelled":
            status = OrderStatus.CANCELED
        elif st == "inactive":
            status = OrderStatus.REJECTED
        else:
            status = OrderStatus.SUBMITTED

        return PlaceOrderResult(
            order_id=order_id,
            status=status,
            raw={"ib_status": trade.orderStatus.status},
        )

    def cancel_order(self, order_id: str) -> bool:
        if not self._connect():
            return False

        trade = self._trade_cache.get(str(order_id))
        if not trade:
            return False

        try:
            self.ib.cancelOrder(trade.order)
            util.sleep(0.5)
            return True
        except Exception:
            return False

    def get_us_holdings(self) -> List[dict]:
        """IBKR portfolio 조회 → [{symbol, qty, avg_price}, ...]

        qty > 0, avg_price > 0 항목만 반환.
        """
        if not self._connect():
            raise RuntimeError("IBKR not connected — cannot fetch holdings")

        try:
            positions = self.ib.portfolio()
        except Exception as e:
            raise RuntimeError(f"IBKR portfolio() failed: {e}") from e

        holdings: List[dict] = []
        for item in positions:
            try:
                symbol = item.contract.symbol
                qty = Decimal(str(item.position))
                avg_price = Decimal(str(item.averageCost))
                if qty > 0 and avg_price > 0:
                    holdings.append({
                        "symbol": symbol,
                        "qty": qty,
                        "avg_price": avg_price,
                    })
            except Exception:
                continue
        return holdings

    def get_order_fills(
        self, order_id: str
    ) -> List[dict]:
        """
        주문의 체결(Fill) 내역 조회.
        Returns: [{"qty", "price", "exec_id", "order_id", "ts_ms", "fee"}, ...]
        exec_id 있으면 멱등에 최우선 사용.
        """
        if not self._connect():
            return []

        trade = self._trade_cache.get(str(order_id))
        if not trade:
            for t in self.ib.trades():
                if str(t.order.orderId) == str(order_id):
                    trade = t
                    break
            if not trade:
                return []

        fills = []
        for f in getattr(trade, "fills", []) or []:
            try:
                ex = getattr(f, "execution", None) or f
                cr = getattr(f, "commissionReport", None)
                qty = Decimal(str(getattr(ex, "shares", 0) or 0))
                price = Decimal(str(getattr(ex, "price", 0) or 0))
                if qty <= 0:
                    continue
                exec_id = getattr(ex, "execId", None) or (getattr(cr, "execId", None) if cr else None)
                exec_id = str(exec_id) if exec_id else None
                fill_order_id = str(getattr(ex, "orderId", 0) or order_id)
                fill_time = getattr(f, "time", None)
                ts_ms = (
                    str(int(fill_time.timestamp() * 1000))
                    if fill_time and hasattr(fill_time, "timestamp")
                    else str(int(time.time() * 1000))
                )
                fee = Decimal("0")
                if cr:
                    fee = Decimal(str(getattr(cr, "commission", 0) or 0))
                fills.append({
                    "qty": qty,
                    "price": price,
                    "exec_id": exec_id,
                    "order_id": fill_order_id,
                    "ts_ms": ts_ms,
                    "fee": fee,
                })
            except Exception:
                continue
        return fills
