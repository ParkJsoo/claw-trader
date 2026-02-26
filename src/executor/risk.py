from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from pydantic import BaseModel, Field
from redis import Redis

from domain.models import Signal
from exchange.base import ExchangeClient


@dataclass
class MarketRiskConfig:
    max_concurrent_positions: int = 5
    daily_loss_limit: Decimal = Decimal("-500")
    allocation_cap_pct: Decimal = Decimal("0.20")


@dataclass
class RiskConfig:
    kr: MarketRiskConfig = field(default_factory=lambda: MarketRiskConfig(daily_loss_limit=Decimal("-500000")))
    us: MarketRiskConfig = field(default_factory=lambda: MarketRiskConfig(daily_loss_limit=Decimal("-500")))

    def for_market(self, market: str) -> MarketRiskConfig:
        if market == "KR":
            return self.kr
        if market == "US":
            return self.us
        raise ValueError(f"Unknown market: {market}")


class RiskDecision(BaseModel):
    allow: bool
    reason: str
    meta: dict[str, Any] = Field(default_factory=dict)  # [수정1] mutable default 명시


class RiskEngine:
    PAUSE_KEY_PRIMARY = "claw:pause:global"
    PAUSE_KEY_COMPAT = "trading:paused"
    PAUSE_REASON_KEY = "claw:pause:reason"
    PAUSE_META_KEY = "claw:pause:meta"

    def __init__(self, redis: Redis, cfg: RiskConfig, client: ExchangeClient):
        self.redis = redis
        self.cfg = cfg
        self.client = client

    @staticmethod
    def _is_truthy(v: Optional[bytes]) -> bool:
        """Redis 값이 pause 활성화 상태인지 판정. 대소문자/다양한 표현 방어."""
        return v is not None and v.decode(errors="replace").strip().lower() in ("true", "1", "yes")

    def _rule0_global_pause(self) -> Optional[RiskDecision]:
        if self._is_truthy(self.redis.get(self.PAUSE_KEY_PRIMARY)):
            return RiskDecision(allow=False, reason="PAUSED", meta={"key": self.PAUSE_KEY_PRIMARY})
        if self._is_truthy(self.redis.get(self.PAUSE_KEY_COMPAT)):
            return RiskDecision(allow=False, reason="PAUSED", meta={"key": self.PAUSE_KEY_COMPAT})
        return None

    def _rule1_duplicate_position(self, signal: Signal, cfg: MarketRiskConfig) -> Optional[RiskDecision]:
        if signal.direction == "EXIT":
            return None
        raw = self.redis.hget(f"position:{signal.market}:{signal.symbol}", "qty")
        if raw is None:
            return None
        try:
            qty = Decimal(raw.decode())
        except (InvalidOperation, Exception):
            # [수정5] corrupt data → 조용히 통과 대신 명시적 거부
            return RiskDecision(
                allow=False,
                reason="POSITION_DATA_CORRUPT",
                meta={"symbol": signal.symbol, "raw_qty": raw.decode(errors="replace")},
            )
        if qty > 0:
            return RiskDecision(
                allow=False,
                reason="DUPLICATE_POSITION",
                meta={"symbol": signal.symbol, "existing_qty": str(qty)},
            )
        return None

    def _rule2_max_concurrent(self, signal: Signal, cfg: MarketRiskConfig) -> Optional[RiskDecision]:
        count = self.redis.scard(f"position_index:{signal.market}") or 0
        if count >= cfg.max_concurrent_positions:
            return RiskDecision(
                allow=False,
                reason="MAX_CONCURRENT_POSITIONS",
                meta={"current": count, "limit": cfg.max_concurrent_positions},
            )
        return None

    def _rule3_killswitch_pnl(self, signal: Signal, cfg: MarketRiskConfig) -> Optional[RiskDecision]:
        raw = self.redis.hget(f"pnl:{signal.market}", "realized_pnl")
        if raw is None:
            return None
        try:
            realized = Decimal(raw.decode())
        except (InvalidOperation, Exception):
            return None
        if realized <= cfg.daily_loss_limit:
            meta = {
                "realized_pnl": str(realized),
                "limit": str(cfg.daily_loss_limit),
                "market": signal.market,
            }
            self.apply_killswitch("KILLSWITCH_REALIZED", meta)
            return RiskDecision(allow=False, reason="KILLSWITCH_REALIZED", meta=meta)
        return None

    def _rule4_allocation_cap(self, signal: Signal, cfg: MarketRiskConfig) -> Optional[RiskDecision]:
        try:
            snapshot = self.client.get_account_snapshot()
            if snapshot.available_cash <= 0:
                return RiskDecision(
                    allow=False,
                    reason="ACCOUNT_SNAPSHOT_ERROR",
                    meta={
                        "available_cash": str(snapshot.available_cash),
                        "equity": str(snapshot.equity),
                        "cash": str(snapshot.cash),
                        "currency": snapshot.currency,
                        "detail": "available_cash <= 0 (broker disconnected or incomplete snapshot)",
                    },
                )
            cap = snapshot.available_cash * cfg.allocation_cap_pct
            if signal.entry.size_cash > cap:
                return RiskDecision(
                    allow=False,
                    reason="ALLOCATION_CAP_EXCEEDED",
                    meta={
                        "size_cash": str(signal.entry.size_cash),
                        "cap": str(cap),
                        "available_cash": str(snapshot.available_cash),
                    },
                )
        except Exception as e:
            return RiskDecision(
                allow=False,
                reason="ACCOUNT_SNAPSHOT_ERROR",
                meta={
                    "error": str(e),
                    "market": signal.market,
                    "symbol": signal.symbol,
                    "size_cash": str(signal.entry.size_cash),
                },
            )
        return None

    def apply_killswitch(self, reason: str, meta: dict) -> None:
        # [수정2] SET NX 결과 확인 → 첫 발동에만 reason/meta 기록 (원자성 보장)
        set_ok = self.redis.set(self.PAUSE_KEY_PRIMARY, "true", nx=True)
        if set_ok:
            ts_ms = str(int(time.time() * 1000))  # ms 표준 통일
            pipe = self.redis.pipeline()
            pipe.set(self.PAUSE_REASON_KEY, reason)
            pipe.hset(self.PAUSE_META_KEY, mapping={**meta, "ts_ms": ts_ms})
            # 감사용 스냅샷 (market별, 24h TTL)
            detail_key = f"claw:killswitch:{meta.get('market', 'unknown')}"
            blob = json.dumps({"reason": reason, "meta": meta, "ts_ms": ts_ms})
            pipe.set(detail_key, blob, nx=True, ex=86400)
            pipe.execute()

    def check(self, signal: Signal) -> RiskDecision:
        cfg = self.cfg.for_market(signal.market)
        rules = [
            self._rule0_global_pause,
            lambda: self._rule1_duplicate_position(signal, cfg),
            lambda: self._rule2_max_concurrent(signal, cfg),
            lambda: self._rule3_killswitch_pnl(signal, cfg),
            lambda: self._rule4_allocation_cap(signal, cfg),
        ]
        for rule in rules:
            decision = rule()
            if decision is not None:
                return decision
        return RiskDecision(allow=True, reason="RISK_OK", meta={})  # [수정4] 빈 문자열 → RISK_OK
