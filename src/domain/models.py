from __future__ import annotations

from enum import Enum
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal, Any, Dict


# =========================
# 공통 주문 타입 (KR/US)
# =========================

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class PlaceOrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    qty: Decimal
    order_type: OrderType
    limit_price: Optional[Decimal] = None
    tif: TimeInForce = TimeInForce.DAY
    client_order_id: str


class PlaceOrderResult(BaseModel):
    order_id: str
    status: OrderStatus
    raw: Optional[Dict[str, Any]] = None  # 브로커 원본 응답(디버깅/감사)


class AccountSnapshot(BaseModel):
    equity: Decimal
    cash: Decimal
    available_cash: Decimal
    currency: str


class Position(BaseModel):
    symbol: str
    qty: Decimal
    avg_price: Decimal
    unrealized_pnl: Decimal


# =========================
# Signal 스키마 (Strategy -> Executor)
# signal.schema.json 기반
# =========================

Market = Literal["KR", "US"]
Direction = Literal["LONG", "EXIT"]


class SignalEntry(BaseModel):
    price: Decimal
    size_cash: Decimal  # 현금 기준 사이징 (레버리지 금지 철학과 정합)


class SignalStop(BaseModel):
    price: Decimal


class Signal(BaseModel):
    signal_id: str
    ts: str  # ISO8601 문자열 유지(나중에 datetime으로 바꿔도 됨)
    market: Market
    symbol: str
    direction: Direction
    entry: SignalEntry
    stop: SignalStop


# =========================
# (선택) 주문 이벤트 스키마 (관측/로깅)
# order_event.schema.json 기반 최소 필드
# =========================

class OrderEvent(BaseModel):
    order_id: str
    status: str
    ts: str
    market: Optional[Market] = None
    signal_id: Optional[str] = None
    retry_count: Optional[int] = 0
    emergency_market_used: Optional[bool] = False
    raw: Optional[Dict[str, Any]] = None


# =========================
# Portfolio / Position Engine (PHASE 4)
# =========================

class FillEvent(BaseModel):
    """체결(Fill) 이벤트 — Portfolio Engine 입력"""
    order_id: Optional[str] = None
    market: Market
    symbol: str
    side: OrderSide
    qty: Decimal
    price: Decimal
    exec_id: Optional[str] = None  # 브로커 execution ID (멱등키, 최우선)
    ts: str  # unix ts ms string only (예: "1699123456789")
    signal_id: Optional[str] = None
    retry: int = 0
    fee: Decimal = Field(default=Decimal("0"), description="수수료")
    source: Optional[str] = None  # "fallback" 등 감사/디버깅용

    @field_validator("ts")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError(f"ts must be a numeric string (unix ms), got: {v!r}")
        if not (12 <= len(v) <= 14):
            raise ValueError(
                f"ts length must be 12-14 digits (unix ms), got length {len(v)}: {v!r}"
            )
        return v

    def _fmt_decimal(self, v: Decimal) -> str:
        """Decimal → 고정 포맷 문자열 (KR exec_id 없을 때 fallback 멱등용)."""
        if v == 0:
            return "0"
        s = format(v.normalize(), "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    def trade_id(self) -> str:
        """결정론적 trade_id — exec_id 있으면 사용, 없으면 fallback."""
        if self.exec_id:
            return self.exec_id
        ts_ms = (self.ts or "0").strip()
        qty_s = self._fmt_decimal(self.qty)
        price_s = self._fmt_decimal(self.price)
        oid = (self.order_id or "na")[:64]
        return (
            f"{self.market}:{self.symbol}:{self.side.value}:{ts_ms}:"
            f"{qty_s}:{price_s}:{oid}"
        )


class PositionState(BaseModel):
    """Redis position:{market}:{symbol} 스냅샷"""
    symbol: str
    qty: Decimal
    avg_price: Decimal
    realized_pnl: Decimal = Decimal("0")
    updated_ts: str
    currency: str
    unrealized_pnl: Optional[Decimal] = None  # 현재가 있을 때만
