"""consensus_signal_runner — Phase 10

dual-LLM 합의 결과를 읽어 최소 prefilter 통과 시
전체 Signal 객체를 생성하고 claw:signal:queue에 push한다.

책임 범위:
  - dual consensus 확인 (claude_emit == 1 AND qwen_emit == 1 AND 방향 일치)
  - entry prefilter (ret_5m > 0, range_5m > 0.004)
  - 데이터 무결성 확인
  - Signal 정규화 (Pydantic 검증 포함)
  - claw:signal:queue enqueue
  - 감사 로그 / 통계

책임 외:
  - session gating (StrategyEngine)
  - cooldown / re-entry (StrategyEngine)
  - position 존재 여부 (RiskEngine)
  - max_positions / daily loss (RiskEngine)
  - 주문 실행 (OrderExecutor)
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
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

import redis

from domain.models import Signal, SignalEntry, SignalStop
from utils.redis_helpers import parse_watchlist, load_watchlist, today_kst, is_market_hours, is_paused

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "consensus:runner:lock"
_LOCK_TTL = 120  # seconds

_POLL_SEC = float(os.getenv("CONSENSUS_POLL_SEC", "30"))

# Phase 10 prefilter 기준값 (Phase 11: ret_5m threshold 강화 0.0 → 0.001)
_MIN_RET_5M = float(os.getenv("CONSENSUS_MIN_RET_5M", "0.001"))
_MIN_RANGE_5M = 0.004

# Phase 11: symbol-level cooldown (같은 종목 N초 내 재emit 방지)
_SYMBOL_COOLDOWN_SEC = int(os.getenv("CONSENSUS_SYMBOL_COOLDOWN_SEC", "180"))

# stop loss 비율 (-2%)
_STOP_PCT = Decimal("0.02")

_AUDIT_TTL = 7 * 86400   # 7일
_STATS_TTL = 30 * 86400


# ---------------------------------------------------------------------------
# KR 호가 단위 정규화 (내림)
# ---------------------------------------------------------------------------

def normalize_kr_price_tick(price: Decimal) -> Decimal:
    """KR 주식 호가 단위에 맞게 내림 처리."""
    p = int(price)
    if p < 1_000:
        tick = 1
    elif p < 5_000:
        tick = 5
    elif p < 10_000:
        tick = 10
    elif p < 50_000:
        tick = 50
    elif p < 100_000:
        tick = 100
    elif p < 500_000:
        tick = 500
    else:
        tick = 1_000
    return Decimal((p // tick) * tick)


# ---------------------------------------------------------------------------
# 로깅 헬퍼
# ---------------------------------------------------------------------------

def _log(event: str, **kwargs) -> None:
    parts = [event] + [f"{k}={v}" for k, v in kwargs.items()]
    print(f"consensus: {' '.join(parts)}", flush=True)


# ---------------------------------------------------------------------------
# Redis 유틸
# ---------------------------------------------------------------------------

def _decode(v) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v) if v is not None else ""


def _hgetall_str(r, key: str) -> dict:
    """hgetall 결과를 str:str dict로 반환."""
    raw = r.hgetall(key)
    if not raw:
        return {}
    return {_decode(k): _decode(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# 감사 / 통계
# ---------------------------------------------------------------------------

def _save_audit(r, market: str, signal: Signal, ret_5m: float, range_5m: float) -> None:
    today = today_kst()
    payload = {
        "signal_id": signal.signal_id,
        "market": market,
        "symbol": signal.symbol,
        "ts": signal.ts,
        "direction": signal.direction,
        "entry_price": str(signal.entry.price),
        "stop_price": str(signal.stop.price),
        "ret_5m": str(ret_5m),
        "range_5m": str(range_5m),
        "source": "consensus_signal_runner",
    }
    audit_key = f"consensus:audit:{market}:{signal.signal_id}"
    r.set(audit_key, json.dumps(payload), ex=_AUDIT_TTL)

    # 일별 통계 카운터
    stats_key = f"consensus:stats:{market}:{today}"
    r.hincrby(stats_key, "candidate", 1)
    r.expire(stats_key, _STATS_TTL)

    # 일별 candidate 생성 수
    daily_key = f"consensus:daily_count:{market}:{today}"
    r.incr(daily_key)
    r.expire(daily_key, _STATS_TTL)


def _record_reject(r, market: str, reason_code: str) -> None:
    today = today_kst()
    stats_key = f"consensus:stats:{market}:{today}"
    r.hincrby(stats_key, reason_code, 1)
    r.expire(stats_key, _STATS_TTL)


# ---------------------------------------------------------------------------
# 핵심 처리: 심볼 1개
# ---------------------------------------------------------------------------

def run_once(market: str, symbol: str, r) -> Optional[dict]:
    """
    dual eval Redis 결과를 읽어 candidate Signal을 생성하고 queue에 push.

    Returns:
        signal dict (push 성공 시) or None (reject / 오류 시)
    """
    # 1. dual eval 결과 읽기
    claude = _hgetall_str(r, f"ai:dual:last:claude:{market}:{symbol}")
    qwen   = _hgetall_str(r, f"ai:dual:last:qwen:{market}:{symbol}")

    if not claude or not qwen:
        return None  # 아직 평가 결과 없음 — 무시 (cold start)

    # 2. dedup: 이미 이 ts_ms로 처리한 결과면 스킵 (중복 push 방지)
    # dual eval runner가 새 결과를 쓰기 전까지 동일 hash 반복 읽힘
    c_ts_ms = claude.get("ts_ms", "")
    q_ts_ms = qwen.get("ts_ms", "")
    seen_key = f"consensus:seen:{market}:{symbol}:{c_ts_ms}:{q_ts_ms}"
    if not r.set(seen_key, "1", nx=True, ex=int(_POLL_SEC * 6)):
        return None  # 이미 처리한 eval 결과

    # 3. 데이터 무결성: features_json 파싱
    try:
        c_features = json.loads(claude.get("features_json") or "{}")
        q_features = json.loads(qwen.get("features_json") or "{}")
    except (json.JSONDecodeError, Exception) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"features_json parse error: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 3. dual consensus 확인 (emit)
    c_emit = claude.get("emit") == "1"
    q_emit = qwen.get("emit") == "1"
    if not c_emit or not q_emit:
        _log(
            "runner.reject.consensus_failed",
            symbol=symbol,
            claude_emit=claude.get("emit"),
            qwen_emit=qwen.get("emit"),
        )
        _record_reject(r, market, "reject_consensus_failed")
        return None

    # 4. 방향 일치 확인
    c_dir = claude.get("direction", "")
    q_dir = qwen.get("direction", "")
    if not c_dir or c_dir != q_dir:
        _log("runner.reject.direction_mismatch", symbol=symbol, claude_dir=c_dir, qwen_dir=q_dir)
        _record_reject(r, market, "reject_direction_mismatch")
        return None

    # Phase 10: LONG 방향만 처리
    if c_dir != "LONG":
        _log("runner.reject.direction_not_long", symbol=symbol, direction=c_dir)
        _record_reject(r, market, "reject_direction_not_long")
        return None

    # 4-b. Phase 11: symbol-level cooldown (같은 종목 N초 내 재emit 방지)
    cooldown_key = f"consensus:symbol_cooldown:{market}:{symbol}"
    if not r.set(cooldown_key, "1", nx=True, ex=_SYMBOL_COOLDOWN_SEC):
        _log("runner.reject.symbol_cooldown", symbol=symbol, cooldown_sec=_SYMBOL_COOLDOWN_SEC)
        _record_reject(r, market, "reject_symbol_cooldown")
        return None

    # 5. prefilter: ret_5m, range_5m
    try:
        ret_5m_raw   = c_features.get("ret_5m")
        range_5m_raw = c_features.get("range_5m")
        if ret_5m_raw is None or range_5m_raw is None:
            raise ValueError("ret_5m or range_5m missing in features_json")
        ret_5m   = float(ret_5m_raw)
        range_5m = float(range_5m_raw)
    except (TypeError, ValueError) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=str(e))
        _record_reject(r, market, "reject_invalid_payload")
        return None

    if ret_5m <= _MIN_RET_5M:
        _log("runner.reject.prefilter_ret_5m", symbol=symbol, ret_5m=ret_5m)
        _record_reject(r, market, "reject_prefilter_ret_5m")
        return None

    if range_5m <= _MIN_RANGE_5M:
        _log("runner.reject.prefilter_range_5m", symbol=symbol, range_5m=range_5m)
        _record_reject(r, market, "reject_prefilter_range_5m")
        return None

    # 6. current_price 추출 및 검증
    try:
        price_raw = c_features.get("current_price")
        if not price_raw:
            raise ValueError("current_price missing in features_json")
        current_price = Decimal(str(price_raw))
        if current_price <= 0:
            raise ValueError(f"current_price must be positive: {current_price}")
    except (InvalidOperation, ValueError) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"current_price: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 7. stop price 계산 (KR 호가 단위 정규화)
    stop_raw = current_price * (1 - _STOP_PCT)
    stop_price = normalize_kr_price_tick(stop_raw)
    if stop_price <= 0:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"stop_price={stop_price} <= 0")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 8. Signal 객체 생성 (Pydantic 검증 포함)
    signal_id = str(uuid.uuid4())
    ts = datetime.now(_KST).isoformat()

    try:
        signal = Signal(
            signal_id=signal_id,
            ts=ts,
            market=market,
            symbol=symbol,
            direction="LONG",
            entry=SignalEntry(
                price=current_price,
                size_cash=current_price,   # 1주: qty = size_cash / price = 1
            ),
            stop=SignalStop(price=stop_price),
        )
    except Exception as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"Signal validation: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 9. claw:signal:queue enqueue
    payload = {
        "signal_id": signal.signal_id,
        "ts": signal.ts,
        "market": signal.market,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry": {
            "price": str(signal.entry.price),
            "size_cash": str(signal.entry.size_cash),
        },
        "stop": {"price": str(signal.stop.price)},
        "source": "consensus_signal_runner",
        "status": "candidate",
        "consensus": "EMIT",
        "claude_emit": 1,
        "qwen_emit": 1,
        "ret_5m": ret_5m,
        "range_5m": range_5m,
    }

    try:
        r.lpush("claw:signal:queue", json.dumps(payload))
    except Exception as e:
        _log("runner.error.publish_failed", symbol=symbol, signal_id=signal_id, error=str(e))
        return None

    # 10. 감사 로그 / 통계
    _save_audit(r, market, signal, ret_5m, range_5m)

    _log(
        "runner.pass.candidate_created",
        signal_id=signal_id,
        symbol=symbol,
        market=market,
        entry_price=str(current_price),
        stop_price=str(stop_price),
        ret_5m=f"{ret_5m:.4f}",
        range_5m=f"{range_5m:.4f}",
    )

    return payload


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("consensus: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 프로세스 락
    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("consensus: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("consensus: SIGTERM received, lock released", flush=True)
        sys.exit(0)

    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
    if not watchlist_kr:
        print("consensus: GEN_WATCHLIST_KR empty — exiting", flush=True)
        r.delete(_LOCK_KEY)
        sys.exit(1)

    print(
        f"consensus: started poll_sec={_POLL_SEC} "
        f"prefilter=ret_5m>{_MIN_RET_5M} range_5m>{_MIN_RANGE_5M} "
        f"stop_pct={_STOP_PCT} watchlist_kr={watchlist_kr}",
        flush=True,
    )

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            # 장외 시간 또는 pause 상태면 스킵
            if not is_market_hours("KR"):
                time.sleep(_POLL_SEC)
                continue
            if is_paused(r):
                time.sleep(_POLL_SEC)
                continue

            # 동적 워치리스트 갱신
            watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")

            for symbol in watchlist_kr:
                try:
                    run_once("KR", symbol, r)
                except Exception as e:
                    _log("runner.error.unexpected", symbol=symbol, error=str(e))

            time.sleep(_POLL_SEC)

    finally:
        r.delete(_LOCK_KEY)
        print("consensus: lock released", flush=True)


if __name__ == "__main__":
    main()
