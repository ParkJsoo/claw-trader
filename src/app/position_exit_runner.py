"""position_exit_runner — Phase 12

KIS 보유종목 조회 → Redis 포지션 동기화 → Exit 조건 감시 → 자동 매도

Exit 조건 (하나라도 충족 시):
  1. Stop-loss:   mark_price <= avg_price * (1 - EXIT_STOP_LOSS_PCT)   기본 2%
  2. Take-profit: mark_price >= avg_price * (1 + EXIT_TAKE_PROFIT_PCT) 기본 2%
  3. Time-based:  보유 시간 >= EXIT_TIME_LIMIT_SEC                     기본 1800s (30분)

책임:
  - KIS 잔고조회(TTTC8434R output1) → position:KR:{symbol} 동기화
  - Exit 조건 감시 (mark:KR:{symbol} 활용 — MarketDataRunner가 갱신)
  - SELL 주문 (limit at mark_price, global pause 무시)
  - 중복 방지: claw:exit_lock:KR:{symbol} SET NX TTL

책임 외:
  - 매수 신호 생성 (consensus_signal_runner)
  - Risk/Strategy gate (runner.py)
  - 주문 TTL 취소 (order_watcher)

알려진 한계:
  - SELL 체결 후 FillEvent가 portfolio engine에 push되지 않음 → PnL 수동 확인 필요
    (order_watcher KR fill detection 구현 전까지)
  - 재기동 시 신규 발견 포지션의 opened_ts가 현재 시각으로 초기화됨
"""
from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import redis

from exchange.kis.client import KisClient
from domain.models import PlaceOrderRequest, OrderSide, OrderType, OrderStatus
from utils.redis_helpers import is_market_hours

_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "exit_runner:lock"

_POLL_SEC = float(os.getenv("EXIT_POLL_SEC", "30"))
_LOCK_TTL = max(120, int(_POLL_SEC * 3) + 30)  # poll 주기의 3배 + 여유

_STOP_LOSS_PCT = Decimal(os.getenv("EXIT_STOP_LOSS_PCT", "0.02"))
_TAKE_PROFIT_PCT = Decimal(os.getenv("EXIT_TAKE_PROFIT_PCT", "0.02"))
_TIME_LIMIT_SEC = int(os.getenv("EXIT_TIME_LIMIT_SEC", "1800"))

# 설정값 유효성 검증
if not (Decimal("0") < _STOP_LOSS_PCT < Decimal("1")):
    raise ValueError(f"EXIT_STOP_LOSS_PCT must be 0 < x < 1, got {_STOP_LOSS_PCT}")
if not (Decimal("0") < _TAKE_PROFIT_PCT < Decimal("1")):
    raise ValueError(f"EXIT_TAKE_PROFIT_PCT must be 0 < x < 1, got {_TAKE_PROFIT_PCT}")

_EXIT_LOCK_TTL = 300   # 5분: 중복 매도 방지
_POSITION_TTL = 7 * 86400
_ORDER_META_TTL = 86400


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------

def _log(event: str, **kwargs) -> None:
    parts = [event] + [f"{k}={v}" for k, v in kwargs.items()]
    print(f"exit_runner: {' '.join(parts)}", flush=True)


# ---------------------------------------------------------------------------
# 포지션 동기화 (KIS → Redis)
# ---------------------------------------------------------------------------

def _load_cached_positions(r) -> dict:
    """Redis에 캐시된 포지션 읽기 (KIS API 실패 시 fallback)."""
    result = {}
    for b in (r.smembers("position_index:KR") or []):
        symbol = b.decode() if isinstance(b, bytes) else b
        raw = r.hgetall(f"position:KR:{symbol}")
        if not raw:
            continue
        def d(k):
            v = raw.get(k.encode() if isinstance(list(raw.keys())[0], bytes) else k, b"")
            return v.decode() if isinstance(v, bytes) else v
        try:
            qty = Decimal(d("qty") or "0")
            avg_price = Decimal(d("avg_price") or "0")
            opened_ts = int(d("opened_ts") or int(time.time()))
            if qty > 0:
                result[symbol] = {"qty": qty, "avg_price": avg_price, "opened_ts": opened_ts}
        except Exception:
            continue
    return result


def _sync_positions(r, kis: KisClient) -> dict:
    """KIS 잔고조회 output1 → Redis position:KR:{symbol} 동기화.

    KIS API 실패 시 Redis 캐시 포지션으로 fallback (exit 조건 평가는 계속).

    Returns:
        {symbol: {"qty": Decimal, "avg_price": Decimal, "opened_ts": int}}
    """
    try:
        holdings = kis.get_kr_holdings()
    except Exception as e:
        _log("sync_error_fallback_cache", reason=str(e))
        return _load_cached_positions(r)

    now_ms = int(time.time() * 1000)

    # 현재 Redis에 있는 KR 포지션 목록
    existing: set[str] = set()
    for b in (r.smembers("position_index:KR") or []):
        existing.add(b.decode() if isinstance(b, bytes) else b)

    synced: dict = {}
    held_symbols: set[str] = set()

    for h in holdings:
        symbol = h["symbol"]
        qty = h["qty"]
        avg_price = h["avg_price"]
        held_symbols.add(symbol)

        pos_key = f"position:KR:{symbol}"

        # opened_ts: 기존에 있으면 유지, 처음 보이면 현재 시각
        raw_opened = r.hget(pos_key, "opened_ts")
        if raw_opened:
            try:
                opened_ts = int(raw_opened.decode() if isinstance(raw_opened, bytes) else raw_opened)
            except Exception:
                opened_ts = int(time.time())
        else:
            opened_ts = int(time.time())

        r.hset(pos_key, mapping={
            "qty": str(qty),
            "avg_price": str(avg_price),
            "opened_ts": str(opened_ts),
            "updated_ts": str(now_ms),
            "currency": "KRW",
        })
        r.expire(pos_key, _POSITION_TTL)
        r.sadd("position_index:KR", symbol)
        r.expire("position_index:KR", _POSITION_TTL)

        synced[symbol] = {"qty": qty, "avg_price": avg_price, "opened_ts": opened_ts}

    # Redis에는 있지만 KIS 잔고에 없는 종목 → 정리
    for sym in existing:
        if sym not in held_symbols:
            r.delete(f"position:KR:{sym}")
            r.srem("position_index:KR", sym)
            _log("position_removed", symbol=sym, reason="not_in_kis_holdings")

    return synced


# ---------------------------------------------------------------------------
# 현재가 조회
# ---------------------------------------------------------------------------

def _get_mark_price(r, symbol: str):
    """mark:KR:{symbol} 에서 현재가 조회 (MarketDataRunner가 갱신)."""
    raw = r.get(f"mark:KR:{symbol}")
    if not raw:
        return None
    try:
        return Decimal(raw.decode() if isinstance(raw, bytes) else raw)
    except (InvalidOperation, Exception):
        return None


# ---------------------------------------------------------------------------
# Exit 조건 판단
# ---------------------------------------------------------------------------

def _check_exit(avg_price: Decimal, mark_price: Decimal, opened_ts: int):
    """Exit 조건 확인. 조건 충족 시 reason 문자열 반환, 없으면 None."""
    stop_price = avg_price * (1 - _STOP_LOSS_PCT)
    take_price = avg_price * (1 + _TAKE_PROFIT_PCT)
    held_sec = int(time.time()) - opened_ts

    if mark_price <= stop_price:
        return f"stop_loss(mark={mark_price:.0f}<=stop={stop_price:.0f})"
    if mark_price >= take_price:
        return f"take_profit(mark={mark_price:.0f}>=take={take_price:.0f})"
    if held_sec >= _TIME_LIMIT_SEC:
        return f"time_limit(held={held_sec}s>={_TIME_LIMIT_SEC}s)"
    return None


# ---------------------------------------------------------------------------
# 매도 주문
# ---------------------------------------------------------------------------

def _place_sell(r, kis: KisClient, symbol: str, qty: Decimal,
                limit_price: Decimal, reason: str) -> bool:
    """SELL 주문 제출 + Redis order/meta 기록."""
    client_order_id = str(uuid.uuid4())
    req = PlaceOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        qty=qty,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )
    try:
        result = kis.place_order(req)
    except Exception as e:
        _log("sell_error", symbol=symbol, error=str(e))
        return False

    if result.status == OrderStatus.REJECTED:
        _log("sell_rejected", symbol=symbol, order_id=result.order_id)
        return False

    order_id = result.order_id

    # order_watcher가 TTL 취소 추적할 수 있도록 기록
    r.set(f"order:KR:{order_id}", "SUBMITTED")
    r.expire(f"order:KR:{order_id}", _ORDER_META_TTL)
    r.hset(f"claw:order_meta:KR:{order_id}", mapping={
        "symbol": symbol,
        "side": "SELL",
        "qty": str(qty),
        "limit_price": str(limit_price),
        "exit_reason": reason,
        "first_seen_ts": str(int(time.time())),
        "source": "exit_runner",
    })
    r.expire(f"claw:order_meta:KR:{order_id}", _ORDER_META_TTL)

    _log("sell_submitted",
         symbol=symbol, order_id=order_id,
         qty=str(qty), price=str(limit_price), reason=reason)
    return True


# ---------------------------------------------------------------------------
# 핵심: 1회 실행
# ---------------------------------------------------------------------------

def run_once(r, kis: KisClient) -> None:
    """포지션 동기화 → exit 조건 체크 → 필요 시 매도."""
    # 장중에만 exit 평가 (time_limit 스팸 방지 + 장외 주문 방지)
    if not is_market_hours("KR"):
        return

    positions = _sync_positions(r, kis)
    if not positions:
        return

    for symbol, pos in positions.items():
        qty = pos["qty"]
        avg_price = pos["avg_price"]
        opened_ts = pos["opened_ts"]

        if qty <= 0:
            continue

        # 이미 매도 주문 진행 중이면 skip
        if r.exists(f"claw:exit_lock:KR:{symbol}"):
            continue

        mark_price = _get_mark_price(r, symbol)
        if mark_price is None:
            _log("no_mark_price", symbol=symbol)
            continue

        reason = _check_exit(avg_price, mark_price, opened_ts)
        if reason is None:
            pnl_pct = float((mark_price - avg_price) / avg_price * 100)
            held_sec = int(time.time()) - opened_ts
            _log("hold", symbol=symbol,
                 avg=str(avg_price), mark=str(mark_price),
                 pnl_pct=f"{pnl_pct:+.2f}%",
                 held_sec=held_sec,
                 stop=str((avg_price * (1 - _STOP_LOSS_PCT)).quantize(Decimal("1"))),
                 take=str((avg_price * (1 + _TAKE_PROFIT_PCT)).quantize(Decimal("1"))))
            continue

        # Exit 조건 충족 → 매도 lock 획득 후 주문
        lock_key = f"claw:exit_lock:KR:{symbol}"
        if not r.set(lock_key, "1", nx=True, ex=_EXIT_LOCK_TTL):
            # 다른 프로세스/사이클이 이미 lock 획득 → 중복 방지
            _log("exit_lock_held_skip", symbol=symbol)
            continue

        _log("exit_triggered", symbol=symbol, reason=reason,
             avg=str(avg_price), mark=str(mark_price), qty=str(qty))

        ok = _place_sell(r, kis, symbol, qty, mark_price, reason)
        if not ok:
            # 주문 실패 시 lock 해제 → 다음 폴링에서 재시도
            r.delete(lock_key)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("exit_runner: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("exit_runner: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("exit_runner: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    kis = KisClient()

    print(
        f"exit_runner: started "
        f"poll_sec={_POLL_SEC} "
        f"stop_loss={float(_STOP_LOSS_PCT)*100:.1f}% "
        f"take_profit={float(_TAKE_PROFIT_PCT)*100:.1f}% "
        f"time_limit={_TIME_LIMIT_SEC}s "
        f"lock_ttl={_LOCK_TTL}s",
        flush=True,
    )

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            try:
                run_once(r, kis)
            except Exception as e:
                _log("unexpected_error", error=str(e))
            time.sleep(_POLL_SEC)
    finally:
        r.delete(_LOCK_KEY)
        print("exit_runner: lock released", flush=True)


if __name__ == "__main__":
    main()
