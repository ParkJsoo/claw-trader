"""hedge_runner — KOSPI 급락 시 인버스 ETF 자동 헤지.

포지션 보유 중 워치리스트 평균 ret_5m < -1% → 114800 BUY 신호 push.

기동:
    PYTHONPATH=src venv/bin/python -m app.hedge_runner
"""
from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

_KST = ZoneInfo("Asia/Seoul")

_HEDGE_SYMBOL = os.getenv("HEDGE_SYMBOL_KR", "114800")
_HEDGE_TRIGGER_RET = float(os.getenv("HEDGE_TRIGGER_RET", "-0.01"))  # -1% 급락
_HEDGE_SIZE_CASH = float(os.getenv("HEDGE_SIZE_CASH", "100000"))  # 10만원
_HEDGE_LOCK_TTL = int(os.getenv("HEDGE_LOCK_TTL", "3600"))  # 1시간 재발동 금지
_POLL_SEC = float(os.getenv("HEDGE_POLL_SEC", "60"))

_INVERSE_ETF_KR = set(os.getenv("INVERSE_ETF_KR", "114800,251340").split(","))


def _has_long_positions(r, market: str) -> bool:
    """LONG 포지션 (인버스 ETF 제외) 존재 여부."""
    symbols = r.smembers(f"position_index:{market}")
    for sym_bytes in symbols:
        sym = sym_bytes.decode() if isinstance(sym_bytes, bytes) else sym_bytes
        if sym in _INVERSE_ETF_KR:
            continue
        pos = r.hgetall(f"position:{market}:{sym}")
        if pos:
            qty_raw = pos.get(b"qty") or pos.get("qty", b"0")
            qty = float(qty_raw.decode() if isinstance(qty_raw, bytes) else qty_raw)
            if qty > 0:
                return True
    return False


def _avg_market_ret(r, market: str) -> float | None:
    """워치리스트 종목 평균 ret_5m. 데이터 3개 미만이면 None."""
    from utils.redis_helpers import load_watchlist
    env_key = f"GEN_WATCHLIST_{market}"
    watchlist = load_watchlist(r, market, env_key)
    rets = []
    for sym in watchlist:
        if sym in _INVERSE_ETF_KR:
            continue
        raw = r.hget(f"ai:dual:last:claude:{market}:{sym}", "features_json")
        if not raw:
            continue
        try:
            features = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            ret_5m = float(features.get("ret_5m", 0))
            rets.append(ret_5m)
        except Exception:
            continue
    if len(rets) < 3:
        return None
    return sum(rets) / len(rets)


def _get_mark_price(r, market: str, symbol: str) -> float | None:
    """Redis에서 현재가 조회."""
    raw = r.get(f"mark:{market}:{symbol}")
    if not raw:
        return None
    try:
        return float(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, TypeError):
        return None


def _push_hedge_signal(r, market: str, symbol: str, price: float, size_cash: float) -> None:
    """claw:signal:queue에 헤지 BUY 신호 push."""
    signal_id = f"HEDGE-{uuid.uuid4().hex[:8]}"
    stop_price = round(price * 0.98, 0)
    signal = {
        "signal_id": signal_id,
        "ts": datetime.now(_KST).isoformat(),
        "market": market,
        "symbol": symbol,
        "direction": "LONG",
        "entry": {
            "price": str(price),
            "size_cash": str(size_cash),
        },
        "stop": {"price": str(stop_price)},
        "stop_pct": "0.02",
        "take_pct": "0.03",
        "source": "hedge",
    }
    r.lpush("claw:signal:queue", json.dumps(signal))
    print(f"hedge_runner: pushed hedge signal signal_id={signal_id} symbol={symbol} price={price} size={size_cash}", flush=True)


def run_once(r, market: str = "KR") -> bool:
    """헤지 조건 체크. 발동 시 True 반환."""
    lock_key = f"claw:hedge:lock:{market}"

    # lock 선획득 (atomic) — 이미 실행 중이거나 쿨다운 중이면 즉시 False
    if not r.set(lock_key, "1", nx=True, ex=_HEDGE_LOCK_TTL):
        return False

    try:
        if not _has_long_positions(r, market):
            r.delete(lock_key)
            return False

        avg_ret = _avg_market_ret(r, market)
        if avg_ret is None or avg_ret >= _HEDGE_TRIGGER_RET:
            r.delete(lock_key)
            return False

        # 인버스 ETF 이미 보유 중이면 스킵
        pos = r.hgetall(f"position:{market}:{_HEDGE_SYMBOL}")
        if pos:
            qty_raw = pos.get(b"qty") or pos.get("qty", b"0")
            if float(qty_raw.decode() if isinstance(qty_raw, bytes) else qty_raw) > 0:
                r.delete(lock_key)
                return False

        price = _get_mark_price(r, market, _HEDGE_SYMBOL)
        if not price or price <= 0:
            print(f"hedge_runner: no mark price for {_HEDGE_SYMBOL}, skipping", flush=True)
            r.delete(lock_key)
            return False

        _push_hedge_signal(r, market, _HEDGE_SYMBOL, price, _HEDGE_SIZE_CASH)
        # lock은 유지 (TTL 동안 재발동 방지)

        try:
            from guards.notifier import send_telegram
            send_telegram(
                f"[CLAW] 헤지 발동\n"
                f"market={market} avg_ret_5m={avg_ret:.2%}\n"
                f"-> {_HEDGE_SYMBOL} BUY {_HEDGE_SIZE_CASH:,.0f}원"
            )
        except Exception:
            pass

        return True
    except Exception as e:
        r.delete(lock_key)  # 예외 시 lock 해제
        print(f"hedge_runner: run_once error {e}", flush=True)
        return False


_LOCK_KEY = "hedge_runner:lock:KR"
_LOCK_TTL = 120


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("hedge_runner: REDIS_URL not set — exiting", flush=True)
        return

    r = redis.from_url(redis_url)

    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("hedge_runner: already running — exiting", flush=True)
        return

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    print(f"hedge_runner: started poll={_POLL_SEC}s trigger={_HEDGE_TRIGGER_RET:.1%} hedge_symbol={_HEDGE_SYMBOL}", flush=True)

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            try:
                run_once(r, "KR")
            except Exception as e:
                print(f"hedge_runner: error {e}", flush=True)
            time.sleep(_POLL_SEC)
    finally:
        r.delete(_LOCK_KEY)


if __name__ == "__main__":
    main()
