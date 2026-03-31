"""position_exit_runner — Phase 12 / US 확장

KIS/IBKR 보유종목 조회 → Redis 포지션 동기화 → Exit 조건 감시 → 자동 매도

Exit 조건 (하나라도 충족 시):
  1. Stop-loss:   mark_price <= avg_price * (1 - EXIT_STOP_LOSS_PCT)   기본 2%
  2. Take-profit: mark_price >= avg_price * (1 + EXIT_TAKE_PROFIT_PCT) 기본 2%
  3. Time-based:  보유 시간 >= EXIT_TIME_LIMIT_SEC                     기본 1800s (30분)

책임:
  - KR: KIS 잔고조회(TTTC8434R output1) → position:KR:{symbol} 동기화
  - US: IBKR portfolio() → position:US:{symbol} 동기화
  - Exit 조건 감시 (mark:{market}:{symbol} 활용 — MarketDataRunner가 갱신)
  - SELL 주문 (limit at mark_price, global pause 무시)
  - 중복 방지: claw:exit_lock:{market}:{symbol} SET NX TTL
  - Fill 감지: 보유종목 diff → FillEvent push → claw:fill:queue

책임 외:
  - 매수 신호 생성 (consensus_signal_runner)
  - Risk/Strategy gate (runner.py)
  - 주문 TTL 취소 (order_watcher)

알려진 한계:
  - 재기동 시 신규 발견 포지션의 opened_ts가 현재 시각으로 초기화됨
"""
from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import redis

from exchange.kis.client import KisClient
from exchange.ibkr.client import IbkrClient
from exchange.upbit.client import UpbitClient
from domain.models import PlaceOrderRequest, OrderSide, OrderType, OrderStatus
from utils.redis_helpers import is_market_hours, today_kst, get_config
from guards.notifier import send_telegram

_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "exit_runner:lock"

_POLL_SEC = float(os.getenv("EXIT_POLL_SEC", "30"))
_LOCK_TTL = max(120, int(_POLL_SEC * 3) + 30)  # poll 주기의 3배 + 여유

_STOP_LOSS_PCT = Decimal(os.getenv("EXIT_STOP_LOSS_PCT", "0.02"))
_TAKE_PROFIT_PCT = Decimal(os.getenv("EXIT_TAKE_PROFIT_PCT", "0.02"))
_TIME_LIMIT_SEC = int(os.getenv("EXIT_TIME_LIMIT_SEC", "1800"))
_TRAIL_STOP_PCT = Decimal(os.getenv("EXIT_TRAIL_STOP_PCT", "0.015"))
_TIME_LIMIT_MAX_SEC = int(os.getenv("EXIT_TIME_LIMIT_MAX_SEC", str(_TIME_LIMIT_SEC * 2)))

# 설정값 유효성 검증
if not (Decimal("0") < _STOP_LOSS_PCT < Decimal("1")):
    raise ValueError(f"EXIT_STOP_LOSS_PCT must be 0 < x < 1, got {_STOP_LOSS_PCT}")
if not (Decimal("0") < _TAKE_PROFIT_PCT < Decimal("1")):
    raise ValueError(f"EXIT_TAKE_PROFIT_PCT must be 0 < x < 1, got {_TAKE_PROFIT_PCT}")

_EXIT_LOCK_TTL = 60    # 1분: 중복 매도 방지 (SIGKILL 시 공백 최소화)
_POSITION_TTL = 7 * 86400
_ORDER_META_TTL = 86400

# H5: IBKR 연속 fallback 카운터 (캐시 포지션 매도 루프 방지)
_ibkr_fallback_count = 0
_IBKR_FALLBACK_MAX = 3


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------

def _log(event: str, **kwargs) -> None:
    parts = [event] + [f"{k}={v}" for k, v in kwargs.items()]
    print(f"exit_runner: {' '.join(parts)}", flush=True)


# ---------------------------------------------------------------------------
# FillEvent push helper
# ---------------------------------------------------------------------------

_FILL_QUEUE_KEY = "claw:fill:queue"
_FILL_DEDUPE_TTL = 86400  # 24h — exec_id 중복 방지 키 TTL


def _push_fill_event(r, symbol: str, side: str, qty: Decimal,
                     price: Decimal, order_id: str,
                     market: str = "KR") -> bool:
    """FillEvent를 claw:fill:queue에 lpush.

    exec_id 기반 setnx로 중복 push를 방지한다.
    Returns True if pushed, False if duplicate.
    """
    prefix = market.lower()
    exec_id = f"{prefix}_fill_{order_id}"
    dedupe_key = f"claw:fill_dedupe:{exec_id}"
    if not r.set(dedupe_key, "1", nx=True, ex=_FILL_DEDUPE_TTL):
        return False  # 이미 push된 이벤트

    ts_ms = str(int(time.time() * 1000))
    fill = {
        "exec_id": exec_id,
        "order_id": order_id,
        "symbol": symbol,
        "market": market,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "ts": ts_ms,
        "source": "position_exit_runner",
        "fee": "0",
        "retry": 0,
    }
    r.lpush(_FILL_QUEUE_KEY, json.dumps(fill))
    _log("fill_pushed", market=market, symbol=symbol, side=side,
         qty=str(qty), price=str(price), exec_id=exec_id)
    return True


# ---------------------------------------------------------------------------
# 포지션 동기화 (KIS → Redis)
# ---------------------------------------------------------------------------

def _load_cached_positions(r, market: str) -> dict:
    """Redis에 캐시된 포지션 읽기 (API 실패 시 fallback)."""
    result = {}
    idx_key = f"position_index:{market}"
    for b in (r.smembers(idx_key) or []):
        symbol = b.decode() if isinstance(b, bytes) else b
        raw = r.hgetall(f"position:{market}:{symbol}")
        if not raw:
            continue
        use_bytes = isinstance(next(iter(raw)), bytes)
        def d(k):
            key = k.encode() if use_bytes else k
            v = raw.get(key, b"" if use_bytes else "")
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


def _fetch_holdings(client, market: str) -> list[dict]:
    """market에 따라 적절한 holdings API 호출."""
    if market == "KR":
        return client.get_kr_holdings()
    elif market == "COIN":
        result = []
        for b in client.get_balances():
            currency = b.get("currency", "")
            if currency == "KRW":
                continue
            qty = Decimal(str(b.get("balance", "0")))
            avg_price = Decimal(str(b.get("avg_buy_price", "0")))
            if qty <= 0:
                continue
            result.append({"symbol": f"KRW-{currency}", "qty": qty, "avg_price": avg_price})
        return result
    else:
        return client.get_us_holdings()


def _sync_positions(r, client, market: str) -> dict:
    """거래소 잔고조회 → Redis position:{market}:{symbol} 동기화.

    API 실패 시 Redis 캐시 포지션으로 fallback (exit 조건 평가는 계속).

    Returns:
        {symbol: {"qty": Decimal, "avg_price": Decimal, "opened_ts": int}}
    """
    currency = "KRW" if market in ("KR", "COIN") else "USD"
    idx_key = f"position_index:{market}"

    global _ibkr_fallback_count
    try:
        holdings = _fetch_holdings(client, market)
        if market == "US":
            _ibkr_fallback_count = 0  # 성공 시 리셋
    except Exception as e:
        _log("sync_error_fallback_cache", market=market, reason=str(e))
        if market == "US":
            _ibkr_fallback_count += 1
        return _load_cached_positions(r, market)

    now_ms = int(time.time() * 1000)

    # 현재 Redis에 있는 포지션 목록
    existing: set[str] = set()
    for b in (r.smembers(idx_key) or []):
        existing.add(b.decode() if isinstance(b, bytes) else b)

    synced: dict = {}
    held_symbols: set[str] = set()

    for h in holdings:
        symbol = h["symbol"]
        qty = h["qty"]
        avg_price = h["avg_price"]
        held_symbols.add(symbol)

        pos_key = f"position:{market}:{symbol}"

        # opened_ts: 기존에 있으면 유지, 처음 보이면 현재 시각
        raw_opened = r.hget(pos_key, "opened_ts")
        if raw_opened:
            try:
                opened_ts = int(raw_opened.decode() if isinstance(raw_opened, bytes) else raw_opened)
            except Exception:
                opened_ts = int(time.time())
        else:
            opened_ts = int(time.time())

        # BUY fill detection: 새로 나타난 종목 (Redis에 없었던 것)
        # COIN: discovered BUY 스킵 — claw가 매수한 것만 추적 (기존 보유 코인 자동매도 방지)
        # 이미 Redis에 qty>0 포지션이 있으면 중복 fill 방지
        if symbol not in existing and market != "COIN":
            existing_qty_raw = r.hget(pos_key, "qty")
            already_tracked = False
            if existing_qty_raw is not None:
                try:
                    already_tracked = Decimal(
                        existing_qty_raw.decode() if isinstance(existing_qty_raw, bytes) else existing_qty_raw
                    ) > 0
                except Exception:
                    pass
            if not already_tracked:
                order_id = f"buy_discovered_{symbol}_{int(time.time())}"
                _push_fill_event(r, symbol, "BUY", qty, avg_price, order_id,
                                 market=market)
                r.set(f"claw:trail_hwm:{market}:{symbol}", str(avg_price), ex=_POSITION_TTL)

        # COIN: claw가 매수하지 않은 코인(position_index에 없음)은 추적 제외
        # 기존 보유 코인(SBD, APENFT 등) 자동매도 방지
        if market == "COIN" and symbol not in existing:
            continue

        # stop_pct/take_pct: 새 포지션이면 signal_pct에서 읽기, 기존이면 유지
        if symbol not in existing:
            pct_raw = r.hgetall(f"claw:signal_pct:{market}:{symbol}")
            def _dpct(k, default, _raw=pct_raw):
                if _raw:
                    use_bytes = isinstance(next(iter(_raw)), bytes)
                    key = k.encode() if use_bytes else k
                    v = _raw.get(key)
                    if v:
                        return v.decode() if isinstance(v, bytes) else v
                return default
            stop_pct_val = _dpct("stop_pct", "0.0200")
            take_pct_val = _dpct("take_pct", "0.0200")
            pos_mapping = {
                "qty": str(qty),
                "avg_price": str(avg_price),
                "opened_ts": str(opened_ts),
                "updated_ts": str(now_ms),
                "currency": currency,
                "stop_pct": stop_pct_val,
                "take_pct": take_pct_val,
            }
        else:
            pos_mapping = {
                "qty": str(qty),
                "avg_price": str(avg_price),
                "opened_ts": str(opened_ts),
                "updated_ts": str(now_ms),
                "currency": currency,
            }
        r.hset(pos_key, mapping=pos_mapping)
        r.expire(pos_key, _POSITION_TTL)
        r.sadd(idx_key, symbol)
        r.expire(idx_key, _POSITION_TTL)

        synced[symbol] = {"qty": qty, "avg_price": avg_price, "opened_ts": opened_ts}

    # Redis에는 있지만 잔고에 없는 종목 → 정리
    # SELL fill detection: 사라진 종목에 대해 최근 SELL order_meta 조회 후 FillEvent push
    for sym in existing:
        if sym not in held_symbols:
            # C2: BUY 주문 제출 후 아직 잔고에 반영 안 된 경우 → 건너뜀
            if r.exists(f"claw:buy_pending:{market}:{sym}"):
                _log("skip_buy_pending", market=market, symbol=sym)
                continue
            # 삭제 전에 포지션 정보 읽기
            pos_key = f"position:{market}:{sym}"
            raw_pos = r.hgetall(pos_key)
            cached_qty = Decimal("0")
            cached_avg_price = Decimal("0")
            if raw_pos:
                _pos_bytes = isinstance(next(iter(raw_pos)), bytes)
                def _d(k, _raw=raw_pos, _b=_pos_bytes):
                    key = k.encode() if _b else k
                    v = _raw.get(key, b"" if _b else "")
                    return v.decode() if isinstance(v, bytes) else (v or "")
                try:
                    cached_qty = Decimal(_d("qty") or "0")
                    cached_avg_price = Decimal(_d("avg_price") or "0")
                except Exception:
                    pass

            # 역방향 조회 키로 SELL order_id 직접 조회 (scan_iter 제거)
            sell_order_id = None
            sell_price = cached_avg_price  # fallback: avg_price
            try:
                exit_order_raw = r.get(f"claw:exit_order:{market}:{sym}")
                if exit_order_raw:
                    oid = exit_order_raw.decode() if isinstance(exit_order_raw, bytes) else exit_order_raw
                    meta = r.hgetall(f"claw:order_meta:{market}:{oid}")
                    if meta:
                        is_bytes = isinstance(next(iter(meta)), bytes)
                        def _dm(k, m=meta, b=is_bytes):
                            key = k.encode() if b else k
                            v = m.get(key, b"" if b else "")
                            return v.decode() if isinstance(v, bytes) else v
                        lp = _dm("limit_price")
                        if lp:
                            try:
                                sell_price = Decimal(lp)
                            except Exception:
                                pass
                        sell_order_id = oid
            except Exception as e:
                _log("sell_fill_meta_error", market=market, symbol=sym, error=str(e))

            if sell_order_id and cached_qty > 0:
                _push_fill_event(r, sym, "SELL", cached_qty, sell_price,
                                 sell_order_id, market=market)
            elif cached_qty > 0:
                # order_meta 없어도 FillEvent push (날짜 기반 결정론적 ID로 중복 방지)
                order_id = f"sell_discovered_{sym}_{today_kst()}"
                _push_fill_event(r, sym, "SELL", cached_qty, sell_price,
                                 order_id, market=market)

            # position_index에서 즉시 제거 (exit_runner 재처리 방지)
            # position hash는 60초 TTL 유지 — position_engine이 SELL fill 처리 시 avg_price 읽을 수 있도록
            pipe = r.pipeline()
            pipe.expire(f"position:{market}:{sym}", 60)
            pipe.srem(idx_key, sym)
            pipe.delete(f"claw:trail_hwm:{market}:{sym}")
            pipe.execute()
            _log("position_removed", market=market, symbol=sym,
                 reason="not_in_holdings")

    return synced


# ---------------------------------------------------------------------------
# 현재가 조회
# ---------------------------------------------------------------------------

def _get_mark_price(r, market: str, symbol: str):
    """mark:{market}:{symbol} 에서 현재가 조회 (MarketDataRunner가 갱신)."""
    raw = r.get(f"mark:{market}:{symbol}")
    if not raw:
        return None
    try:
        return Decimal(raw.decode() if isinstance(raw, bytes) else raw)
    except (InvalidOperation, Exception):
        return None


# ---------------------------------------------------------------------------
# Exit 조건 판단
# ---------------------------------------------------------------------------

def _check_exit(avg_price: Decimal, mark_price: Decimal, opened_ts: int, pos: dict = None, hwm_price: Decimal = None,
                stop_pct: Decimal = None, take_pct: Decimal = None, trail_pct: Decimal = None):
    """Exit 조건 확인. 조건 충족 시 reason 문자열 반환, 없으면 None.

    pos: position hash dict (str:str). stop_pct/take_pct가 있으면 동적 값 사용, 없으면 전역 fallback.
    hwm_price: trailing stop용 고점(High Water Mark). 제공 시 HWM 기반 trail stop 적용.
    stop_pct/take_pct/trail_pct: get_config()로 읽은 시장 전역 오버라이드 (pos hash보다 우선순위 낮음).
    """
    if avg_price <= 0 or mark_price <= 0:
        return None

    # 우선순위: cfg(get_config 오버라이드) > pos hash(per-signal 동적값) > 모듈 상수
    # stop_pct가 None이면 pos hash → 모듈 상수 순으로 fallback (테스트/직접호출용)
    if stop_pct is not None:
        _eff_stop = stop_pct
    elif pos:
        try:
            _eff_stop = Decimal(pos.get("stop_pct") or str(_STOP_LOSS_PCT))
        except Exception:
            _eff_stop = _STOP_LOSS_PCT
    else:
        _eff_stop = _STOP_LOSS_PCT

    if take_pct is not None:
        _eff_take = take_pct
    elif pos:
        try:
            _eff_take = Decimal(pos.get("take_pct") or str(_TAKE_PROFIT_PCT))
        except Exception:
            _eff_take = _TAKE_PROFIT_PCT
    else:
        _eff_take = _TAKE_PROFIT_PCT

    _eff_trail = trail_pct if trail_pct is not None else _TRAIL_STOP_PCT

    stop_price = avg_price * (1 - _eff_stop)

    # Trailing stop: HWM에서 trail_pct 이상 하락하면 청산 (floor = static stop)
    if hwm_price is not None and hwm_price > avg_price:
        trail_stop = hwm_price * (1 - _eff_trail)
        stop_price = max(stop_price, trail_stop)  # 더 높은(엄격한) stop 적용

    take_price = avg_price * (1 + _eff_take)
    # H2: opened_ts 호환 — >1e12이면 밀리초, 아니면 초
    now_ms = int(time.time() * 1000)
    if opened_ts > 1_000_000_000_000:
        held_sec = (now_ms - opened_ts) // 1000
    else:
        held_sec = int(time.time()) - opened_ts

    if mark_price <= stop_price:
        return f"stop_loss(mark={mark_price:.4f}<=stop={stop_price:.4f})"
    if mark_price >= take_price:
        return f"take_profit(mark={mark_price:.4f}>=take={take_price:.4f})"
    if held_sec >= _TIME_LIMIT_SEC:
        # 수익 중이면 TIME_LIMIT_MAX_SEC까지 연장 (추세 포지션 보호)
        if mark_price > avg_price and held_sec < _TIME_LIMIT_MAX_SEC:
            pass  # 연장: 아직 exit 조건 아님
        else:
            return f"time_limit(held={held_sec}s>={_TIME_LIMIT_SEC}s)"
    return None


# ---------------------------------------------------------------------------
# 매도 주문
# ---------------------------------------------------------------------------

def _place_sell(r, client, market: str, symbol: str, qty: Decimal,
                limit_price: Decimal, reason: str) -> bool:
    """SELL 주문 제출 + Redis order/meta 기록."""
    # KR: 정수, COIN: 소수점 8자리, US: 소수점 2자리
    if market == "KR":
        limit_price = limit_price.quantize(Decimal("1"))
    elif market == "COIN":
        limit_price = limit_price.quantize(Decimal("0.00000001"))
    else:
        limit_price = limit_price.quantize(Decimal("0.01"))

    client_order_id = str(uuid.uuid4())
    # COIN(Upbit): 시장가 매도 — 종목별 틱 단위 불일치로 지정가 400 오류 방지
    if market == "COIN":
        req = PlaceOrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=qty,
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
        )
    else:
        req = PlaceOrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            client_order_id=client_order_id,
        )
    try:
        result = client.place_order(req)
    except Exception as e:
        _log("sell_error", market=market, symbol=symbol, error=str(e))
        return False

    if result.status == OrderStatus.REJECTED:
        _log("sell_rejected", market=market, symbol=symbol,
             order_id=result.order_id)
        return False

    order_id = result.order_id

    # order_watcher가 TTL 취소 추적할 수 있도록 기록
    r.set(f"order:{market}:{order_id}", "SUBMITTED")
    r.expire(f"order:{market}:{order_id}", _ORDER_META_TTL)
    r.hset(f"claw:order_meta:{market}:{order_id}", mapping={
        "symbol": symbol,
        "side": "SELL",
        "qty": str(qty),
        "limit_price": str(limit_price),
        "exit_reason": reason,
        "first_seen_ts": str(int(time.time())),
        "source": "exit_runner",
    })
    r.expire(f"claw:order_meta:{market}:{order_id}", _ORDER_META_TTL)
    # 역방향 조회 키: symbol → order_id (scan_iter 없이 직접 조회)
    r.set(f"claw:exit_order:{market}:{symbol}", order_id, ex=_ORDER_META_TTL)

    _log("sell_submitted",
         market=market, symbol=symbol, order_id=order_id,
         qty=str(qty), price=str(limit_price), reason=reason)

    # SELL 주문접수 알림
    try:
        currency = "KRW" if market in ("KR", "COIN") else "USD"
        send_telegram(
            f"[CLAW] SELL 주문접수\n"
            f"market={market} symbol={symbol}\n"
            f"qty={qty} price={limit_price} {currency}\n"
            f"reason={reason} order_id={order_id}"
        )
    except Exception:
        pass

    return True


# ---------------------------------------------------------------------------
# 핵심: 1회 실행
# ---------------------------------------------------------------------------

def _run_market(r, client, market: str) -> None:
    """단일 market의 포지션 동기화 → exit 조건 체크 → 필요 시 매도."""
    # 장중에만 exit 평가 (time_limit 스팸 방지 + 장외 주문 방지)
    if not is_market_hours(market):
        return

    positions = _sync_positions(r, client, market)
    if not positions:
        return

    # H5: IBKR 연속 fallback 시 exit 평가 스킵 (캐시 포지션으로 매도 방지)
    if market == "US" and _ibkr_fallback_count >= _IBKR_FALLBACK_MAX:
        _log("skip_exit_ibkr_fallback", market=market, fallback_count=_ibkr_fallback_count)
        return

    # Redis config 오버라이드 읽기 (매 폴링마다 갱신)
    # TG /claw set으로 명시적 override가 있을 때만 cfg 값 사용 (없으면 None → per-signal 동적값 사용)
    _config_key = f"claw:config:{market}"

    def _cfg_or_none(field: str) -> "Decimal | None":
        raw = r.hget(_config_key, field)
        if raw is None:
            return None
        try:
            return Decimal(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            return None

    cfg_stop = _cfg_or_none("stop_pct")
    cfg_take = _cfg_or_none("take_pct")
    cfg_trail = _cfg_or_none("trail_pct")

    # 가격 quantize 단위: KR=정수, COIN=소수점 8자리, US=소수점 2자리
    if market == "KR":
        q_unit = Decimal("1")
    elif market == "COIN":
        q_unit = Decimal("0.00000001")
    else:
        q_unit = Decimal("0.01")

    for symbol, pos in positions.items():
        qty = pos["qty"]
        avg_price = pos["avg_price"]
        opened_ts = pos["opened_ts"]

        if qty <= 0:
            continue

        # 이미 매도 주문 진행 중이면 skip — 단, SELL 주문이 CANCELED이면 lock 해제 후 재시도
        lock_key = f"claw:exit_lock:{market}:{symbol}"
        if r.exists(lock_key):
            exit_order_raw = r.get(f"claw:exit_order:{market}:{symbol}")
            if exit_order_raw:
                oid = exit_order_raw.decode() if isinstance(exit_order_raw, bytes) else exit_order_raw
                order_status = r.get(f"order:{market}:{oid}")
                if order_status and order_status.decode() == "CANCELED":
                    r.delete(lock_key)
                    _log("sell_retry_after_cancel", market=market, symbol=symbol, order_id=oid)
                else:
                    continue
            else:
                continue

        mark_price = _get_mark_price(r, market, symbol)
        if mark_price is None or mark_price <= 0:
            _log("no_mark_price", market=market, symbol=symbol)
            continue

        # position hash 전체 읽기 (stop_pct/take_pct 포함)
        pos_key = f"position:{market}:{symbol}"
        pos_hash_raw = r.hgetall(pos_key)
        pos_hash: dict = {}
        if pos_hash_raw:
            for k, v in pos_hash_raw.items():
                dk = k.decode() if isinstance(k, bytes) else k
                dv = v.decode() if isinstance(v, bytes) else v
                pos_hash[dk] = dv

        # Trailing stop용 HWM(고점) 갱신
        hwm_key = f"claw:trail_hwm:{market}:{symbol}"
        hwm_raw = r.get(hwm_key)
        try:
            prev_hwm = Decimal(hwm_raw.decode()) if hwm_raw else mark_price
        except Exception:
            prev_hwm = mark_price
        hwm_price = max(prev_hwm, mark_price)
        r.set(hwm_key, str(hwm_price), ex=_POSITION_TTL)

        reason = _check_exit(avg_price, mark_price, opened_ts, pos=pos_hash, hwm_price=hwm_price,
                             stop_pct=cfg_stop, take_pct=cfg_take, trail_pct=cfg_trail)
        if reason is None:
            pnl_pct = float((mark_price - avg_price) / avg_price * 100)
            # H2: opened_ts 호환
            if opened_ts > 1_000_000_000_000:
                held_sec = (int(time.time() * 1000) - opened_ts) // 1000
            else:
                held_sec = int(time.time()) - opened_ts
            try:
                _stop_pct = Decimal(pos_hash.get("stop_pct") or str(_STOP_LOSS_PCT))
                _take_pct = Decimal(pos_hash.get("take_pct") or str(_TAKE_PROFIT_PCT))
            except Exception:
                _stop_pct = _STOP_LOSS_PCT
                _take_pct = _TAKE_PROFIT_PCT
            _log("hold", market=market, symbol=symbol,
                 avg=str(avg_price), mark=str(mark_price),
                 pnl_pct=f"{pnl_pct:+.2f}%",
                 held_sec=held_sec,
                 stop=str((avg_price * (1 - _stop_pct)).quantize(q_unit)),
                 take=str((avg_price * (1 + _take_pct)).quantize(q_unit)))
            continue

        # Exit 조건 충족 → 매도 lock 획득 후 주문
        lock_key = f"claw:exit_lock:{market}:{symbol}"
        if not r.set(lock_key, "1", nx=True, ex=_EXIT_LOCK_TTL):
            # 다른 프로세스/사이클이 이미 lock 획득 → 중복 방지
            _log("exit_lock_held_skip", market=market, symbol=symbol)
            continue

        _log("exit_triggered", market=market, symbol=symbol, reason=reason,
             avg=str(avg_price), mark=str(mark_price), qty=str(qty))

        # H4: stop_loss/time_limit 매도 시 0.3% 낮은 가격으로 지정가 (체결 확률 향상)
        if "take_profit" in reason:
            sell_price = mark_price
        else:
            sell_price = mark_price * Decimal("0.997")

        ok = _place_sell(r, client, market, symbol, qty, sell_price, reason)
        if ok and "stop_loss" in reason:
            # 당일 재진입 차단 마킹
            today = today_kst()
            r.set(f"claw:daily_stop:{market}:{symbol}:{today}", "1", ex=86400)
            _log("daily_stop_marked", market=market, symbol=symbol, today=today)
        if not ok:
            # 주문 실패 시 lock 해제 → 다음 폴링에서 재시도
            r.delete(lock_key)


def run_once(r, kis: KisClient, ibkr: IbkrClient = None, upbit: UpbitClient = None) -> None:
    """KR/US/COIN 포지션 동기화 → exit 조건 체크 → 필요 시 매도."""
    _run_market(r, kis, "KR")
    if ibkr is not None:
        _run_market(r, ibkr, "US")
    if upbit is not None:
        _run_market(r, upbit, "COIN")


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

    # IBKR: IBKR_ACCOUNT_ID가 설정되어 있으면 US 시장도 처리
    ibkr = None
    if os.getenv("IBKR_ACCOUNT_ID"):
        try:
            ibkr = IbkrClient()
            _log("ibkr_enabled")
        except Exception as e:
            _log("ibkr_init_failed", error=str(e))

    # Upbit: UPBIT_ACCESS_KEY가 설정되어 있으면 COIN 시장도 처리
    upbit = None
    if os.getenv("UPBIT_ACCESS_KEY"):
        try:
            upbit = UpbitClient()
            _log("upbit_enabled")
        except Exception as e:
            _log("upbit_init_failed", error=str(e))

    markets = ["KR"] + (["US"] if ibkr else []) + (["COIN"] if upbit else [])
    print(
        f"exit_runner: started "
        f"markets={markets} "
        f"poll_sec={_POLL_SEC} "
        f"stop_loss={float(_STOP_LOSS_PCT)*100:.1f}% "
        f"take_profit={float(_TAKE_PROFIT_PCT)*100:.1f}% "
        f"time_limit={_TIME_LIMIT_SEC}s "
        f"trail_stop={float(_TRAIL_STOP_PCT)*100:.1f}% "
        f"time_limit_max={_TIME_LIMIT_MAX_SEC}s "
        f"lock_ttl={_LOCK_TTL}s",
        flush=True,
    )

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            try:
                run_once(r, kis, ibkr, upbit)
            except Exception as e:
                _log("unexpected_error", error=str(e))
            time.sleep(_POLL_SEC)
    finally:
        r.delete(_LOCK_KEY)
        print("exit_runner: lock released", flush=True)


if __name__ == "__main__":
    main()
