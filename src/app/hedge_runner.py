"""hedge_runner — KOSPI 급락 시 인버스 ETF 자동 헤지.

포지션 보유 중 워치리스트 평균 ret_5m < -1% → 114800 BUY 신호 push.

기동:
    PYTHONPATH=src venv/bin/python -m app.hedge_runner
"""
from dotenv import load_dotenv
load_dotenv()

import json
import logging
import os
import time
import uuid

import redis

logger = logging.getLogger(__name__)

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
    """signal:{market} 큐에 헤지 BUY 신호 push."""
    signal_id = f"HEDGE-{uuid.uuid4().hex[:8]}"
    signal = {
        "signal_id": signal_id,
        "market": market,
        "symbol": symbol,
        "direction": "LONG",
        "entry": {
            "price": str(price),
            "size_cash": str(size_cash),
        },
        "stop_pct": "0.02",
        "take_pct": "0.03",
        "source": "hedge",
    }
    r.lpush(f"signal:{market}", json.dumps(signal))
    logger.info(f"hedge_runner: pushed hedge signal signal_id={signal_id} symbol={symbol} price={price} size={size_cash}")


def run_once(r, market: str = "KR") -> bool:
    """헤지 조건 체크. 발동 시 True 반환."""
    lock_key = f"claw:hedge:lock:{market}"
    if r.exists(lock_key):
        return False

    if not _has_long_positions(r, market):
        return False

    avg_ret = _avg_market_ret(r, market)
    if avg_ret is None or avg_ret >= _HEDGE_TRIGGER_RET:
        return False

    # 인버스 ETF 이미 보유 중이면 스킵
    pos = r.hgetall(f"position:{market}:{_HEDGE_SYMBOL}")
    if pos:
        qty_raw = pos.get(b"qty") or pos.get("qty", b"0")
        if float(qty_raw.decode() if isinstance(qty_raw, bytes) else qty_raw) > 0:
            return False

    price = _get_mark_price(r, market, _HEDGE_SYMBOL)
    if not price or price <= 0:
        logger.warning(f"hedge_runner: no mark price for {_HEDGE_SYMBOL}, skipping")
        return False

    _push_hedge_signal(r, market, _HEDGE_SYMBOL, price, _HEDGE_SIZE_CASH)
    r.set(lock_key, "1", ex=_HEDGE_LOCK_TTL)

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


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("hedge_runner: REDIS_URL not set — exiting", flush=True)
        return

    r = redis.from_url(redis_url)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"hedge_runner: started poll={_POLL_SEC}s trigger={_HEDGE_TRIGGER_RET:.1%} hedge_symbol={_HEDGE_SYMBOL}", flush=True)

    while True:
        try:
            run_once(r, "KR")
        except Exception as e:
            logger.error(f"hedge_runner: error {e}", exc_info=True)
        time.sleep(_POLL_SEC)


if __name__ == "__main__":
    main()
