"""
Redis 기반 Position / PnL / Trade 저장소.
- position:{market}:{symbol}
- pnl:{market}
- trade:{market}:{trade_id}
- position_index:{market}
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Optional

from redis import Redis

from domain.models import FillEvent, PositionState, OrderSide, Market


def _decode(v) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v) if v is not None else ""


def _hgetall_str(r: Redis, key: str) -> dict[str, str]:
    raw = r.hgetall(key)
    if not raw:
        return {}
    return {_decode(k): _decode(v) for k, v in raw.items()}


def _to_ts_ms(ts: str) -> str:
    """
    fill.ts를 ms 문자열로 정규화.
    - 숫자 문자열: 길이 13+ → ms, 10 내외 → 초*1000
    - ISO8601 → datetime 파싱 후 ms
    - 파싱 실패 시 현재 ms fallback
    """
    if not ts or not ts.strip():
        return str(int(time.time() * 1000))
    s = ts.strip()
    if re.match(r"^\d+\.?\d*$", s):
        if len(s) >= 13:
            return s.split(".")[0][:13]
        return str(int(float(s) * 1000))
    try:
        s_clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s_clean)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return str(int(time.time() * 1000))


class RedisPositionRepository:
    POSITION_KEY = "position:{market}:{symbol}"
    PNL_KEY = "pnl:{market}"
    TRADE_KEY = "trade:{market}:{trade_id}"
    POSITION_INDEX_KEY = "position_index:{market}"
    TRADE_DEDUPE_KEY = "trade_dedupe:{market}:{trade_id}"
    TRADE_INDEX_KEY = "trade_index:{market}:{symbol}"
    TRADE_SYMBOLS_KEY = "trade_symbols:{market}"
    MARK_KEY = "mark:{market}:{symbol}"
    FILL_QUEUE_KEY = "claw:fill:queue"
    FILL_DLQ_KEY = "claw:fill:dlq"

    POSITION_TTL = 7 * 24 * 3600
    TRADE_TTL = 30 * 24 * 3600

    def __init__(self, redis: Redis):
        self.r = redis

    def _position_key(self, market: str, symbol: str) -> str:
        return self.POSITION_KEY.format(market=market, symbol=symbol)

    def _pnl_key(self, market: str) -> str:
        return self.PNL_KEY.format(market=market)

    def _trade_key(self, market: str, trade_id: str) -> str:
        return self.TRADE_KEY.format(market=market, trade_id=trade_id)

    def _position_index_key(self, market: str) -> str:
        return self.POSITION_INDEX_KEY.format(market=market)

    def _trade_dedupe_key(self, market: str, trade_id: str) -> str:
        return self.TRADE_DEDUPE_KEY.format(market=market, trade_id=trade_id)

    def _trade_index_key(self, market: str, symbol: str) -> str:
        return self.TRADE_INDEX_KEY.format(market=market, symbol=symbol)

    def _trade_symbols_key(self, market: str) -> str:
        return self.TRADE_SYMBOLS_KEY.format(market=market)

    def _mark_key(self, market: str, symbol: str) -> str:
        return self.MARK_KEY.format(market=market, symbol=symbol)

    def trade_exists(self, market: str, trade_id: str) -> bool:
        """멱등키 존재 여부 확인."""
        return bool(self.r.exists(self._trade_dedupe_key(market, trade_id)))

    def get_position(self, market: str, symbol: str) -> Optional[PositionState]:
        key = self._position_key(market, symbol)
        raw = self.r.hgetall(key)
        if not raw:
            return None

        _is_bytes = isinstance(next(iter(raw)), bytes)

        def d(k: str) -> str:
            v = raw.get(k.encode() if _is_bytes else k, b"" if _is_bytes else "")
            return v.decode() if isinstance(v, bytes) else (v or "")

        return PositionState(
            symbol=symbol,
            qty=Decimal(d("qty") or "0"),
            avg_price=Decimal(d("avg_price") or "0"),
            realized_pnl=Decimal(d("realized_pnl") or "0"),  # legacy/optional
            updated_ts=d("updated_ts") or "0",
            currency=d("currency") or ("KRW" if market in ("KR", "COIN") else "USD"),
        )

    def get_position_context(self, market: str, symbol: str) -> dict[str, str]:
        """포지션 hash 원문(str:str) 조회. 연구/보조 메타 연결용."""
        return _hgetall_str(self.r, self._position_key(market, symbol))

    def save_position(
        self,
        market: str,
        symbol: str,
        qty: Decimal,
        avg_price: Decimal,
        realized_pnl: Decimal,
        currency: str,
        meta: Optional[dict[str, str]] = None,
    ) -> None:
        key = self._position_key(market, symbol)
        idx_key = self._position_index_key(market)

        if qty == 0:
            self.r.delete(key)
            self.r.srem(idx_key, symbol)
            return

        is_new_position = not self.r.exists(key)

        now_ms = str(int(time.time() * 1000))
        existing_opened = _hgetall_str(self.r, key).get("opened_ts", "")
        opened_ts = existing_opened or now_ms
        mapping = {
            "qty": str(qty),
            "avg_price": str(avg_price),
            "realized_pnl": str(realized_pnl),
            "updated_ts": now_ms,
            "opened_ts": opened_ts,
            "currency": currency,
            "side": "BUY",
        }
        if meta:
            for k, v in meta.items():
                if v is None:
                    continue
                mapping[k] = str(v)
        self.r.hset(key, mapping=mapping)
        self.r.expire(key, self.POSITION_TTL)
        self.r.sadd(idx_key, symbol)
        self.r.expire(idx_key, self.POSITION_TTL)

        # 신규 포지션 오픈 시 HWM을 avg_price로 초기화
        # 이전 포지션의 HWM 잔재가 trailing stop 계산에 영향 미치는 것 방지
        # (재진입 시 stop이 avg_price보다 높게 설정되어 즉시 stop_loss 발동하는 버그 차단)
        if is_new_position:
            hwm_key = f"claw:trail_hwm:{market}:{symbol}"
            self.r.set(hwm_key, str(avg_price), ex=self.POSITION_TTL)

    def get_all_positions(self, market: str) -> list[PositionState]:
        idx_key = self._position_index_key(market)
        symbols = self.r.smembers(idx_key)
        if not symbols:
            return []

        result = []
        for b in symbols:
            symbol = b.decode() if isinstance(b, bytes) else b
            pos = self.get_position(market, symbol)
            if pos:
                result.append(pos)
        return result

    def record_trade(self, trade_id: str, fill: FillEvent, realized_pnl: Decimal) -> bool:
        """
        멱등 트레이드 기록. trade_id는 외부에서 결정론적으로 전달.
        ts = 체결 시각(fill.ts), recorded_at_ms = 기록 시각.
        Returns: True=새로 기록됨(처리 계속), False=이미 존재(중복 스킵)
        """
        dedupe_key = self._trade_dedupe_key(fill.market, trade_id)
        if not self.r.set(dedupe_key, "1", nx=True, ex=self.TRADE_TTL):
            return False

        key = self._trade_key(fill.market, trade_id)
        fill_ts_ms = _to_ts_ms(fill.ts)
        recorded_at_ms = str(int(time.time() * 1000))
        payload = {
            "order_id": fill.order_id or "",
            "symbol": fill.symbol,
            "side": fill.side.value,
            "qty": str(fill.qty),
            "price": str(fill.price),
            "realized_pnl": str(realized_pnl),
            "ts": fill_ts_ms,
            "recorded_at_ms": recorded_at_ms,
            "exec_id": fill.exec_id or "",
            "fee": str(fill.fee),
            "signal_id": fill.signal_id or "",
            "source": getattr(fill, "source", None) or "",
        }
        self.r.hset(key, mapping=payload)
        self.r.expire(key, self.TRADE_TTL)

        idx_key = self._trade_index_key(fill.market, fill.symbol)
        symbols_key = self._trade_symbols_key(fill.market)
        try:
            score = int(fill_ts_ms)
            self.r.zadd(idx_key, {trade_id: score})
            self.r.expire(idx_key, self.TRADE_TTL)
            self.r.sadd(symbols_key, fill.symbol)
            self.r.expire(symbols_key, self.TRADE_TTL)
        except Exception:
            pass
        return True

    def get_pnl(self, market: str) -> tuple[Decimal, Decimal]:
        """(realized_pnl, unrealized_pnl)"""
        key = self._pnl_key(market)
        raw = self.r.hgetall(key)
        if not raw:
            return Decimal("0"), Decimal("0")

        _is_bytes = isinstance(next(iter(raw)), bytes)

        def d(k: str) -> str:
            v = raw.get(k.encode() if _is_bytes else k, b"" if _is_bytes else "")
            return v.decode() if isinstance(v, bytes) else (v or "")

        return (
            Decimal(d("realized_pnl") or "0"),
            Decimal(d("unrealized_pnl") or "0"),
        )

    def update_pnl(
        self,
        market: str,
        realized_delta: Decimal,
        unrealized: Optional[Decimal] = None,
    ) -> None:
        key = self._pnl_key(market)
        now_ms = str(int(time.time() * 1000))
        currency = "KRW" if market in ("KR", "COIN") else "USD"

        r, u = self.get_pnl(market)
        new_realized = r + realized_delta
        new_unrealized = unrealized if unrealized is not None else u

        self.r.hset(
            key,
            mapping={
                "realized_pnl": str(new_realized),
                "unrealized_pnl": str(new_unrealized),
                "currency": currency,
                "updated_ts": now_ms,
            },
        )

    def push_fill(self, fill: FillEvent) -> None:
        payload = self._fill_to_payload(fill)
        self.r.lpush(self.FILL_QUEUE_KEY, json.dumps(payload, ensure_ascii=False))

    def _fill_to_payload(self, fill: FillEvent) -> dict:
        payload = fill.model_dump(mode="json")
        payload["qty"] = str(payload["qty"])
        payload["price"] = str(payload["price"])
        payload["fee"] = str(payload.get("fee", "0"))
        payload["retry"] = getattr(fill, "retry", 0)
        if "source" in payload and payload["source"] is None:
            payload["source"] = ""
        return payload

    def push_fill_dlq(self, fill: FillEvent, reason: str) -> None:
        """DLQ에 LPUSH. payload에 reason, failed_at_ms 포함."""
        payload = self._fill_to_payload(fill)
        payload["reason"] = reason
        payload["failed_at_ms"] = str(int(time.time() * 1000))
        self.r.lpush(self.FILL_DLQ_KEY, json.dumps(payload, ensure_ascii=False))

    def requeue_fill(self, fill: FillEvent) -> None:
        """fill.retry += 1 후 LPUSH claw:fill:queue."""
        fill.retry += 1
        payload = self._fill_to_payload(fill)
        self.r.lpush(self.FILL_QUEUE_KEY, json.dumps(payload, ensure_ascii=False))

    def get_recent_trades(
        self, market: str, symbol: str, limit: int = 20
    ) -> list[dict]:
        """
        trade_index ZREVRANGE로 최근 거래 조회.
        Returns: [{"trade_id", "order_id", "side", "qty", "price", "realized_pnl", "ts", ...}, ...]
        """
        idx_key = self._trade_index_key(market, symbol)
        trade_ids = self.r.zrevrange(idx_key, 0, limit - 1, withscores=False)
        if not trade_ids:
            return []
        result = []
        for b in trade_ids:
            tid = b.decode() if isinstance(b, bytes) else b
            key = self._trade_key(market, tid)
            raw = self.r.hgetall(key)
            if not raw:
                continue
            out = {"trade_id": tid}
            for k, v in raw.items():
                dk = k.decode() if isinstance(k, bytes) else k
                dv = v.decode() if isinstance(v, bytes) else v
                out[dk] = dv
            result.append(out)
        return result

    def set_mark_price(self, market: str, symbol: str, price: Decimal) -> None:
        """임시 마크가 저장. unrealized 계산용."""
        key = self._mark_key(market, symbol)
        self.r.set(key, str(price))
        self.r.expire(key, self.POSITION_TTL)

    def recalc_unrealized(self, market: str) -> Decimal:
        """
        포지션별 mark_price 기반 unrealized 합산, pnl:{market}에 저장.
        Returns: unrealized 합계
        """
        positions = self.get_all_positions(market)
        unrealized = Decimal("0")
        for pos in positions:
            mk = self._mark_key(market, pos.symbol)
            raw = self.r.get(mk)
            if raw is None:
                continue
            try:
                mark_price = Decimal(raw.decode())
            except Exception:
                continue
            unrealized += (mark_price - pos.avg_price) * pos.qty

        key = self._pnl_key(market)
        r, _ = self.get_pnl(market)
        now_ms = str(int(time.time() * 1000))
        currency = "KRW" if market in ("KR", "COIN") else "USD"
        self.r.hset(
            key,
            mapping={
                "realized_pnl": str(r),
                "unrealized_pnl": str(unrealized),
                "currency": currency,
                "updated_ts": now_ms,
            },
        )
        return unrealized

    def pop_fill(self, timeout: float = 0) -> Optional[FillEvent]:
        raw = self.r.brpop(self.FILL_QUEUE_KEY, timeout=timeout)
        if not raw:
            return None
        _, data = raw
        try:
            d = json.loads(data)
            d["qty"] = Decimal(d["qty"])
            d["price"] = Decimal(d["price"])
            d["retry"] = d.get("retry", 0)
            d["fee"] = Decimal(d.get("fee", "0"))
            if "source" not in d:
                d["source"] = None
            return FillEvent.model_validate(d)
        except Exception as e:
            reason = f"pop_fill_parse_error:{e}"
            raw_text = (
                data.decode(errors="replace")
                if isinstance(data, (bytes, bytearray))
                else str(data)
            )
            self.r.lpush(
                self.FILL_DLQ_KEY,
                json.dumps(
                    {
                        "raw_payload": raw_text,
                        "reason": reason,
                        "failed_at_ms": str(int(time.time() * 1000)),
                    },
                    ensure_ascii=False,
                ),
            )
            return None
