from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

# 취소된 종목 재진입 방지 cooldown (기본 1시간)
_CANCEL_COOLDOWN_SEC: int = int(os.getenv("CANCEL_COOLDOWN_SEC", "3600"))

import redis

from domain.models import FillEvent, OrderSide
from exchange.ibkr.client import IbkrClient
from exchange.kis.client import KisClient
from portfolio.engine import PositionEngine
from portfolio.redis_repo import RedisPositionRepository


@dataclass
class WatcherConfig:
    redis_url: str
    poll_interval_sec: float = 1.0
    ttl_cancel_sec: int = 15          # 미체결 자동취소 TTL (단타 기준 15초 추천)
    meta_ttl_sec: int = 24 * 3600     # 메타/리젝트 보관 24h
    scan_count: int = 200             # SCAN 배치 크기


class OrderWatcher:
    """
    Redis 기반 주문 감시자(v1):
    - order:{MARKET}:{order_id} 키를 스캔
    - SUBMITTED 주문을 대상으로:
        - US(IBKR): 실제 주문 상태를 조회해서 Redis 상태 갱신
        - KR(KIS): v1은 상태조회 생략(다음 단계에서 체결조회 API 추가). TTL 초과 시 취소만 수행
    - TTL 초과 시 자동취소 시도
    """

    def __init__(self, cfg: WatcherConfig):
        self.cfg = cfg
        self.r = redis.from_url(cfg.redis_url)

        # 브로커 클라이언트(Watcher는 "조회/취소" 용도)
        self.ibkr = IbkrClient() if os.getenv("IBKR_ACCOUNT_ID") else None
        self.kis = KisClient()

        # Portfolio Engine (Fill → Position 갱신)
        repo = RedisPositionRepository(self.r)
        self.position_engine = PositionEngine(repo)

    # -------------------------
    # Redis helpers
    # -------------------------

    def _order_key(self, market: str, order_id: str) -> str:
        return f"order:{market}:{order_id}"

    def _meta_key(self, market: str, order_id: str) -> str:
        return f"claw:order_meta:{market}:{order_id}"

    def _reject_key(self, market: str, tag: str) -> str:
        # tag에는 order_id 또는 임의 식별자 사용
        return f"claw:reject:{market}:{tag}"

    def _ensure_meta(self, market: str, order_id: str) -> int:
        """
        meta가 없으면 first_seen_ts를 지금으로 세팅하고 반환.
        있으면 existing first_seen_ts 반환.
        """
        mk = self._meta_key(market, order_id)
        first = self.r.hget(mk, "first_seen_ts")
        if first is not None:
            try:
                return int(first)
            except Exception:
                pass

        now = int(time.time())
        self.r.hset(mk, mapping={"first_seen_ts": str(now)})
        self.r.expire(mk, self.cfg.meta_ttl_sec)
        return now

    def _set_order_status(self, market: str, order_id: str, status: str) -> None:
        self.r.set(self._order_key(market, order_id), status)

    def _record_reject(self, market: str, tag: str, reason: str, detail: dict) -> None:
        rk = self._reject_key(market, tag)
        payload = {"reason": reason, "ts": str(int(time.time()))}
        for k, v in detail.items():
            payload[k] = str(v)
        self.r.hset(rk, mapping=payload)
        self.r.expire(rk, self.cfg.meta_ttl_sec)

    # -------------------------
    # IBKR status (US)
    # -------------------------

    def _ibkr_query_status(self, order_id: str) -> Optional[str]:
        """
        IBKR의 orderId로 상태 조회.
        반환: "SUBMITTED" | "FILLED" | "CANCELED" | "REJECTED" | None(조회불가)
        """
        if self.ibkr is None or not self.ibkr.ping():
            return None

        try:
            self.ibkr.ib.reqAllOpenOrders()
            time.sleep(0.1)

            trades = list(self.ibkr.ib.trades())
            for t in trades:
                try:
                    if str(t.order.orderId) != str(order_id):
                        continue
                except Exception:
                    continue

                st = (t.orderStatus.status or "").lower()

                if st in ("submitted", "presubmitted"):
                    return "SUBMITTED"
                if st == "filled":
                    return "FILLED"
                if st == "cancelled":
                    return "CANCELED"
                if st == "inactive":
                    return "REJECTED"

                # 나머지는 v1에서는 SUBMITTED 취급
                return "SUBMITTED"

            return None
        except Exception:
            return None

    # -------------------------
    # Cancel
    # -------------------------

    def _cancel_order(self, market: str, order_id: str) -> bool:
        """
        브로커에 취소 요청.
        """
        if market == "US":
            if self.ibkr is None:
                return False
            return self.ibkr.cancel_order(order_id)
        if market == "KR":
            return self.kis.cancel_order(order_id)
        return False

    # -------------------------
    # Portfolio Engine (Fill 처리)
    # -------------------------

    def _process_fill_on_filled(self, market: str, order_id: str) -> None:
        """
        FILLED 상태 감지 시 Fill 이벤트 생성 후 Position Engine에 반영.
        """
        mk = self._meta_key(market, order_id)
        meta = self.r.hgetall(mk)
        if not meta:
            return
        try:
            def d(k: str) -> str:
                return meta.get(k.encode(), b"").decode()
            symbol = d("symbol")
            side_str = d("side")
            qty_str = d("qty")
            price_str = d("limit_price")
            signal_id = d("signal_id") or None
            if not symbol or not side_str or not qty_str or not price_str:
                return
            qty = Decimal(qty_str)
            price = Decimal(price_str)
            side = OrderSide(side_str)
        except Exception:
            return

        # US: 브로커 Fill 조회 (exec_id 포함), KR: order_meta 기준
        fills_data: list[dict] = []
        if market == "US" and hasattr(self.ibkr, "get_order_fills"):
            raw = self.ibkr.get_order_fills(order_id)
            for r in raw or []:
                if isinstance(r, dict):
                    fills_data.append(r)
                elif isinstance(r, (list, tuple)):
                    fills_data.append({"qty": r[0], "price": r[1], "exec_id": None})
        if not fills_data:
            fills_data = [{"qty": qty, "price": price, "exec_id": None}]

        ts_ms = str(int(time.time() * 1000))
        for fd in fills_data:
            fqty = fd.get("qty", qty)
            fprice = fd.get("price", price)
            exec_id = fd.get("exec_id")
            ts_val = fd.get("ts_ms") or ts_ms
            fee_val = Decimal(str(fd.get("fee", 0)))
            fill = FillEvent(
                order_id=order_id,
                market=market,
                symbol=symbol,
                side=side,
                qty=fqty,
                price=fprice,
                exec_id=exec_id,
                ts=ts_val,
                signal_id=signal_id,
                fee=fee_val,
                source="watcher",
            )
            try:
                self.position_engine.apply_fill(fill)
            except Exception as e:
                self._record_reject(
                    market=market, tag=f"FILL-{order_id}",
                    reason="position_update_failed", detail={"error": str(e)},
                )

    # -------------------------
    # Main loop
    # -------------------------

    def _iter_order_keys(self) -> Tuple[str, str]:
        """
        yield: (market, order_id)
        """
        # US
        for k in self.r.scan_iter(match="order:US:*", count=self.cfg.scan_count):
            try:
                s = k.decode()
                _, market, order_id = s.split(":", 2)
                yield market, order_id
            except Exception:
                continue

        # KR
        for k in self.r.scan_iter(match="order:KR:*", count=self.cfg.scan_count):
            try:
                s = k.decode()
                _, market, order_id = s.split(":", 2)
                yield market, order_id
            except Exception:
                continue

    def run_forever(self):
        # Redis 연결 확인
        self.r.ping()

        print(
            f"[watcher] started | poll={self.cfg.poll_interval_sec}s | ttl_cancel={self.cfg.ttl_cancel_sec}s"
        )

        while True:
            now = int(time.time())

            for market, order_id in self._iter_order_keys():
                status = self.r.get(self._order_key(market, order_id))
                if status is None:
                    continue

                status_str = status.decode()

                # v1: SUBMITTED만 적극 감시
                if status_str != "SUBMITTED":
                    continue

                first_seen = self._ensure_meta(market, order_id)
                age = now - first_seen

                # 1) US는 실제 상태 조회로 갱신
                if market == "US":
                    real = self._ibkr_query_status(order_id)
                    if real in ("FILLED", "CANCELED", "REJECTED"):
                        if real == "FILLED":
                            self._process_fill_on_filled(market, order_id)
                        self._set_order_status(market, order_id, real)
                        continue

                # 2) TTL 초과면 자동 취소 (SELL 주문은 손절/익절이므로 취소 제외)
                if age >= self.cfg.ttl_cancel_sec:
                    mk = self._meta_key(market, order_id)
                    meta = self.r.hgetall(mk)
                    side = meta.get(b"side", b"").decode() if meta else ""
                    if side == "SELL":
                        continue  # 매도 주문은 TTL 취소 안 함
                    ok = self._cancel_order(market, order_id)
                    if ok:
                        self._set_order_status(market, order_id, "CANCELED")
                        # 취소된 종목은 일정 시간 재진입 금지
                        if meta:
                            sym = meta.get(b"symbol", b"").decode()
                            if sym:
                                ck = f"consensus:symbol_cooldown:{market}:{sym}"
                                self.r.set(ck, "1", ex=_CANCEL_COOLDOWN_SEC)
                    else:
                        self._record_reject(
                            market=market,
                            tag=f"CANCEL-{order_id}",
                            reason="cancel_failed",
                            detail={"order_id": order_id, "age_sec": age},
                        )

            time.sleep(self.cfg.poll_interval_sec)
