"""upbit_ws_exit_monitor — WebSocket 기반 COIN 실시간 exit 감시

Upbit WebSocket API로 보유 COIN 종목의 ticker/orderbook을 실시간 수신하여:
  1. mark price 즉시 갱신
  2. HWM(High Water Mark) 갱신
  3. exit 조건 충족 시 즉시 시장가 매도
  4. orderbook 데이터 Redis 저장 (ob_ratio 등)

기존 position_exit_runner.py는 KR 포지션 + COIN fallback으로 유지.
이 프로세스는 COIN 전용 추가 감시 — exit_lock으로 중복 청산 방지.
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import os
import signal as _signal
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation

import redis
import websockets

from exchange.upbit.client import UpbitClient
from domain.models import PlaceOrderRequest, OrderSide, OrderType, OrderStatus
from utils.redis_helpers import is_market_hours, today_kst
from guards.notifier import send_telegram

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

_WS_URL = "wss://api.upbit.com/websocket/v1"

_POSITION_SCAN_SEC = 30       # 감시 종목 갱신 주기
_RECONNECT_BASE_SEC = 1.0     # 재연결 base (exponential backoff)
_RECONNECT_MAX_SEC = 60.0     # 재연결 최대 대기
_PING_INTERVAL_SEC = 30       # WebSocket ping 주기
_OB_TOP_N = 5                 # orderbook 상위 N호가

_POSITION_TTL = 7 * 86400
_ORDER_META_TTL = 86400
_EXIT_LOCK_TTL = 60           # 중복 매도 방지 lock TTL
_OB_TTL = 10                  # orderbook Redis TTL

# Exit 파라미터 (position_exit_runner와 동일)
_STOP_LOSS_PCT = Decimal(os.getenv("EXIT_STOP_LOSS_PCT", "0.02"))
_TAKE_PROFIT_PCT = Decimal(os.getenv("EXIT_TAKE_PROFIT_PCT", "0.02"))
_TRAIL_STOP_PCT = Decimal(os.getenv("EXIT_TRAIL_STOP_PCT", "0.015"))
_TIME_LIMIT_SEC = int(os.getenv("EXIT_TIME_LIMIT_SEC", "1800"))
_TIME_LIMIT_MAX_SEC = int(os.getenv("EXIT_TIME_LIMIT_MAX_SEC", str(_TIME_LIMIT_SEC * 2)))

_COIN_STOP_LOSS_PCT = Decimal(os.getenv("COIN_EXIT_STOP_LOSS_PCT", str(_STOP_LOSS_PCT)))
_COIN_TAKE_PROFIT_PCT = Decimal(os.getenv("COIN_EXIT_TAKE_PROFIT_PCT", str(_TAKE_PROFIT_PCT)))
_COIN_TRAIL_STOP_PCT = Decimal(os.getenv("COIN_EXIT_TRAIL_STOP_PCT", str(_TRAIL_STOP_PCT)))
_COIN_TIME_LIMIT_SEC = int(os.getenv("COIN_EXIT_TIME_LIMIT_SEC", str(_TIME_LIMIT_SEC)))
_COIN_TIME_LIMIT_MAX_SEC = int(os.getenv("COIN_EXIT_TIME_LIMIT_MAX_SEC", str(_TIME_LIMIT_MAX_SEC)))

_FILL_QUEUE_KEY = "claw:fill:queue"
_FILL_DEDUPE_TTL = 86400

_LOCK_KEY = "ws_exit_monitor:lock"
_LOCK_TTL = 120


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------

def _log(event: str, **kwargs) -> None:
    parts = [event] + [f"{k}={v}" for k, v in kwargs.items()]
    print(f"ws_exit: {' '.join(parts)}", flush=True)


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _get_coin_positions(r) -> dict:
    """Redis에서 COIN 포지션 목록 조회. {symbol: {qty, avg_price, opened_ts, ...}}"""
    result = {}
    idx_key = "position_index:COIN"
    members = r.smembers(idx_key)
    if not members:
        return result

    for b in members:
        symbol = b.decode() if isinstance(b, bytes) else b
        raw = r.hgetall(f"position:COIN:{symbol}")
        if not raw:
            continue
        pos = {}
        for k, v in raw.items():
            dk = k.decode() if isinstance(k, bytes) else k
            dv = v.decode() if isinstance(v, bytes) else v
            pos[dk] = dv
        try:
            qty = Decimal(pos.get("qty", "0"))
            avg_price = Decimal(pos.get("avg_price", "0"))
            opened_ts = int(pos.get("opened_ts", str(int(time.time()))))
            if qty > 0 and avg_price > 0:
                result[symbol] = {
                    "qty": qty,
                    "avg_price": avg_price,
                    "opened_ts": opened_ts,
                    "hash": pos,
                }
        except (InvalidOperation, ValueError):
            continue
    return result


def _cfg_or_none(r, field: str) -> "Decimal | None":
    """claw:config:COIN에서 Redis override 읽기."""
    raw = r.hget("claw:config:COIN", field)
    if raw is None:
        return None
    try:
        return Decimal(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


def _symbol_to_upbit_code(symbol: str) -> str:
    """BTC -> KRW-BTC 변환."""
    if symbol.startswith("KRW-"):
        return symbol
    return f"KRW-{symbol}"


def _upbit_code_to_symbol(code: str) -> str:
    """Upbit code -> 내부 symbol. 시스템 표준이 KRW-xxx 이므로 그대로 반환."""
    return code


# ---------------------------------------------------------------------------
# exit 조건 판단 (position_exit_runner._check_exit와 동일 로직)
# ---------------------------------------------------------------------------

def _check_exit(avg_price: Decimal, mark_price: Decimal, opened_ts: int,
                pos: dict = None, hwm_price: Decimal = None,
                stop_pct: Decimal = None, take_pct: Decimal = None,
                trail_pct: Decimal = None, time_limit_sec: int = None,
                time_limit_max_sec: int = None):
    """Exit 조건 확인. 조건 충족 시 reason 문자열, 없으면 None."""
    if avg_price <= 0 or mark_price <= 0:
        return None

    # 우선순위: cfg > pos hash > 모듈 상수
    if stop_pct is not None:
        _eff_stop = stop_pct
    elif pos:
        try:
            _eff_stop = Decimal(pos.get("stop_pct") or str(_COIN_STOP_LOSS_PCT))
        except Exception:
            _eff_stop = _COIN_STOP_LOSS_PCT
    else:
        _eff_stop = _COIN_STOP_LOSS_PCT

    if take_pct is not None:
        _eff_take = take_pct
    elif pos:
        try:
            _eff_take = Decimal(pos.get("take_pct") or str(_COIN_TAKE_PROFIT_PCT))
        except Exception:
            _eff_take = _COIN_TAKE_PROFIT_PCT
    else:
        _eff_take = _COIN_TAKE_PROFIT_PCT

    _eff_trail = trail_pct if trail_pct is not None else _COIN_TRAIL_STOP_PCT

    stop_price = avg_price * (1 - _eff_stop)

    # Trailing stop: HWM에서 trail_pct 이상 하락하면 청산
    if hwm_price is not None and hwm_price > avg_price:
        trail_stop = hwm_price * (1 - _eff_trail)
        stop_price = max(stop_price, trail_stop)

    take_price = avg_price * (1 + _eff_take)

    now_ms = int(time.time() * 1000)
    if opened_ts > 1_000_000_000_000:
        held_sec = (now_ms - opened_ts) // 1000
    else:
        held_sec = int(time.time()) - opened_ts

    if mark_price <= stop_price:
        return f"stop_loss(mark={mark_price:.4f}<=stop={stop_price:.4f})"
    if mark_price >= take_price:
        return f"take_profit(mark={mark_price:.4f}>=take={take_price:.4f})"

    _eff_time_limit = time_limit_sec if time_limit_sec is not None else _COIN_TIME_LIMIT_SEC
    _eff_time_limit_max = time_limit_max_sec if time_limit_max_sec is not None else _COIN_TIME_LIMIT_MAX_SEC
    if held_sec >= _eff_time_limit:
        _extend_floor = avg_price * (1 - _eff_stop / 2)
        if mark_price >= _extend_floor and held_sec < _eff_time_limit_max:
            pass
        else:
            return f"time_limit(held={held_sec}s>={_eff_time_limit}s)"
    return None


# ---------------------------------------------------------------------------
# 매도 주문 / fill push
# ---------------------------------------------------------------------------

def _push_fill_event(r, symbol: str, side: str, qty: Decimal,
                     price: Decimal, order_id: str) -> bool:
    """FillEvent를 claw:fill:queue에 lpush."""
    exec_id = f"coin_fill_{order_id}"
    dedupe_key = f"claw:fill_dedupe:{exec_id}"
    if not r.set(dedupe_key, "1", nx=True, ex=_FILL_DEDUPE_TTL):
        return False

    ts_ms = str(int(time.time() * 1000))
    fill = {
        "exec_id": exec_id,
        "order_id": order_id,
        "symbol": symbol,
        "market": "COIN",
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "ts": ts_ms,
        "source": "ws_exit_monitor",
        "fee": "0",
        "retry": 0,
    }
    r.lpush(_FILL_QUEUE_KEY, json.dumps(fill))
    _log("fill_pushed", symbol=symbol, side=side, qty=str(qty),
         price=str(price), exec_id=exec_id)
    return True


def _place_sell(r, upbit: UpbitClient, symbol: str, qty: Decimal,
                mark_price: Decimal, reason: str) -> bool:
    """COIN 시장가 매도 주문."""
    client_order_id = str(uuid.uuid4())
    req = PlaceOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
    )
    try:
        result = upbit.place_order(req)
    except Exception as e:
        _log("sell_error", symbol=symbol, error=str(e))
        return False

    if result.status == OrderStatus.REJECTED:
        _log("sell_rejected", symbol=symbol, order_id=result.order_id)
        return False

    order_id = result.order_id
    limit_price = mark_price.quantize(Decimal("0.00000001"))

    r.set(f"order:COIN:{order_id}", "SUBMITTED")
    r.expire(f"order:COIN:{order_id}", _ORDER_META_TTL)
    r.hset(f"claw:order_meta:COIN:{order_id}", mapping={
        "symbol": symbol,
        "side": "SELL",
        "qty": str(qty),
        "limit_price": str(limit_price),
        "exit_reason": reason,
        "first_seen_ts": str(int(time.time())),
        "source": "ws_exit_monitor",
    })
    r.expire(f"claw:order_meta:COIN:{order_id}", _ORDER_META_TTL)
    r.set(f"claw:exit_order:COIN:{symbol}", order_id, ex=_ORDER_META_TTL)

    _log("sell_submitted", symbol=symbol, order_id=order_id,
         qty=str(qty), price=str(limit_price), reason=reason)

    try:
        send_telegram(
            f"[CLAW] SELL 주문접수 (WS)\n"
            f"market=COIN symbol={symbol}\n"
            f"qty={qty} price={limit_price} KRW\n"
            f"reason={reason} order_id={order_id}"
        )
    except Exception:
        pass

    return True


# ---------------------------------------------------------------------------
# WebSocket 메시지 처리
# ---------------------------------------------------------------------------

def _handle_ticker(r, upbit: UpbitClient, data: dict, positions: dict,
                   cfg_stop, cfg_take, cfg_trail) -> None:
    """ticker 메시지 처리: mark/HWM 갱신 + exit 조건 체크."""
    code = data.get("code", "")
    symbol = _upbit_code_to_symbol(code)
    trade_price = data.get("trade_price")
    if trade_price is None or symbol not in positions:
        return

    mark_price = Decimal(str(trade_price))

    # mark price 갱신
    r.set(f"mark:COIN:{symbol}", str(mark_price))

    pos_info = positions[symbol]
    avg_price = pos_info["avg_price"]
    qty = pos_info["qty"]
    opened_ts = pos_info["opened_ts"]
    pos_hash = pos_info["hash"]

    # HWM 갱신
    hwm_key = f"claw:trail_hwm:COIN:{symbol}"
    hwm_raw = r.get(hwm_key)
    try:
        prev_hwm = Decimal(hwm_raw.decode()) if hwm_raw else mark_price
    except Exception:
        prev_hwm = mark_price
    hwm_price = max(prev_hwm, mark_price)
    r.set(hwm_key, str(hwm_price), ex=_POSITION_TTL)

    # exit_lock 확인 — 이미 매도 중이면 skip
    lock_key = f"claw:exit_lock:COIN:{symbol}"
    if r.exists(lock_key):
        # CANCELED 주문이면 lock 해제
        exit_order_raw = r.get(f"claw:exit_order:COIN:{symbol}")
        if exit_order_raw:
            oid = exit_order_raw.decode() if isinstance(exit_order_raw, bytes) else exit_order_raw
            order_status = r.get(f"order:COIN:{oid}")
            if order_status and order_status.decode() == "CANCELED":
                r.delete(lock_key)
                _log("sell_retry_after_cancel", symbol=symbol, order_id=oid)
            else:
                return
        else:
            return

    # exit 조건 체크
    reason = _check_exit(
        avg_price, mark_price, opened_ts, pos=pos_hash, hwm_price=hwm_price,
        stop_pct=cfg_stop, take_pct=cfg_take, trail_pct=cfg_trail,
        time_limit_sec=_COIN_TIME_LIMIT_SEC, time_limit_max_sec=_COIN_TIME_LIMIT_MAX_SEC,
    )
    if reason is None:
        return

    # exit 조건 충족 → lock 획득 후 매도
    if not r.set(lock_key, "1", nx=True, ex=_EXIT_LOCK_TTL):
        _log("exit_lock_held_skip", symbol=symbol)
        return

    _log("exit_triggered", symbol=symbol, reason=reason,
         avg=str(avg_price), mark=str(mark_price), qty=str(qty))

    ok = _place_sell(r, upbit, symbol, qty, mark_price, reason)
    if ok and "stop_loss" in reason:
        today = today_kst()
        _ds_key = f"claw:daily_stop:COIN:{symbol}:{today}"
        r.hset(_ds_key, mapping={"stop_price": str(mark_price), "stop_ts": str(int(time.time()))})
        r.expire(_ds_key, 86400)
        _log("daily_stop_marked", symbol=symbol, today=today)
    if ok and "time_limit" in reason:
        r.set(f"consensus:symbol_cooldown:COIN:{symbol}", "1", ex=7200)
        _log("time_limit_cooldown_marked", symbol=symbol, cooldown_sec=7200)
    if ok and "take_profit" in reason:
        r.set(f"consensus:symbol_cooldown:COIN:{symbol}", "1", ex=1800)
        _log("take_profit_cooldown_marked", symbol=symbol, cooldown_sec=1800)
    if not ok:
        r.delete(lock_key)


def _handle_orderbook(r, data: dict) -> None:
    """orderbook 메시지 처리: Redis에 bid/ask 집계 저장."""
    code = data.get("code", "")
    symbol = _upbit_code_to_symbol(code)
    units = data.get("orderbook_units", [])
    if not units:
        return

    top_n = units[:_OB_TOP_N]
    bid_total = sum(float(u.get("bid_size", 0)) * float(u.get("bid_price", 0)) for u in top_n)
    ask_total = sum(float(u.get("ask_size", 0)) * float(u.get("ask_price", 0)) for u in top_n)
    ob_ratio = bid_total / ask_total if ask_total > 0 else 0.0

    ob_key = f"orderbook:COIN:{symbol}"
    r.hset(ob_key, mapping={
        "ob_bid_total": f"{bid_total:.2f}",
        "ob_ask_total": f"{ask_total:.2f}",
        "ob_ratio": f"{ob_ratio:.4f}",
        "ts": str(int(time.time())),
    })
    r.expire(ob_key, _OB_TTL)


# ---------------------------------------------------------------------------
# WebSocket 메인 루프
# ---------------------------------------------------------------------------

async def _ws_loop(r, upbit: UpbitClient) -> None:
    """WebSocket 연결 + 메시지 수신 루프."""
    reconnect_delay = _RECONNECT_BASE_SEC
    positions = {}
    watched_symbols = set()

    while True:
        # 포지션 목록 갱신
        if not is_market_hours("COIN"):
            _log("market_closed", sleep_sec=_POSITION_SCAN_SEC)
            await asyncio.sleep(_POSITION_SCAN_SEC)
            continue

        positions = _get_coin_positions(r)
        new_symbols = set(positions.keys())

        if not new_symbols:
            _log("no_positions", sleep_sec=_POSITION_SCAN_SEC)
            await asyncio.sleep(_POSITION_SCAN_SEC)
            continue

        # Redis config override 읽기
        cfg_stop = _cfg_or_none(r, "stop_pct")
        cfg_take = _cfg_or_none(r, "take_pct")
        cfg_trail = _cfg_or_none(r, "trail_pct")
        if cfg_stop is None:
            cfg_stop = _COIN_STOP_LOSS_PCT
        if cfg_take is None:
            cfg_take = _COIN_TAKE_PROFIT_PCT
        if cfg_trail is None:
            cfg_trail = _COIN_TRAIL_STOP_PCT

        codes = [_symbol_to_upbit_code(s) for s in new_symbols]
        watched_symbols = new_symbols

        _log("ws_connect", symbols=",".join(sorted(new_symbols)), count=len(new_symbols))

        try:
            async with websockets.connect(
                _WS_URL,
                ping_interval=_PING_INTERVAL_SEC,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                # 구독 요청: ticker + orderbook
                subscribe_msg = json.dumps([
                    {"ticket": str(uuid.uuid4())},
                    {"type": "ticker", "codes": codes},
                    {"type": "orderbook", "codes": codes},
                ])
                await ws.send(subscribe_msg)
                _log("ws_subscribed", codes=len(codes))

                reconnect_delay = _RECONNECT_BASE_SEC  # 연결 성공 → reset

                # 포지션 스캔 타이머
                last_scan = time.time()

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_POSITION_SCAN_SEC)
                    except asyncio.TimeoutError:
                        # timeout = 포지션 스캔 시간
                        pass
                    else:
                        # 바이너리 메시지 (Upbit은 바이너리 전송)
                        if isinstance(raw, bytes):
                            try:
                                data = json.loads(raw.decode("utf-8"))
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                        elif isinstance(raw, str):
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                        else:
                            continue

                        msg_type = data.get("type", "")
                        if msg_type == "ticker":
                            _handle_ticker(r, upbit, data, positions,
                                           cfg_stop, cfg_take, cfg_trail)
                        elif msg_type == "orderbook":
                            _handle_orderbook(r, data)

                    # 주기적 포지션/config 갱신
                    now = time.time()
                    if now - last_scan >= _POSITION_SCAN_SEC:
                        last_scan = now

                        if not is_market_hours("COIN"):
                            _log("market_closed_disconnect")
                            break

                        positions = _get_coin_positions(r)
                        new_symbols = set(positions.keys())

                        # config 갱신
                        cfg_stop = _cfg_or_none(r, "stop_pct")
                        cfg_take = _cfg_or_none(r, "take_pct")
                        cfg_trail = _cfg_or_none(r, "trail_pct")
                        if cfg_stop is None:
                            cfg_stop = _COIN_STOP_LOSS_PCT
                        if cfg_take is None:
                            cfg_take = _COIN_TAKE_PROFIT_PCT
                        if cfg_trail is None:
                            cfg_trail = _COIN_TRAIL_STOP_PCT

                        # 감시 종목 변경 시 reconnect
                        if new_symbols != watched_symbols:
                            _log("symbols_changed",
                                 old=",".join(sorted(watched_symbols)),
                                 new=",".join(sorted(new_symbols)))
                            break  # outer loop에서 reconnect

                        # lock 갱신
                        r.expire(_LOCK_KEY, _LOCK_TTL)

        except websockets.exceptions.ConnectionClosed as e:
            _log("ws_closed", code=e.code, reason=str(e.reason)[:100])
        except websockets.exceptions.WebSocketException as e:
            _log("ws_error", error=str(e)[:200])
        except Exception as e:
            _log("ws_unexpected_error", error=str(e)[:200])

        # Exponential backoff 재연결
        _log("ws_reconnect", delay_sec=f"{reconnect_delay:.1f}")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, _RECONNECT_MAX_SEC)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("ws_exit: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    if not os.getenv("UPBIT_ACCESS_KEY"):
        print("ws_exit: UPBIT_ACCESS_KEY not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("ws_exit: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("ws_exit: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    upbit = UpbitClient()

    print(
        f"ws_exit: started "
        f"COIN: stop={float(_COIN_STOP_LOSS_PCT)*100:.1f}% "
        f"take={float(_COIN_TAKE_PROFIT_PCT)*100:.1f}% "
        f"trail={float(_COIN_TRAIL_STOP_PCT)*100:.1f}% "
        f"time_limit={_COIN_TIME_LIMIT_SEC}s "
        f"time_limit_max={_COIN_TIME_LIMIT_MAX_SEC}s "
        f"scan_sec={_POSITION_SCAN_SEC}",
        flush=True,
    )

    try:
        asyncio.run(_ws_loop(r, upbit))
    except KeyboardInterrupt:
        pass
    finally:
        r.delete(_LOCK_KEY)
        print("ws_exit: lock released", flush=True)


if __name__ == "__main__":
    main()
