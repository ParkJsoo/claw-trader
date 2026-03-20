from __future__ import annotations

import time
import uuid
import json
from decimal import Decimal
from redis import Redis

from domain.models import (
    PlaceOrderRequest,
    OrderStatus,
    OrderType,
    OrderSide,
    Signal,
    FillEvent,
)
from exchange.base import ExchangeClient
from executor.risk import RiskEngine, RiskDecision
from portfolio.redis_repo import RedisPositionRepository
from guards.notifier import send_telegram


def _push_fills_from_executor(
    client: ExchangeClient,
    redis: Redis,
    market: str,
    order_id: str,
    symbol: str,
    side: OrderSide,
    qty: Decimal,
    limit_price: Decimal,
    signal_id: str,
) -> None:
    """
    place_order 즉시 FILLED 시 claw:fill:queue에 push만.
    get_order_fills가 있으면 사용, 없거나 빈 결과 시 1~2회 재조회 후에도 비면 fallback.
    fallback은 limit_price 사용 (시장가/슬리피지 시 실제 체결가와 다를 수 있음) → source=fallback 표시.
    """
    fills_data = []
    if hasattr(client, "get_order_fills"):
        for attempt in range(3):
            fills_data = client.get_order_fills(order_id)
            if fills_data:
                break
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))

    repo = RedisPositionRepository(redis)
    ts_ms = str(int(time.time() * 1000))

    if not fills_data:
        fills_data = [
            {
                "qty": qty,
                "price": limit_price,
                "exec_id": None,
                "ts_ms": ts_ms,
                "fee": 0,
                "source": "fallback",  # get_order_fills 실패 시 limit_price 사용, 감사용 표시
            }
        ]
    for fd in fills_data:
        if isinstance(fd, (list, tuple)):
            fqty, fprice = fd[0], fd[1]
            exec_id, fill_ts_ms, fee_val, src = None, ts_ms, Decimal("0"), None
        else:
            fqty = fd.get("qty", qty)
            fprice = fd.get("price", limit_price)
            exec_id = fd.get("exec_id")
            fill_ts_ms = fd.get("ts_ms") or ts_ms
            fee_val = Decimal(str(fd.get("fee", 0)))
            src = fd.get("source") or "executor"
        fill = FillEvent(
            order_id=order_id,
            market=market,
            symbol=symbol,
            side=side,
            qty=fqty,
            price=fprice,
            exec_id=exec_id,
            ts=fill_ts_ms,
            signal_id=signal_id,
            fee=fee_val,
            source=src,
        )
        repo.push_fill(fill)


class Executor:

    def __init__(self, client: ExchangeClient, redis: Redis, market: str, risk: RiskEngine):
        self.client = client
        self.redis = redis
        self.market = market
        self.risk = risk

    def _reject_key(self, signal_id: str) -> str:
        return f"claw:reject:{self.market}:{signal_id}"

    def _record_reject(self, signal_id: str, reason: str, detail: dict | None = None):
        payload = {
            "reason": reason,
            "ts": str(int(time.time())),
        }

        if detail:
            for k, v in detail.items():
                payload[k] = (
                    json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (dict, list))
                    else str(v)
                )

        key = self._reject_key(signal_id)
        self.redis.hset(key, mapping=payload)
        self.redis.expire(key, 24 * 3600)

    def _lock_signal(self, signal_id: str) -> bool:
        return bool(
            self.redis.set(
                f"claw:idempo:{self.market}:{signal_id}",
                str(int(time.time())),
                nx=True,
                ex=6 * 60 * 60,
            )
        )

    def build_order_from_signal(self, signal: Signal) -> PlaceOrderRequest:
        side = OrderSide.BUY if signal.direction == "LONG" else OrderSide.SELL
        price = signal.entry.price
        qty = signal.entry.size_cash / price

        client_order_id = f"CLAW-{self.market}-{signal.signal_id}-{uuid.uuid4().hex[:6]}"

        return PlaceOrderRequest(
            symbol=signal.symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=price,
            client_order_id=client_order_id,
        )

    def execute_signal(self, signal: Signal) -> OrderStatus:
        decision = self.risk.check(signal)
        if not decision.allow:
            self._record_reject(signal.signal_id, decision.reason, decision.meta)
            return OrderStatus.RISK_REJECTED

        if not self._lock_signal(signal.signal_id):
            self._record_reject(signal.signal_id, "idempotency_duplicate")
            return OrderStatus.RISK_REJECTED

        try:
            req = self.build_order_from_signal(signal)
        except Exception as e:
            self._record_reject(signal.signal_id, "build_order_failed", {"error": str(e)})
            return OrderStatus.ERROR

        result = self.client.place_order(req)

        # C2: BUY 주문 제출 시 buy_pending 키 설정 (position_exit_runner race condition 방지)
        if req.side == OrderSide.BUY and result.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED):
            pending_key = f"claw:buy_pending:{self.market}:{req.symbol}"
            self.redis.set(pending_key, "1", ex=120)

        order_key = f"order:{self.market}:{result.order_id}"
        self.redis.set(order_key, result.status.value, ex=7 * 86400)

        # Portfolio Engine용 주문 메타 (Fill 시 position 갱신에 사용)
        signal_id = signal.signal_id
        meta_key = f"claw:order_meta:{self.market}:{result.order_id}"
        self.redis.hset(
            meta_key,
            mapping={
                "symbol": req.symbol,
                "side": req.side.value,
                "qty": str(req.qty),
                "limit_price": str(req.limit_price),
                "signal_id": signal_id,
                "stop_pct": str(signal.stop_pct) if signal.stop_pct else "0.02",
                "take_pct": str(signal.take_pct) if signal.take_pct else "0.02",
            },
        )
        self.redis.expire(meta_key, 24 * 3600)

        # 즉시 체결된 경우 (place_order 반환 시 FILLED) Fill 큐에 push만
        if result.status == OrderStatus.FILLED:
            _push_fills_from_executor(
                self.client, self.redis, self.market, result.order_id,
                req.symbol, req.side, req.qty, req.limit_price, signal_id,
            )

        # BUY 주문접수/체결 알림
        if result.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED):
            try:
                currency = "KRW" if self.market == "KR" else "USD"
                side_str = req.side.value
                send_telegram(
                    f"[CLAW] {side_str} 주문접수\n"
                    f"market={self.market} symbol={req.symbol}\n"
                    f"qty={req.qty} price={req.limit_price} {currency}\n"
                    f"order_id={result.order_id}"
                )
            except Exception:
                pass

        if result.status in (OrderStatus.REJECTED, OrderStatus.ERROR):
            self._record_reject(signal.signal_id, "broker_rejected", {
                "order_id": result.order_id,
                "raw": result.raw,
            })

        return result.status

    def cancel(self, order_id: str) -> bool:
        # 소유권 검증: 이 시스템에서 생성한 주문만 취소 허용
        meta_key = f"claw:order_meta:{self.market}:{order_id}"
        if not self.redis.exists(meta_key):
            self._record_reject(f"CANCEL-{order_id}", "unknown_order")
            return False

        ok = self.client.cancel_order(order_id)

        if ok:
            key = f"order:{self.market}:{order_id}"
            self.redis.set(key, "CANCELED", ex=7 * 86400)
        else:
            self._record_reject(f"CANCEL-{order_id}", "cancel_failed")

        return ok
