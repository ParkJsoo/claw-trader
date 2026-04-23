from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from redis import Redis

from domain.models import Signal
from exchange.base import ExchangeClient
from utils.redis_helpers import get_signal_family_mode, infer_signal_family

_KST = ZoneInfo("Asia/Seoul")
_PENDING_BUY_RESERVE_SEC = max(60, int(os.getenv("RISK_PENDING_BUY_RESERVE_SEC", "900")))


@dataclass
class MarketRiskConfig:
    max_concurrent_positions: int = 5
    daily_loss_limit: Decimal = Decimal("-500")
    allocation_cap_pct: Decimal = Decimal("0.20")


@dataclass
class RiskConfig:
    kr: MarketRiskConfig = field(default_factory=lambda: MarketRiskConfig(daily_loss_limit=Decimal("-500000")))
    us: MarketRiskConfig = field(default_factory=lambda: MarketRiskConfig(daily_loss_limit=Decimal("-500")))
    coin: MarketRiskConfig = field(default_factory=lambda: MarketRiskConfig(daily_loss_limit=Decimal("-50000")))

    def for_market(self, market: str) -> MarketRiskConfig:
        if market == "KR":
            return self.kr
        if market == "US":
            return self.us
        if market == "COIN":
            return self.coin
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
    def _decode(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return str(value) if value is not None else ""

    def _recent_submitted_buy_orders(self, market: str) -> list[dict[str, Any]]:
        now = int(time.time())
        cutoff = now - _PENDING_BUY_RESERVE_SEC
        orders: list[dict[str, Any]] = []

        for meta_key in self.redis.scan_iter(f"claw:order_meta:{market}:*"):
            meta_key_str = self._decode(meta_key)
            order_id = meta_key_str.rsplit(":", 1)[-1]
            status = self.redis.get(f"order:{market}:{order_id}")
            if self._decode(status) != "SUBMITTED":
                continue

            raw = self.redis.hgetall(meta_key_str)
            meta = {self._decode(k): self._decode(v) for k, v in raw.items()}
            if meta.get("side") != "BUY":
                continue

            first_seen_ts = meta.get("first_seen_ts", "")
            if not first_seen_ts.isdigit() or int(first_seen_ts) < cutoff:
                continue

            try:
                qty = Decimal(meta.get("qty") or "0")
                price = Decimal(meta.get("limit_price") or "0")
            except (InvalidOperation, ValueError):
                continue

            orders.append(
                {
                    "order_id": order_id,
                    "symbol": meta.get("symbol", ""),
                    "qty": qty,
                    "price": price,
                    "notional": qty * price,
                    "first_seen_ts": int(first_seen_ts),
                }
            )

        return orders

    @staticmethod
    def _is_truthy(v: Optional[bytes]) -> bool:
        """Redis 값이 pause 활성화 상태인지 판정. 대소문자/다양한 표현 방어."""
        return v is not None and v.decode(errors="replace").strip().lower() in ("true", "1", "yes")

    def _market_pause_bypass_allowed(self, signal: Signal) -> bool:
        """Allow exact opt-in canary families to bypass market-specific pause.

        Global pause always wins. This exists so a COIN alt canary can run while
        the broader COIN market remains paused.
        """
        if signal.direction == "EXIT":
            return True

        family = infer_signal_family(
            getattr(signal, "signal_family", None),
            strategy=getattr(signal, "strategy", None),
            source=getattr(signal, "source", None),
        )
        if not family or not family.startswith("type_b_alt_"):
            return False

        mode = get_signal_family_mode(
            self.redis,
            signal.market,
            family,
            strategy=getattr(signal, "strategy", None),
            source=getattr(signal, "source", None),
            default="off",
        )
        if mode != "live":
            return False

        try:
            raw = self.redis.hget(f"claw:pause_bypass:{signal.market}", family)
        except Exception:
            raw = None
        return self._is_truthy(raw)

    def _rule0_global_pause(self, signal: Signal) -> Optional[RiskDecision]:
        if self._is_truthy(self.redis.get(self.PAUSE_KEY_PRIMARY)):
            return RiskDecision(allow=False, reason="PAUSED", meta={"key": self.PAUSE_KEY_PRIMARY})
        if self._is_truthy(self.redis.get(self.PAUSE_KEY_COMPAT)):
            return RiskDecision(allow=False, reason="PAUSED", meta={"key": self.PAUSE_KEY_COMPAT})
        market_key = f"claw:pause:{signal.market}"
        if self._is_truthy(self.redis.get(market_key)):
            if self._market_pause_bypass_allowed(signal):
                return None
            return RiskDecision(allow=False, reason="PAUSED", meta={"key": market_key, "market": signal.market})
        return None

    def _rule0_signal_family_mode(self, signal: Signal) -> Optional[RiskDecision]:
        if signal.direction == "EXIT":
            return None

        family = infer_signal_family(
            getattr(signal, "signal_family", None),
            strategy=getattr(signal, "strategy", None),
            source=getattr(signal, "source", None),
        )
        if not family:
            return None

        mode = get_signal_family_mode(
            self.redis,
            signal.market,
            family,
            strategy=getattr(signal, "strategy", None),
            source=getattr(signal, "source", None),
        )
        if mode == "live":
            return None

        reason = "SIGNAL_FAMILY_SHADOW_ONLY" if mode == "shadow" else "SIGNAL_FAMILY_DISABLED"
        return RiskDecision(
            allow=False,
            reason=reason,
            meta={
                "market": signal.market,
                "symbol": signal.symbol,
                "signal_family": family,
                "signal_mode": mode,
                "key": f"claw:signal_mode:{signal.market}",
            },
        )

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
        if signal.direction == "EXIT":
            return None
        count = self.redis.scard(f"position_index:{signal.market}") or 0
        pending_buy_count = len(self._recent_submitted_buy_orders(signal.market))
        effective_count = count + pending_buy_count
        if effective_count >= cfg.max_concurrent_positions:
            return RiskDecision(
                allow=False,
                reason="MAX_CONCURRENT_POSITIONS",
                meta={
                    "current": count,
                    "pending_buy_count": pending_buy_count,
                    "effective_current": effective_count,
                    "limit": cfg.max_concurrent_positions,
                },
            )
        return None

    def _rule3_killswitch_pnl(self, signal: Signal, cfg: MarketRiskConfig) -> Optional[RiskDecision]:
        raw = self.redis.hget(f"pnl:{signal.market}", "realized_pnl")
        if raw is None:
            return None
        try:
            realized = Decimal(raw.decode())
        except (InvalidOperation, Exception):
            return RiskDecision(
                allow=False,
                reason="PNL_DATA_CORRUPT",
                meta={"market": signal.market},
            )
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
        if signal.direction == "EXIT":
            return None
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
            pending_orders = self._recent_submitted_buy_orders(signal.market)
            reserved_cash = sum((o["notional"] for o in pending_orders), Decimal("0"))
            effective_available_cash = max(Decimal("0"), snapshot.available_cash - reserved_cash)
            cap = effective_available_cash * cfg.allocation_cap_pct
            if signal.entry.size_cash > cap:
                return RiskDecision(
                    allow=False,
                    reason="ALLOCATION_CAP_EXCEEDED",
                    meta={
                        "size_cash": str(signal.entry.size_cash),
                        "cap": str(cap),
                        "available_cash": str(snapshot.available_cash),
                        "effective_available_cash": str(effective_available_cash),
                        "pending_buy_reserved_cash": str(reserved_cash),
                        "pending_buy_count": len(pending_orders),
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

    def _record_reject_counter(self, market: str, reason: str) -> None:
        """Risk reject 일별 통계 (Phase 11: execution funnel 추적)."""
        today = datetime.now(_KST).strftime("%Y%m%d")
        key = f"risk:reject_count:{market}:{today}"
        try:
            self.redis.hincrby(key, reason, 1)
            self.redis.expire(key, 7 * 86400)
        except Exception:
            pass

    def check(self, signal: Signal) -> RiskDecision:
        cfg = self.cfg.for_market(signal.market)
        rules = [
            lambda: self._rule0_global_pause(signal),
            lambda: self._rule0_signal_family_mode(signal),
            lambda: self._rule1_duplicate_position(signal, cfg),
            lambda: self._rule2_max_concurrent(signal, cfg),
            lambda: self._rule3_killswitch_pnl(signal, cfg),
            lambda: self._rule4_allocation_cap(signal, cfg),
        ]
        for rule in rules:
            decision = rule()
            if decision is not None:
                self._record_reject_counter(signal.market, decision.reason)
                return decision
        return RiskDecision(allow=True, reason="RISK_OK", meta={})  # [수정4] 빈 문자열 → RISK_OK
