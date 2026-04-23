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
import builtins as _builtins
import sys
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

# 모든 print에 타임스탬프 자동 prefix
_orig_print = _builtins.print
def print(*args, sep=' ', end='\n', file=None, flush=False):  # noqa: A001
    _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args and isinstance(args[0], str):
        _orig_print(f"[{_ts}] {args[0]}", *args[1:], sep=sep, end=end, file=file, flush=flush)
    else:
        _orig_print(f"[{_ts}]", *args, sep=sep, end=end, file=file, flush=flush)

import redis

from app.coin_type_b_gate_profiles import alt_shadow_profile_names, first_gate_fail_reason, scenario_map
from app.coin_research import save_pre_consensus_signal_snapshot, save_signal_snapshot
from domain.models import Signal, SignalEntry, SignalStop
from utils.redis_helpers import (
    parse_watchlist,
    load_watchlist,
    today_kst,
    is_market_hours,
    is_paused,
    get_signal_family_mode,
)

# H1: 잔고 비율 기반 size_cash 계산
_SIZE_CASH_PCT_KR = float(os.getenv("CONSENSUS_KR_SIZE_CASH_PCT", "0.30"))
_SIZE_CASH_PCT_US = float(os.getenv("CONSENSUS_US_SIZE_CASH_PCT", "0.30"))
_SIZE_CASH_PCT_COIN = float(os.getenv("CONSENSUS_COIN_SIZE_CASH_PCT", "0.30"))
_COIN_PRE_SHADOW_SIZE_CASH = Decimal(os.getenv("COIN_PRE_SHADOW_SIZE_CASH", "100000"))

# KisClient / IbkrClient 싱글톤 캐시 (프로세스 재시작 시 초기화)
_client_cache: dict[str, object] = {}

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "consensus:runner:lock"
_LOCK_TTL = 120  # seconds

_POLL_SEC = float(os.getenv("CONSENSUS_POLL_SEC", "30"))

# momentum breakout prefilter: 5분 급등 최소 기준
_MIN_SURGE_5M = float(os.getenv("MB_MIN_SURGE_5M", "0.020"))        # legacy fallback
_MIN_SURGE_5M_COIN = float(os.getenv("MB_MIN_SURGE_5M_COIN", str(_MIN_SURGE_5M)))
_MIN_SURGE_5M_US = float(os.getenv("MB_MIN_SURGE_5M_US", str(_MIN_SURGE_5M)))
_MIN_SURGE_5M_KR = float(os.getenv("MB_MIN_SURGE_5M_KR", "0.030"))  # KR 3.0% (노이즈 필터링)
_MAX_SURGE_5M = float(os.getenv("MB_MAX_SURGE_5M", "0.035"))       # 5분 급등 상한 (꼭지 매수 방지)
_MIN_RANGE_5M = float(os.getenv("MB_MIN_RANGE_5M", "0.004"))

# Phase 11: symbol-level cooldown (같은 종목 N초 내 재emit 방지)
_SYMBOL_COOLDOWN_SEC = int(os.getenv("CONSENSUS_SYMBOL_COOLDOWN_SEC", "180"))

# momentum breakout prefilter: 1분 급등 최소 기준
_MIN_RET_1M = float(os.getenv("MB_MIN_RET_1M", "0.002"))  # 0.2%
_MIN_RET_1M_COIN = float(os.getenv("MB_MIN_RET_1M_COIN", "0.004"))
_COIN_MIN_RET1M_TO_RET5M_RATIO = float(os.getenv("COIN_MIN_RET1M_TO_RET5M_RATIO", "0.35"))

# Phase 17: 신호 품질 강화
_MIN_RET_15M = float(os.getenv("CONSENSUS_MIN_RET_15M", "0.0"))  # 15분 추세: 0 이상이어야 함
_VOLUME_SURGE_RATIO = float(os.getenv("CONSENSUS_VOLUME_SURGE_RATIO", "1.5"))  # COIN/US 거래량 배수
_VOLUME_SURGE_RATIO_KR = float(os.getenv("CONSENSUS_VOLUME_SURGE_RATIO_KR", "1.2"))  # KR 거래량 배수
_VOLUME_SURGE_MIN_SAMPLES = int(os.getenv("CONSENSUS_VOLUME_SURGE_MIN_SAMPLES", "3"))
_VOLUME_SURGE_MIN_SAMPLES_KR = int(os.getenv("CONSENSUS_VOLUME_SURGE_MIN_SAMPLES_KR", "0"))
_VOLUME_LOOKBACK_DAYS = int(os.getenv("VOLUME_LOOKBACK_DAYS", "7"))  # 5 → 7 (주말 포함)
_CLAUDE_ONLY = os.getenv("EXECUTION_MODE", "dual").lower() == "claude_only"  # Qwen 무시

_BULLISH_THRESHOLD = float(os.getenv("REGIME_BULLISH_THRESHOLD", "0.30"))  # bearish 비율 < 30% → bullish

# Type B: 추세 탑승 신호 파라미터
_TYPE_B_POLL_SEC = float(os.getenv("TYPE_B_POLL_SEC", "300"))           # 5분마다 체크
_TYPE_B_COOLDOWN_SEC = int(os.getenv("TYPE_B_COOLDOWN_SEC", "14400"))   # 4시간 쿨다운
_TYPE_B_MIN_CHANGE_RATE = float(os.getenv("TYPE_B_MIN_CHANGE_RATE", "0.04"))   # 전일대비 +4%
_TYPE_B_MAX_CHANGE_RATE = float(os.getenv("TYPE_B_MAX_CHANGE_RATE", "0.12"))   # 전일대비 +12% 초과 late-chase 차단
_TYPE_B_MIN_VOL_KRW = float(os.getenv("TYPE_B_MIN_VOL_KRW", "10000000000"))    # 24h 거래대금 100억
_TYPE_B_NEAR_HIGH_RATIO = float(os.getenv("TYPE_B_NEAR_HIGH_RATIO", "0.97"))   # 당일 고점 -3% 이내
_TYPE_B_MIN_RET_5M = float(os.getenv("TYPE_B_MIN_RET_5M", "0.005"))            # 5분 ret 0.5% 이상
_TYPE_B_MAX_RET_5M = float(os.getenv("TYPE_B_MAX_RET_5M", "0.025"))            # 5분 ret 2.5% 초과 late-chase 차단
_TYPE_B_REQUIRE_OB_RATIO = os.getenv("TYPE_B_REQUIRE_OB_RATIO", "true").lower() in ("1", "true", "yes", "on")
_TYPE_B_MIN_OB_RATIO = float(os.getenv("TYPE_B_MIN_OB_RATIO", "1.05"))         # 매수 우위 오더북 확인
_COIN_ALT_CANARY_PROFILE = os.getenv("COIN_ALT_CANARY_PROFILE", "alt_pullback_setup_allow_small_dip")
_COIN_ALT_CANARY_SIGNAL_FAMILY = os.getenv("COIN_ALT_CANARY_SIGNAL_FAMILY", "type_b_alt_pullback")
_COIN_ALT_CANARY_DAILY_CAP = int(os.getenv("COIN_ALT_CANARY_DAILY_CAP", "1"))
_COIN_ALT_CANARY_SIZE_CASH = Decimal(os.getenv("COIN_ALT_CANARY_SIZE_CASH", "10000"))

_AUDIT_TTL = 7 * 86400   # 7일
_STATS_TTL = 30 * 86400
_MARK_HIST_SCAN_BATCH = int(os.getenv("MARK_HIST_SCAN_BATCH", "120"))
_MARK_HIST_SCAN_MAX = int(os.getenv("MARK_HIST_SCAN_MAX", "600"))


# ---------------------------------------------------------------------------
# 동적 stop/take pct 계산
# ---------------------------------------------------------------------------

def _dynamic_pcts(range_5m: float, market: str = "KR") -> tuple:
    """모멘텀 브레이크아웃: 시장별 stop/take 분기."""
    if market == "COIN":
        stop = Decimal(os.getenv("COIN_EXIT_STOP_LOSS_PCT", "0.030"))   # -3.0% (COIN 전용)
        take = Decimal(os.getenv("COIN_EXIT_TAKE_PROFIT_PCT", "0.150")) # +15.0% (Big Mover Ride)
    else:
        stop = Decimal(os.getenv("EXIT_STOP_LOSS_PCT", "0.015"))        # -1.5%
        take = Decimal(os.getenv("EXIT_TAKE_PROFIT_PCT", "0.030"))      # +3.0%
    return stop, take


def _surge_threshold_for_market(market: str) -> float:
    if market == "KR":
        return _MIN_SURGE_5M_KR
    if market == "COIN":
        return _MIN_SURGE_5M_COIN
    return _MIN_SURGE_5M_US


def _ret_1m_threshold_for_market(market: str) -> float:
    if market == "COIN":
        return _MIN_RET_1M_COIN
    return _MIN_RET_1M


def _coin_ret1m_accel_ratio(ret_5m: float | None, ret_1m: float | None) -> float | None:
    if ret_5m is None or ret_1m is None or ret_5m <= 0:
        return None
    return ret_1m / ret_5m


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


def _normalize_price(market: str, price: Decimal) -> Decimal:
    """market에 따라 가격 정규화: KR=호가단위, COIN=원본 유지, US=소수점 2자리."""
    if market == "KR":
        return normalize_kr_price_tick(price)
    if market == "COIN":
        return price  # 업비트 코인 가격은 원본 그대로 사용
    return price.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 로깅 헬퍼
# ---------------------------------------------------------------------------

def _get_client(market: str):
    """KisClient/IbkrClient/UpbitClient 싱글톤 캐시."""
    if market not in _client_cache:
        try:
            if market == "KR":
                from exchange.kis.client import KisClient
                _client_cache[market] = KisClient()
            elif market == "COIN":
                from exchange.upbit.client import UpbitClient
                _client_cache[market] = UpbitClient()
            else:
                from exchange.ibkr.client import IbkrClient
                _client_cache[market] = IbkrClient()
        except Exception as e:
            _log("client_init_failed", market=market, error=str(e))
            return None
    return _client_cache.get(market)


def _calc_size_cash(market: str, current_price: Decimal) -> Decimal:
    """H1: 잔고 비율 기반 size_cash 계산. 실패 시 1주(current_price) fallback."""
    if market == "KR":
        pct = _SIZE_CASH_PCT_KR
    elif market == "COIN":
        pct = _SIZE_CASH_PCT_COIN
    else:
        pct = _SIZE_CASH_PCT_US
    try:
        client = _get_client(market)
        if client is None:
            return current_price  # fallback: 1주
        snapshot = client.get_account_snapshot()
        available = snapshot.available_cash
        if available <= 0:
            return current_price  # fallback: 1주
        size_cash = Decimal(str(float(available) * pct))
        # qty = size_cash / price 가 1 미만이면 1주 fallback
        # COIN(Upbit)은 소수점 매수 지원 → 1주 fallback 불필요
        if market != "COIN" and size_cash < current_price:
            return current_price
        return size_cash
    except Exception as e:
        _log("size_cash_error", market=market, error=str(e), exc_type=type(e).__name__)
        if market == "COIN":
            return Decimal("5000")  # Upbit 최소 주문금액
        min_size = Decimal("100000") if market == "KR" else Decimal("100")
        return max(current_price, min_size)


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


def _parse_mark_hist_entry(entry) -> Optional[tuple[int, float]]:
    try:
        raw = _decode(entry)
        ts_str, price_str = raw.split(":", 1)
        return int(ts_str), float(price_str)
    except Exception:
        return None


def _load_mark_hist_until(
    r,
    market: str,
    symbol: str,
    *,
    target_ms: int,
    batch_size: int = _MARK_HIST_SCAN_BATCH,
    max_entries: int = _MARK_HIST_SCAN_MAX,
) -> list[tuple[int, float]]:
    """target 시점이 보일 때까지 mark_hist를 chunk 단위로 로드."""
    parsed: list[tuple[int, float]] = []
    start = 0

    while start < max_entries:
        end = min(start + batch_size - 1, max_entries - 1)
        chunk = r.lrange(f"mark_hist:{market}:{symbol}", start, end)
        if not chunk:
            break

        for entry in chunk:
            item = _parse_mark_hist_entry(entry)
            if item is not None:
                parsed.append(item)

        oldest = _parse_mark_hist_entry(chunk[-1])
        if oldest is not None and oldest[0] <= target_ms:
            break
        if len(chunk) < batch_size:
            break
        start += batch_size

    return parsed


# ---------------------------------------------------------------------------
# 감사 / 통계
# ---------------------------------------------------------------------------

def _save_audit(
    r,
    market: str,
    signal: Signal,
    ret_5m: Optional[float],
    range_5m: Optional[float],
    *,
    source: str = "consensus_signal_runner",
    stats_field: str = "candidate",
    increment_daily_count: bool = True,
) -> None:
    today = today_kst()
    payload = {
        "signal_id": signal.signal_id,
        "market": market,
        "symbol": signal.symbol,
        "ts": signal.ts,
        "direction": signal.direction,
        "entry_price": str(signal.entry.price),
        "stop_price": str(signal.stop.price),
        "source": source,
    }
    if ret_5m is not None:
        payload["ret_5m"] = str(ret_5m)
    if range_5m is not None:
        payload["range_5m"] = str(range_5m)
    audit_key = f"consensus:audit:{market}:{signal.signal_id}"
    r.set(audit_key, json.dumps(payload), ex=_AUDIT_TTL)

    # 일별 통계 카운터
    stats_key = f"consensus:stats:{market}:{today}"
    r.hincrby(stats_key, stats_field, 1)
    r.expire(stats_key, _STATS_TTL)

    # 일별 candidate 생성 수
    if increment_daily_count:
        daily_key = f"consensus:daily_count:{market}:{today}"
        r.incr(daily_key)
        r.expire(daily_key, _STATS_TTL)


def _record_reject(r, market: str, reason_code: str) -> None:
    today = today_kst()
    stats_key = f"consensus:stats:{market}:{today}"
    r.hincrby(stats_key, reason_code, 1)
    r.expire(stats_key, _STATS_TTL)


def _record_type_b_stat(r, field: str, increment: int = 1) -> None:
    today = today_kst()
    stats_key = f"consensus:type_b:stats:COIN:{today}"
    r.hincrby(stats_key, field, increment)
    r.expire(stats_key, _STATS_TTL)


_TYPE_B_REJECT_SAMPLE_LIMIT = int(os.getenv("TYPE_B_REJECT_SAMPLE_LIMIT", "50"))
_TYPE_B_SCAN_SAMPLE_LIMIT = int(os.getenv("TYPE_B_SCAN_SAMPLE_LIMIT", "500"))
_TYPE_B_ALT_SHADOW_COOLDOWN_SEC = int(os.getenv("TYPE_B_ALT_SHADOW_COOLDOWN_SEC", "14400"))


def _record_type_b_reject_sample(
    r,
    reason_code: str,
    *,
    symbol: str,
    **metrics,
) -> None:
    today = today_kst()
    key = f"consensus:type_b:reject_samples:COIN:{today}:{reason_code}"
    payload = {
        "ts": datetime.now(_KST).isoformat(),
        "symbol": symbol,
    }
    for field, value in metrics.items():
        if value is not None:
            payload[field] = value

    r.lpush(key, json.dumps(payload, ensure_ascii=False))
    r.ltrim(key, 0, max(_TYPE_B_REJECT_SAMPLE_LIMIT - 1, 0))
    r.expire(key, _STATS_TTL)


def _record_type_b_scan_sample(
    r,
    *,
    symbol: str,
    status: str,
    reason_code: str | None = None,
    signal_mode: str | None = None,
    change_rate: float | None = None,
    near_high: float | None = None,
    ret_5m: float | None = None,
    trade_price: float | None = None,
    high_price: float | None = None,
    vol_24h: float | None = None,
    ob_ratio: float | None = None,
) -> None:
    today = today_kst()
    key = f"consensus:type_b:scan_samples:COIN:{today}"
    payload = {
        "ts": datetime.now(_KST).isoformat(),
        "symbol": symbol,
        "status": status,
    }
    if reason_code:
        payload["reason_code"] = reason_code
    if signal_mode:
        payload["signal_mode"] = signal_mode

    metrics = {
        "change_rate": change_rate,
        "near_high": near_high,
        "ret_5m": ret_5m,
        "trade_price": trade_price,
        "high_price": high_price,
        "vol_24h": vol_24h,
        "ob_ratio": ob_ratio,
    }
    for field, value in metrics.items():
        if value is not None:
            payload[field] = value

    r.lpush(key, json.dumps(payload, ensure_ascii=False))
    r.ltrim(key, 0, max(_TYPE_B_SCAN_SAMPLE_LIMIT - 1, 0))
    r.expire(key, _STATS_TTL)


def _record_type_b_reject(r, reason_code: str) -> None:
    _record_type_b_stat(r, reason_code)


def _type_b_alt_shadow_origin(profile_name: str) -> str:
    return f"consensus_runner_type_b_alt_shadow:{profile_name}"


def _coin_alt_canary_mode(r, profile_name: str) -> str:
    if profile_name != _COIN_ALT_CANARY_PROFILE:
        return "off"
    return get_signal_family_mode(
        r,
        "COIN",
        _COIN_ALT_CANARY_SIGNAL_FAMILY,
        strategy="trend_riding",
        source="consensus_signal_runner_type_b_alt_canary",
        default="off",
    )


def _coin_alt_canary_daily_key(profile_name: str, today: str | None = None) -> str:
    return f"consensus:coin_alt_canary_daily:COIN:{profile_name}:{today or today_kst()}"


def _coin_alt_canary_has_capacity(r, profile_name: str) -> bool:
    if _COIN_ALT_CANARY_DAILY_CAP <= 0:
        return False
    try:
        return int(r.get(_coin_alt_canary_daily_key(profile_name)) or 0) < _COIN_ALT_CANARY_DAILY_CAP
    except Exception:
        return False


def _coin_alt_canary_mark_used(r, profile_name: str) -> None:
    key = _coin_alt_canary_daily_key(profile_name)
    r.incr(key)
    r.expire(key, 86400)


def _type_b_alt_shadow_sample(
    *,
    change_rate: float | None,
    near_high: float | None,
    ret_5m: float | None,
    trade_price: float | None,
    high_price: float | None,
    vol_24h: float | None,
    ob_ratio: float | None,
) -> dict[str, object]:
    return {
        "change_rate": change_rate,
        "near_high": near_high,
        "ret_5m": ret_5m,
        "trade_price": trade_price,
        "high_price": high_price,
        "vol_24h": vol_24h,
        "ob_ratio": ob_ratio,
    }


def _save_type_b_alt_shadow_candidate(
    r,
    *,
    symbol: str,
    profile_name: str,
    current_price: Decimal,
    change_rate: float,
    near_high: float,
    ret_5m: float,
    trade_price: float,
    high_price: float,
    vol_24h: float,
    ob_ratio: float | None,
    base_reject_reason: str,
) -> str | None:
    cooldown_key = f"consensus:type_b_alt_shadow_cooldown:COIN:{profile_name}:{symbol}"
    if not r.set(cooldown_key, "1", nx=True, ex=_TYPE_B_ALT_SHADOW_COOLDOWN_SEC):
        return None

    canary_mode = _coin_alt_canary_mode(r, profile_name)
    canary_live = canary_mode == "live" and _coin_alt_canary_has_capacity(r, profile_name)
    stop_pct = Decimal(os.getenv("COIN_EXIT_STOP_LOSS_PCT", "0.030"))
    take_pct = Decimal(os.getenv("COIN_EXIT_TAKE_PROFIT_PCT", "0.150"))
    stop_price = current_price * (1 - stop_pct)
    signal_id = str(uuid.uuid4())
    ts_now = datetime.now(_KST).isoformat()
    size_cash = _COIN_ALT_CANARY_SIZE_CASH if canary_live else _calc_size_cash("COIN", current_price)

    payload = {
        "signal_id": signal_id,
        "ts": ts_now,
        "market": "COIN",
        "symbol": symbol,
        "direction": "LONG",
        "entry": {"price": str(current_price), "size_cash": str(size_cash)},
        "stop": {"price": str(stop_price)},
        "source": "consensus_signal_runner_type_b_alt_canary" if canary_live else "consensus_signal_runner_type_b_alt_shadow",
        "status": "candidate" if canary_live else "shadow_candidate",
        "strategy": "trend_riding",
        "claude_emit": 1 if canary_live else 0,
        "claude_conf": "0.70" if canary_live else "0",
        "ret_5m": ret_5m,
        "change_rate_daily": change_rate,
        "high_price": high_price,
        "near_high": near_high,
        "vol_24h": vol_24h,
        "ob_ratio": ob_ratio,
        "signal_family": _COIN_ALT_CANARY_SIGNAL_FAMILY if canary_live else "type_b",
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
        "reject_reason": base_reject_reason,
        "shadow_stage": "post_gate_alt_profile",
        "shadow_origin": _type_b_alt_shadow_origin(profile_name),
        "canary_profile": profile_name,
    }

    if canary_live:
        try:
            r.lpush("claw:signal:queue", json.dumps(payload))
        except Exception as e:
            r.delete(cooldown_key)
            _log("type_b.alt_canary.publish_failed", symbol=symbol, profile=profile_name, error=str(e))
            return None
        _coin_alt_canary_mark_used(r, profile_name)
        r.hset(f"claw:signal_pct:COIN:{symbol}", mapping={
            "stop_pct": str(stop_pct),
            "take_pct": str(take_pct),
        })
        r.expire(f"claw:signal_pct:COIN:{symbol}", 86400)
        _log(
            "type_b.alt_canary.signal_created",
            symbol=symbol,
            profile=profile_name,
            signal_id=signal_id,
            size_cash=str(size_cash),
            base_reject=base_reject_reason,
        )

    save_signal_snapshot(r, payload)
    return signal_id


def _maybe_save_type_b_alt_shadow_candidates(
    r,
    *,
    symbol: str,
    current_price: Decimal | None,
    change_rate: float | None,
    near_high: float | None,
    ret_5m: float | None,
    trade_price: float | None,
    high_price: float | None,
    vol_24h: float | None,
    ob_ratio: float | None,
    base_reject_reason: str,
) -> list[str]:
    if current_price is None or current_price <= 0:
        return []
    if any(value is None for value in (change_rate, near_high, ret_5m, trade_price, high_price, vol_24h)):
        return []

    sample = _type_b_alt_shadow_sample(
        change_rate=change_rate,
        near_high=near_high,
        ret_5m=ret_5m,
        trade_price=trade_price,
        high_price=high_price,
        vol_24h=vol_24h,
        ob_ratio=ob_ratio,
    )
    thresholds_by_name = scenario_map()
    saved_signal_ids: list[str] = []
    for profile_name in alt_shadow_profile_names():
        thresholds = thresholds_by_name.get(profile_name)
        if not thresholds:
            continue
        if first_gate_fail_reason(sample, thresholds) != "pass_pre_ai":
            continue
        signal_id = _save_type_b_alt_shadow_candidate(
            r,
            symbol=symbol,
            profile_name=profile_name,
            current_price=current_price,
            change_rate=float(change_rate),
            near_high=float(near_high),
            ret_5m=float(ret_5m),
            trade_price=float(trade_price),
            high_price=float(high_price),
            vol_24h=float(vol_24h),
            ob_ratio=ob_ratio,
            base_reject_reason=base_reject_reason,
        )
        if signal_id:
            saved_signal_ids.append(f"{profile_name}:{signal_id}")

    if saved_signal_ids:
        _log(
            "type_b.alt_shadow.saved",
            symbol=symbol,
            base_reject=base_reject_reason,
            profiles=",".join(saved_signal_ids),
        )
    return saved_signal_ids


def _save_coin_pre_consensus_shadow_snapshot(
    r,
    *,
    symbol: str,
    claude: dict[str, str],
    current_price: Optional[Decimal] = None,
    ret_5m: Optional[float] = None,
    range_5m: Optional[float] = None,
    ret_1m: Optional[float] = None,
    vol_24h: Optional[float],
    reject_reason: str,
    shadow_origin: str = "consensus_runner_reject",
) -> None:
    try:
        c_features = json.loads(claude.get("features_json") or "{}")
    except (json.JSONDecodeError, Exception):
        c_features = {}

    live = _get_live_ret_5m(r, "COIN", symbol)
    live_ret_5m = live[0] if live is not None else None
    live_price = Decimal(str(live[1])) if live is not None else None

    if current_price is None:
        current_price = live_price
    if current_price is None:
        try:
            current_price_raw = c_features.get("current_price")
            if current_price_raw is not None:
                current_price = Decimal(str(current_price_raw))
        except (InvalidOperation, TypeError, ValueError):
            current_price = None

    if current_price is None or current_price <= 0:
        return

    if ret_5m is None:
        ret_5m = live_ret_5m
    if ret_5m is None:
        try:
            raw_ret_5m = c_features.get("ret_5m")
            if raw_ret_5m is not None:
                ret_5m = float(raw_ret_5m)
        except (TypeError, ValueError):
            ret_5m = None

    if range_5m is None:
        try:
            raw_range_5m = c_features.get("range_5m")
            if raw_range_5m is not None:
                range_5m = float(raw_range_5m)
        except (TypeError, ValueError):
            range_5m = None

    if range_5m is None or range_5m <= 0:
        return

    if ret_1m is None:
        try:
            raw_ret_1m = c_features.get("ret_1m")
            if raw_ret_1m is not None:
                ret_1m = float(raw_ret_1m)
        except (TypeError, ValueError):
            ret_1m = None

    eval_ts_ms = str(claude.get("ts_ms") or int(time.time() * 1000))
    ts_ms = int(time.time() * 1000)
    ts = datetime.fromtimestamp(ts_ms / 1000, _KST).isoformat()
    stop_pct, take_pct = _dynamic_pcts(range_5m, "COIN")
    stop_price = _normalize_price("COIN", current_price * (1 - stop_pct))
    if stop_price <= 0:
        return

    try:
        claude_conf = float(claude.get("confidence") or "0.7")
    except (TypeError, ValueError):
        claude_conf = 0.7

    ob_ratio = None
    ob_raw = r.hget(f"orderbook:COIN:{symbol}", "ob_ratio")
    if ob_raw is not None:
        try:
            ob_ratio = float(ob_raw.decode() if isinstance(ob_raw, bytes) else ob_raw)
        except (TypeError, ValueError):
            ob_ratio = None

    payload = {
        "signal_id": f"pre:{symbol}:{eval_ts_ms}",
        "ts": ts,
        "market": "COIN",
        "symbol": symbol,
        "direction": claude.get("direction") or "LONG",
        "entry": {
            "price": str(current_price),
            "size_cash": str(max(_COIN_PRE_SHADOW_SIZE_CASH, Decimal("5000"))),
        },
        "stop": {"price": str(stop_price)},
        "source": "consensus_signal_runner_pre_consensus",
        "status": "shadow_candidate",
        "strategy": "momentum_breakout",
        "signal_family": "type_a",
        "claude_emit": 1,
        "claude_conf": str(claude_conf),
        "ret_5m": ret_5m,
        "range_5m": range_5m,
        "ret_1m": ret_1m,
        "vol_24h": vol_24h,
        "ob_ratio": ob_ratio,
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
        "claude_reason": claude.get("reason", ""),
        "reject_reason": reject_reason,
        "shadow_stage": "pre_consensus",
        "shadow_origin": shadow_origin,
    }
    save_pre_consensus_signal_snapshot(r, payload)


def _classify_claude_veto(reason: str) -> tuple[str, str]:
    """Claude veto reason을 운영용 reject bucket으로 정규화."""
    text = (reason or "").strip().lower()
    if not text:
        return "reject_claude_veto", "claude_veto"

    patterns = [
        (("market close", "market closed", "after close", "closing bell", "장 마감", "장종료"), "reject_market_close", "market_close"),
        (("late entry", "too late", "late breakout", "late move", "chasing", "chase", "늦은 진입", "추격 매수"), "reject_late_entry", "late_entry"),
        (("momentum decay", "momentum faded", "momentum weak", "weakened momentum", "pullback", "모멘텀 둔화", "탄력 둔화"), "reject_momentum_decay", "momentum_decay"),
    ]
    for needles, code, label in patterns:
        if any(needle in text for needle in needles):
            return code, label
    return "reject_claude_veto", "claude_veto"


def _get_dates_for_news(today: str) -> list:
    """오늘과 어제 날짜 반환 (뉴스 조회용)."""
    try:
        dt = datetime.strptime(today, "%Y%m%d")
        yesterday = (dt - timedelta(days=1)).strftime("%Y%m%d")
        return [today, yesterday]
    except ValueError:
        return [today]


def _is_news_score_eligible(d: dict) -> bool:
    """Deterministic 뉴스 boost에는 외부 뉴스만 사용.

    `ife_home`은 가격 이벤트 요약 성격이 강해서, surge 완화/size boost에 쓰면
    가격 움직임을 다시 뉴스로 해석하는 자기강화가 된다.
    """
    return str(d.get("source", "")).lower() != "ife_home"


def _has_positive_news(r, market: str, symbol: str) -> bool:
    """오늘/어제 뉴스 중 positive+high/medium 뉴스가 있으면 True."""
    today = today_kst()
    for date_str in _get_dates_for_news(today):
        news_key = f"news:symbol:{market}:{symbol}:{date_str}"
        items = r.lrange(news_key, 0, 9)
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if not _is_news_score_eligible(d):
                    continue
                sentiment = d.get("sentiment", "").lower()
                impact = d.get("impact", "").lower()
                if sentiment == "positive" and impact in ("high", "medium"):
                    return True
            except Exception:
                continue
    return False


def _get_news_score(r, market: str, symbol: str) -> str:
    """오늘/어제 뉴스 최고 임팩트 반환: 'high', 'medium', 'none'."""
    today = today_kst()
    for date_str in _get_dates_for_news(today):
        news_key = f"news:symbol:{market}:{symbol}:{date_str}"
        items = r.lrange(news_key, 0, 9)
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if not _is_news_score_eligible(d):
                    continue
                sentiment = d.get("sentiment", "").lower()
                impact = d.get("impact", "").lower()
                if sentiment == "positive" and impact == "high":
                    return "high"
            except Exception:
                continue
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if not _is_news_score_eligible(d):
                    continue
                sentiment = d.get("sentiment", "").lower()
                impact = d.get("impact", "").lower()
                if sentiment == "positive" and impact == "medium":
                    return "medium"
            except Exception:
                continue
    return "none"


def _get_volume_surge_status(r, market: str, symbol: str) -> tuple[bool, dict]:
    """오늘 거래량이 최근 평균 대비 충분한지 평가.

    KR은 장중 데이터 부족 때문에 과거 거래량 히스토리 0건이면 permissive pass.
    """
    ratio_threshold = _VOLUME_SURGE_RATIO_KR if market == "KR" else _VOLUME_SURGE_RATIO
    min_samples = _VOLUME_SURGE_MIN_SAMPLES_KR if market == "KR" else _VOLUME_SURGE_MIN_SAMPLES
    today = today_kst()
    today_raw = r.get(f"vol:{market}:{symbol}:{today}")
    if not today_raw:
        return True, {
            "today_vol": None,
            "avg_vol": None,
            "ratio": None,
            "threshold": ratio_threshold,
            "history_samples": 0,
            "reason": "today_volume_missing",
        }
    try:
        today_vol = int(today_raw.decode() if isinstance(today_raw, bytes) else today_raw)
    except (ValueError, TypeError):
        return True, {
            "today_vol": None,
            "avg_vol": None,
            "ratio": None,
            "threshold": ratio_threshold,
            "history_samples": 0,
            "reason": "today_volume_invalid",
        }

    vols = []
    for i in range(1, _VOLUME_LOOKBACK_DAYS + 1):
        try:
            dt = datetime.strptime(today, "%Y%m%d")
            past_date = (dt - timedelta(days=i)).strftime("%Y%m%d")
            raw = r.get(f"vol:{market}:{symbol}:{past_date}")
            if raw:
                v = int(raw.decode() if isinstance(raw, bytes) else raw)
                if v > 0:
                    vols.append(v)
        except Exception:
            continue

    if not vols:
        allow = market == "KR"
        return allow, {
            "today_vol": today_vol,
            "avg_vol": None,
            "ratio": None,
            "threshold": ratio_threshold,
            "history_samples": 0,
            "reason": "history_missing_allow" if allow else "history_missing_reject",
        }

    if len(vols) < min_samples:
        return False, {
            "today_vol": today_vol,
            "avg_vol": None,
            "ratio": None,
            "threshold": ratio_threshold,
            "history_samples": len(vols),
            "reason": "insufficient_history",
        }

    avg_vol = sum(vols) / len(vols)
    ratio = today_vol / avg_vol if avg_vol > 0 else None
    passed = bool(avg_vol > 0 and ratio is not None and today_vol >= avg_vol * ratio_threshold)
    return passed, {
        "today_vol": today_vol,
        "avg_vol": round(avg_vol, 2),
        "ratio": round(ratio, 4) if ratio is not None else None,
        "threshold": ratio_threshold,
        "history_samples": len(vols),
        "reason": "ratio_ok" if passed else "ratio_below_threshold",
    }


def _get_regime(r, market: str, watchlist: list) -> str:
    """워치리스트 mark_hist 기반 실시간 ret_5m으로 regime 판별.

    AI eval features_json 대신 mark_hist를 사용해 stale 데이터 문제 방지.
    Returns: "bearish" | "bullish" | "neutral"
    """
    if not watchlist:
        return "neutral"

    now_ms = int(time.time() * 1000)
    target_ms = now_ms - 5 * 60 * 1000  # 5분 전
    stale_threshold_ms = 5 * 60 * 1000  # 최신 시세가 5분 이상 오래됐으면 skip

    bearish = 0
    total = 0
    for symbol in watchlist:
        try:
            entries = _load_mark_hist_until(r, market, symbol, target_ms=target_ms)
            if not entries:
                continue
            latest_ts, latest_price = entries[0]

            if now_ms - latest_ts > stale_threshold_ms:
                continue  # 시세 오래됨 — 무시

            past_price = None
            for entry in entries[1:]:
                t, p = entry
                if t <= target_ms:
                    past_price = p
                    break

            if past_price is None or past_price == 0:
                continue

            ret_5m = (latest_price - past_price) / past_price
            total += 1
            if ret_5m < 0:
                bearish += 1
        except Exception:
            continue

    if total < 3:
        return "neutral"
    ratio = bearish / total
    # 마켓별 bearish threshold: KR은 장 초반 눌림 오판 방지를 위해 높게 설정
    if market == "KR":
        _bearish_thr = float(os.getenv("KR_REGIME_BEARISH_THRESHOLD", "0.60"))
    else:
        _bearish_thr = float(os.getenv("COIN_REGIME_BEARISH_THRESHOLD", "0.45"))
    if ratio > _bearish_thr:
        return "bearish"
    if ratio < _BULLISH_THRESHOLD:
        return "bullish"
    return "neutral"


def _is_bearish_regime(r, market: str, watchlist: list) -> bool:
    """backward compat wrapper."""
    return _get_regime(r, market, watchlist) == "bearish"


def _get_live_ret_5m(r, market: str, symbol: str) -> Optional[tuple]:
    """mark_hist에서 실시간 ret_5m, latest_price 계산.

    Returns: (ret_5m: float, latest_price: float) or None if insufficient data.
    최신 시세가 2분 이상 오래됐으면 None 반환 (stale 거부).
    """
    now_ms = int(time.time() * 1000)
    target_ms = now_ms - 5 * 60 * 1000  # 5분 전
    stale_threshold_ms = 2 * 60 * 1000  # 최신 시세 2분 초과 시 skip

    entries = _load_mark_hist_until(r, market, symbol, target_ms=target_ms)
    if not entries:
        return None
    latest_ts, latest_price = entries[0]

    if now_ms - latest_ts > stale_threshold_ms:
        return None  # 시세 2분 이상 오래됨

    past_price = None
    for entry in entries[1:]:
        t, p = entry
        if t <= target_ms:
            past_price = p
            break

    if past_price is None or past_price == 0:
        return None

    ret_5m = (latest_price - past_price) / past_price
    return ret_5m, latest_price


# ---------------------------------------------------------------------------
# 핵심 처리: 심볼 1개
# ---------------------------------------------------------------------------

def run_once(market: str, symbol: str, r) -> Optional[dict]:
    """
    dual eval Redis 결과를 읽어 candidate Signal을 생성하고 queue에 push.

    Returns:
        signal dict (push 성공 시) or None (reject / 오류 시)
    """
    # 1. eval 결과 읽기 (Claude only)
    claude = _hgetall_str(r, f"ai:dual:last:claude:{market}:{symbol}")
    vol_24h: Optional[float] = None

    if not claude:
        return None  # Claude 결과 없음 — cold start

    # 1-b. stale eval 무시:
    # - HOLD는 30분 넘으면 재평가 대기
    # - emit=1도 5분 넘으면 무시 (과거 강세 신호 재처리 방지)
    _hold_eval_stale_sec = int(os.getenv("EVAL_STALE_SEC", "1800"))
    _emit_eval_stale_sec = int(os.getenv("EMIT_STALE_SEC", "300"))
    c_ts_ms_raw = claude.get("ts_ms", "0")
    if c_ts_ms_raw:
        eval_age_ms = int(time.time() * 1000) - int(c_ts_ms_raw)
        eval_is_emit = claude.get("emit") == "1"
        max_eval_age_sec = _emit_eval_stale_sec if eval_is_emit else _hold_eval_stale_sec
        if eval_age_ms > max_eval_age_sec * 1000:
            try:
                # 오래된 eval 캐시는 즉시 폐기해 반복 stale skip/log를 막는다.
                r.delete(f"ai:dual:last:claude:{market}:{symbol}")
            except Exception:
                pass
            _log(
                "runner.skip.stale_eval",
                symbol=symbol,
                emit=claude.get("emit", ""),
                age_sec=int(eval_age_ms / 1000),
                max_age_sec=max_eval_age_sec,
            )
            return None

    # 2. dedup: 이미 이 ts_ms로 처리한 결과면 스킵 (중복 push 방지)
    c_ts_ms = claude.get("ts_ms", "")
    seen_key = f"consensus:seen:{market}:{symbol}:{c_ts_ms}"
    if not r.set(seen_key, "1", nx=True, ex=600):
        return None  # 이미 처리한 eval 결과

    # 2-b. symbol-level cooldown: consensus 판단 전 체크 (불필요한 처리 방지)
    cooldown_key = f"consensus:symbol_cooldown:{market}:{symbol}"
    if r.exists(cooldown_key):
        _log("runner.reject.cooldown", symbol=symbol, cooldown_sec=_SYMBOL_COOLDOWN_SEC)
        _record_reject(r, market, "reject_cooldown")
        return None

    # 2-c. 당일 stop_loss 이후 재진입 차단 (완화: 2시간 경과 + 손절가 대비 +3% 회복 시 허용)
    today = today_kst()
    daily_stop_key = f"claw:daily_stop:{market}:{symbol}:{today}"
    if r.exists(daily_stop_key):
        allow_reentry = False
        try:
            if r.type(daily_stop_key).decode() == "hash":
                ds = _hgetall_str(r, daily_stop_key)
                stop_ts = float(ds.get("stop_ts", 0))
                elapsed = time.time() - stop_ts
                if elapsed >= 7200 and ds.get("stop_price"):
                    ds_stop_price = float(ds["stop_price"])
                    live_check = _get_live_ret_5m(r, market, symbol)
                    if live_check is not None and ds_stop_price > 0:
                        recovery = (live_check[1] - ds_stop_price) / ds_stop_price
                        if recovery >= 0.03:
                            allow_reentry = True
                            _log("runner.daily_stop.reentry_allowed", symbol=symbol,
                                 elapsed_h=f"{elapsed/3600:.1f}h",
                                 recovery_pct=f"{recovery*100:.1f}%")
        except Exception:
            pass
        if not allow_reentry:
            _log("runner.reject.daily_stop", symbol=symbol, market=market)
            _record_reject(r, market, "reject_daily_stop")
            return None

    # 2-d. 종목별 일일 진입 상한 (max 2회, Type A/B 공유 카운터)
    symbol_daily_key = f"consensus:symbol_daily:{market}:{symbol}:{today}"
    _symbol_daily_cap = int(os.getenv("CONSENSUS_SYMBOL_DAILY_CAP", "2"))
    symbol_count = int(r.get(symbol_daily_key) or 0)
    if symbol_count >= _symbol_daily_cap:
        _log("runner.reject.symbol_daily_cap", symbol=symbol, market=market, count=symbol_count)
        _record_reject(r, market, "reject_symbol_daily_cap")
        return None

    # 2-e. 당일 stop_loss 2회 이상 → 완전 차단 (반복 손절 종목 자본 낭비 방지)
    stop_count_key = f"claw:stop_count:{market}:{symbol}:{today}"
    stop_count = int(r.get(stop_count_key) or 0)
    if stop_count >= 2:
        _log("runner.reject.stop_count", symbol=symbol, market=market, count=stop_count)
        _record_reject(r, market, "reject_stop_count")
        return None

    # 3. 데이터 무결성: features_json 파싱
    try:
        c_features = json.loads(claude.get("features_json") or "{}")
    except (json.JSONDecodeError, Exception) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"features_json parse error: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 4. Claude emit 확인 (시장 종료 / 늦은 진입 / 모멘텀 둔화 등 veto 포함)
    c_emit = claude.get("emit") == "1"
    if not c_emit:
        veto_code, veto_label = _classify_claude_veto(claude.get("reason", ""))
        if (
            market == "COIN"
            and veto_code in ("reject_late_entry", "reject_momentum_decay", "reject_claude_veto")
        ):
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                vol_24h=vol_24h if market == "COIN" else None,
                reject_reason=veto_code,
                shadow_origin="consensus_runner_veto_gate",
            )
        _log("runner.reject.claude_veto", symbol=symbol, veto=veto_label, reason=claude.get("reason", "")[:60])
        _record_reject(r, market, veto_code)
        return None

    # 방향 확인
    c_dir = claude.get("direction", "")
    if not c_dir:
        _log("runner.reject.direction_missing", symbol=symbol)
        _record_reject(r, market, "reject_direction_missing")
        return None

    # Phase 10: LONG 방향만 처리
    if c_dir != "LONG":
        _log("runner.reject.direction_not_long", symbol=symbol, direction=c_dir)
        _record_reject(r, market, "reject_direction_not_long")
        return None

    # 5. prefilter: live ret_5m (mark_hist 직접 계산 — stale eval 방지)
    live = _get_live_ret_5m(r, market, symbol)
    if live is None:
        # mark_hist 데이터 없거나 stale → 다음 폴링 대기 (stale 가격으로 진입 방지)
        _log("runner.reject.no_live_price", symbol=symbol)
        _record_reject(r, market, "reject_no_live_price")
        return None
    ret_5m, live_price_float = live
    live_price = Decimal(str(live_price_float))

    try:
        range_5m_raw = c_features.get("range_5m")
        if range_5m_raw is None:
            raise ValueError("range_5m missing in features_json")
        range_5m = float(range_5m_raw)
    except (TypeError, ValueError) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=str(e))
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # momentum breakout: 지금 이 순간에도 5분 상승폭이 충분해야 함
    surge_threshold = _surge_threshold_for_market(market)
    # 뉴스 boost: KR positive+high 뉴스 있으면 surge 하한선 30% 완화
    if market == "KR" and _get_news_score(r, market, symbol) == "high":
        surge_threshold = surge_threshold * 0.7
        _log("runner.news_surge_relaxed", symbol=symbol, threshold=f"{surge_threshold:.3f}")
    if ret_5m <= surge_threshold:
        if market == "COIN":
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                current_price=live_price,
                ret_5m=ret_5m,
                range_5m=range_5m,
                ret_1m=None,
                vol_24h=vol_24h if market == "COIN" else None,
                reject_reason="reject_prefilter_ret_5m",
            )
        _log("runner.reject.prefilter_ret_5m", symbol=symbol, ret_5m=ret_5m)
        _record_reject(r, market, "reject_prefilter_ret_5m")
        return None

    # 꼭지 매수 차단: 이미 너무 많이 오른 경우 후발 진입 방지 (Type A Flash Surge 전용)
    if ret_5m >= _MAX_SURGE_5M:
        if market == "COIN":
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                current_price=live_price,
                ret_5m=ret_5m,
                range_5m=range_5m,
                ret_1m=None,
                vol_24h=vol_24h if market == "COIN" else None,
                reject_reason="reject_prefilter_ret_5m_overextended",
            )
        _log("runner.reject.prefilter_ret_5m_overextended", symbol=symbol, ret_5m=ret_5m)
        _record_reject(r, market, "reject_prefilter_ret_5m_overextended")
        return None

    if range_5m <= _MIN_RANGE_5M:
        if market == "COIN":
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                current_price=live_price,
                ret_5m=ret_5m,
                range_5m=range_5m,
                ret_1m=None,
                vol_24h=vol_24h if market == "COIN" else None,
                reject_reason="reject_prefilter_range_5m",
            )
        _log("runner.reject.prefilter_range_5m", symbol=symbol, range_5m=range_5m)
        _record_reject(r, market, "reject_prefilter_range_5m")
        return None

    ret_1m = None
    # 5-a2. ret_1m 최소 기준 프리필터
    try:
        ret_1m_raw = c_features.get("ret_1m")
        if ret_1m_raw is not None:
            ret_1m = float(ret_1m_raw)
            if ret_1m < _ret_1m_threshold_for_market(market):
                if market == "COIN":
                    _save_coin_pre_consensus_shadow_snapshot(
                        r,
                        symbol=symbol,
                        claude=claude,
                        current_price=live_price,
                        ret_5m=ret_5m,
                        range_5m=range_5m,
                        ret_1m=ret_1m,
                        vol_24h=vol_24h if market == "COIN" else None,
                        reject_reason="reject_prefilter_ret_1m",
                    )
                _log("runner.reject.prefilter_ret_1m", symbol=symbol, ret_1m=ret_1m)
                _record_reject(r, market, "reject_prefilter_ret_1m")
                return None
    except (TypeError, ValueError):
        pass  # ret_1m 없거나 파싱 실패 → 통과 (permissive)

    if market == "COIN":
        if ret_1m is None:
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                current_price=live_price,
                ret_5m=ret_5m,
                range_5m=range_5m,
                ret_1m=ret_1m,
                vol_24h=vol_24h,
                reject_reason="reject_prefilter_ret_1m_missing",
            )
            _log("runner.reject.prefilter_ret_1m_missing", symbol=symbol)
            _record_reject(r, market, "reject_prefilter_ret_1m_missing")
            return None
        accel_ratio = _coin_ret1m_accel_ratio(ret_5m, ret_1m)
        if accel_ratio is not None and accel_ratio < _COIN_MIN_RET1M_TO_RET5M_RATIO:
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                current_price=live_price,
                ret_5m=ret_5m,
                range_5m=range_5m,
                ret_1m=ret_1m,
                vol_24h=vol_24h,
                reject_reason="reject_prefilter_ret1m_ratio",
            )
            _log(
                "runner.reject.prefilter_ret1m_ratio",
                symbol=symbol,
                ret_1m=ret_1m,
                ret_5m=ret_5m,
                accel_ratio=f"{accel_ratio:.3f}",
            )
            _record_reject(r, market, "reject_prefilter_ret1m_ratio")
            return None

    # 5-a3. COIN Type A: 유동성 게이트 (24h 거래대금 하한)
    # 실제 Type A 후보로 남은 경우에만 적용해야 HOLD/veto 심볼이 low_vol로 오분류되지 않는다.
    if market == "COIN":
        _type_a_min_vol = float(os.getenv("TYPE_A_MIN_VOL_KRW", "30000000000"))  # 기본 300억
        vol_raw = r.get(f"vol:COIN:{symbol}:{today}")
        vol_24h = float(vol_raw.decode() if isinstance(vol_raw, bytes) else vol_raw) if vol_raw else 0.0
        if vol_24h < _type_a_min_vol:
            _save_coin_pre_consensus_shadow_snapshot(
                r,
                symbol=symbol,
                claude=claude,
                current_price=live_price,
                ret_5m=ret_5m,
                range_5m=range_5m,
                ret_1m=ret_1m,
                vol_24h=vol_24h,
                reject_reason="reject_low_vol_24h",
                shadow_origin="consensus_runner_liquidity_gate",
            )
            _log("runner.reject.low_vol_24h", symbol=symbol, vol_24h=f"{vol_24h/1e8:.0f}억")
            _record_reject(r, market, "reject_low_vol_24h")
            return None

    # 5-b. Volume surge 필터 (KR + COIN — US는 데이터 없음)
    if market in ("KR", "COIN"):
        volume_ok, volume_diag = _get_volume_surge_status(r, market, symbol)
        if not volume_ok:
            if market == "COIN":
                _save_coin_pre_consensus_shadow_snapshot(
                    r,
                    symbol=symbol,
                    claude=claude,
                    current_price=live_price,
                    ret_5m=ret_5m,
                    range_5m=range_5m,
                    ret_1m=ret_1m,
                    vol_24h=vol_24h if market == "COIN" else None,
                    reject_reason="reject_volume_no_surge",
                )
            _log(
                "runner.reject.volume_no_surge",
                symbol=symbol,
                today_vol=volume_diag["today_vol"],
                avg_vol=volume_diag["avg_vol"],
                ratio=volume_diag["ratio"],
                threshold=volume_diag["threshold"],
                history_samples=volume_diag["history_samples"],
                reason=volume_diag["reason"],
            )
            _record_reject(r, market, "reject_volume_no_surge")
            return None

    # 6. current_price: live_price 우선, fallback features_json
    if live_price is not None:
        current_price = live_price
    else:
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

    # 7. stop/take pct 동적 계산 + stop price 계산 (market-aware 정규화)
    stop_pct, take_pct = _dynamic_pcts(range_5m, market)
    stop_raw = current_price * (1 - stop_pct)
    stop_price = _normalize_price(market, stop_raw)
    if stop_price <= 0:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"stop_price={stop_price} <= 0")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 8. Signal 객체 생성 (Pydantic 검증 포함)
    signal_id = str(uuid.uuid4())
    ts = datetime.now(_KST).isoformat()

    # H1: 잔고 비율 기반 size_cash + confidence 가중
    base_size = _calc_size_cash(market, current_price)

    try:
        c_conf = float(claude.get("confidence") or "0.7")
    except (ValueError, TypeError):
        c_conf = 0.7
    if c_conf >= 0.8:
        conf_mult = Decimal("1.2")
    elif c_conf < 0.6:
        conf_mult = Decimal("0.8")
    else:
        conf_mult = Decimal("1.0")

    # COIN은 소수점 매수 → current_price(BTC=99.5M 등) floor 불필요
    if market == "COIN":
        size_cash = base_size * conf_mult
    else:
        size_cash = max(base_size * conf_mult, current_price)
    _log("size_cash_weighted", symbol=symbol, conf=f"{c_conf:.2f}",
         mult=str(conf_mult), size_cash=str(size_cash))

    # KR positive+high 뉴스: size_cash 1.5배 boost
    if market == "KR":
        news_score = _get_news_score(r, market, symbol)
        if news_score == "high":
            size_cash = size_cash * Decimal("1.5")
            _log("runner.news_size_boost", symbol=symbol, news_score=news_score, size_cash=str(size_cash))

    try:
        signal = Signal(
            signal_id=signal_id,
            ts=ts,
            market=market,
            symbol=symbol,
            direction="LONG",
            entry=SignalEntry(
                price=current_price,
                size_cash=size_cash,
            ),
            stop=SignalStop(price=stop_price),
            stop_pct=stop_pct,
            take_pct=take_pct,
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
        "strategy": "momentum_breakout",
        "signal_family": "type_a",
        "claude_emit": 1,
        "claude_conf": str(c_conf),
        "ret_5m": ret_5m,
        "range_5m": range_5m,
        "ret_1m": ret_1m,
        "vol_24h": vol_24h if market == "COIN" else None,
        "ob_ratio": None,
        "news_score": _get_news_score(r, market, symbol) if market == "KR" else None,
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
    }
    if market == "COIN":
        ob_raw = r.hget(f"orderbook:COIN:{symbol}", "ob_ratio")
        if ob_raw is not None:
            try:
                payload["ob_ratio"] = float(ob_raw.decode() if isinstance(ob_raw, bytes) else ob_raw)
            except (TypeError, ValueError):
                payload["ob_ratio"] = None

    signal_mode = get_signal_family_mode(r, market, "type_a", strategy="momentum_breakout", source="consensus_signal_runner")
    if market == "COIN" and signal_mode == "off":
        _log("runner.skip.signal_mode_off", symbol=symbol, market=market, signal_family="type_a")
        _record_reject(r, market, "reject_signal_mode_off")
        return None

    # cooldown SET: consensus 성공 + prefilter 통과 후, signal push 직전에만 설정
    r.set(cooldown_key, "1", ex=_SYMBOL_COOLDOWN_SEC)

    if market == "COIN" and signal_mode == "shadow":
        payload["status"] = "shadow_candidate"
        save_signal_snapshot(r, payload)
        _save_audit(
            r,
            market,
            signal,
            ret_5m,
            range_5m,
            source="consensus_signal_runner_shadow",
            stats_field="shadow_candidate",
            increment_daily_count=False,
        )
        _log(
            "runner.shadow_only.candidate_saved",
            signal_id=signal_id,
            symbol=symbol,
            market=market,
            signal_family="type_a",
            mode=signal_mode,
        )
        return payload

    try:
        r.lpush("claw:signal:queue", json.dumps(payload))
    except Exception as e:
        _log("runner.error.publish_failed", symbol=symbol, signal_id=signal_id, error=str(e))
        r.delete(cooldown_key)  # lpush 실패 시 cooldown 롤백
        return None
    if market == "COIN":
        save_signal_snapshot(r, payload)

    # 종목별 일일 진입 카운트 증가
    r.incr(symbol_daily_key)
    r.expire(symbol_daily_key, 86400)

    # stop_pct/take_pct를 exit runner가 읽을 수 있도록 저장 (TTL 24시간)
    r.hset(f"claw:signal_pct:{market}:{symbol}", mapping={
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
    })
    r.expire(f"claw:signal_pct:{market}:{symbol}", 86400)

    # 10. 감사 로그 / 통계
    _save_audit(r, market, signal, ret_5m, range_5m)

    _log(
        "runner.pass.candidate_created",
        signal_id=signal_id,
        symbol=symbol,
        market=market,
        entry_price=str(current_price),
        stop_price=str(stop_price),
        stop_pct=str(stop_pct),
        take_pct=str(take_pct),
        ret_5m=f"{ret_5m:.4f}",
        range_5m=f"{range_5m:.4f}",
    )

    return payload


# ---------------------------------------------------------------------------
# Type B: 추세 탑승 신호 (일간 서서히 오르는 종목 포착)
# ---------------------------------------------------------------------------

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic()
    return _anthropic_client


def _run_type_b_coin(symbol: str, r, today: str) -> Optional[dict]:
    """Type B 추세 탑승: 일간 +5% 이상 상승 중이며 현재도 고점 유지 중인 종목."""
    market = "COIN"
    change_rate: float | None = None
    trade_price: float | None = None
    high_price: float | None = None
    vol_krw: float | None = None
    near_high: float | None = None
    ret_5m: float | None = None
    _ob_ratio: float | None = None
    current_price: Decimal | None = None

    def _reject(reason_code: str, **sample_metrics) -> None:
        _record_type_b_reject(r, reason_code)
        _record_type_b_reject_sample(r, reason_code, symbol=symbol, **sample_metrics)

    def _record_scan(status: str, reason_code: str | None = None) -> None:
        _record_type_b_scan_sample(
            r,
            symbol=symbol,
            status=status,
            reason_code=reason_code,
            signal_mode=signal_mode,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
        )

    signal_mode = get_signal_family_mode(r, market, "type_b", strategy="trend_riding", source="consensus_signal_runner_type_b")
    if signal_mode == "off":
        _record_scan("reject", "reject_signal_mode_off")
        _reject("reject_signal_mode_off")
        return None

    _record_type_b_stat(r, "scanned")

    # Type B 쿨다운 (4시간)
    tb_cooldown_key = f"consensus:type_b_cooldown:{market}:{symbol}"
    if r.exists(tb_cooldown_key):
        _record_scan("reject", "reject_cooldown")
        _reject("reject_cooldown")
        return None

    # daily_stop 체크 (완화 로직 동일 적용)
    daily_stop_key = f"claw:daily_stop:{market}:{symbol}:{today}"
    if r.exists(daily_stop_key):
        allow_reentry = False
        try:
            if r.type(daily_stop_key).decode() == "hash":
                ds = _hgetall_str(r, daily_stop_key)
                stop_ts = float(ds.get("stop_ts", 0))
                elapsed = time.time() - stop_ts
                if elapsed >= 7200 and ds.get("stop_price"):
                    ds_stop_price = float(ds["stop_price"])
                    live_check = _get_live_ret_5m(r, market, symbol)
                    if live_check is not None and ds_stop_price > 0:
                        if (live_check[1] - ds_stop_price) / ds_stop_price >= 0.03:
                            allow_reentry = True
        except Exception:
            pass
        if not allow_reentry:
            _record_scan("reject", "reject_daily_stop")
            _reject("reject_daily_stop")
            return None

    # 당일 stop_loss 2회 이상 → 완전 차단
    stop_count_key = f"claw:stop_count:{market}:{symbol}:{today}"
    if int(r.get(stop_count_key) or 0) >= 2:
        _record_scan("reject", "reject_stop_count")
        _reject("reject_stop_count")
        return None

    # 종목별 일일 진입 상한 (Type A와 공유 카운터)
    symbol_daily_key = f"consensus:symbol_daily:{market}:{symbol}:{today}"
    _symbol_daily_cap = int(os.getenv("CONSENSUS_SYMBOL_DAILY_CAP", "2"))
    if int(r.get(symbol_daily_key) or 0) >= _symbol_daily_cap:
        _log("type_b.reject.symbol_daily_cap", symbol=symbol)
        _record_scan("reject", "reject_symbol_daily_cap")
        _reject("reject_symbol_daily_cap")
        return None

    # Upbit ticker 조회
    client = _get_client(market)
    if client is None:
        _record_scan("reject", "reject_client_unavailable")
        _reject("reject_client_unavailable")
        return None
    try:
        ticker = client.get_ticker(symbol)
    except Exception as e:
        _log("type_b.ticker_error", symbol=symbol, error=str(e))
        _record_scan("reject", "reject_ticker_error")
        _reject("reject_ticker_error")
        return None

    change_rate = float(ticker.get("signed_change_rate", 0))
    trade_price = float(ticker.get("trade_price", 0))
    high_price = float(ticker.get("high_price", 0))
    vol_krw = float(ticker.get("acc_trade_price_24h", 0))

    if trade_price <= 0 or high_price <= 0:
        _record_scan("reject", "reject_invalid_ticker")
        _reject("reject_invalid_ticker")
        return None

    near_high = trade_price / high_price
    live = _get_live_ret_5m(r, market, symbol)
    if live is not None:
        ret_5m, live_price_float = live
    else:
        live_price_float = trade_price

    ob_raw = r.hget(f"orderbook:COIN:{symbol}", "ob_ratio")
    if ob_raw:
        try:
            _ob_ratio = float(ob_raw.decode() if isinstance(ob_raw, bytes) else ob_raw)
        except (TypeError, ValueError):
            _ob_ratio = None

    if live is not None:
        current_price = Decimal(str(live_price_float))

    # 조건 ①: 전일대비 +5% 이상
    if change_rate < _TYPE_B_MIN_CHANGE_RATE:
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_change_rate_weak",
        )
        _record_scan("reject", "reject_change_rate_weak")
        _reject(
            "reject_change_rate_weak",
            change_rate=change_rate,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None
    if change_rate > _TYPE_B_MAX_CHANGE_RATE:
        _log("type_b.reject.change_rate_overextended", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%")
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_change_rate_overextended",
        )
        _record_scan("reject", "reject_change_rate_overextended")
        _reject(
            "reject_change_rate_overextended",
            change_rate=change_rate,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None

    # 조건 ②: 당일 고점 -3% 이내 (추세 유지 확인)
    if near_high < _TYPE_B_NEAR_HIGH_RATIO:
        _log("type_b.reject.far_from_high", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", near_high=f"{near_high:.3f}")
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_far_from_high",
        )
        _record_scan("reject", "reject_far_from_high")
        _reject(
            "reject_far_from_high",
            change_rate=change_rate,
            near_high=near_high,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None

    # 조건 ③: 거래대금 100억 이상
    if vol_krw < _TYPE_B_MIN_VOL_KRW:
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_low_vol_24h",
        )
        _record_scan("reject", "reject_low_vol_24h")
        _reject(
            "reject_low_vol_24h",
            change_rate=change_rate,
            near_high=near_high,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None

    # 조건 ④: 현재도 5분 상승 중
    if live is None:
        _record_scan("reject", "reject_no_live_price")
        _reject(
            "reject_no_live_price",
            change_rate=change_rate,
            near_high=near_high,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None
    if ret_5m < _TYPE_B_MIN_RET_5M:
        _log("type_b.reject.ret_5m_weak", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", ret_5m=f"{ret_5m:.4f}")
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_ret_5m_weak",
        )
        _record_scan("reject", "reject_ret_5m_weak")
        _reject(
            "reject_ret_5m_weak",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None
    if ret_5m > _TYPE_B_MAX_RET_5M:
        _log("type_b.reject.ret_5m_overextended", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", ret_5m=f"{ret_5m:.4f}")
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_ret_5m_overextended",
        )
        _record_scan("reject", "reject_ret_5m_overextended")
        _reject(
            "reject_ret_5m_overextended",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None

    current_price = Decimal(str(live_price_float))

    # 오더북 데이터 읽기 (ws_exit_monitor가 갱신)
    if _TYPE_B_REQUIRE_OB_RATIO and _ob_ratio is None:
        _log("type_b.reject.ob_ratio_missing", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%")
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_ob_ratio_missing",
        )
        _record_scan("reject", "reject_ob_ratio_missing")
        _reject(
            "reject_ob_ratio_missing",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
        )
        return None
    if _ob_ratio is not None and _ob_ratio < _TYPE_B_MIN_OB_RATIO:
        _log("type_b.reject.ob_ratio_weak", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", ob_ratio=f"{_ob_ratio:.3f}")
        _maybe_save_type_b_alt_shadow_candidates(
            r,
            symbol=symbol,
            current_price=current_price,
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
            base_reject_reason="reject_ob_ratio_weak",
        )
        _record_scan("reject", "reject_ob_ratio_weak")
        _reject(
            "reject_ob_ratio_weak",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
        )
        return None

    # Claude 평가 (Type B 전용 프롬프트)
    try:
        from ai.providers.base import build_type_b_prompt, parse_decision_response
        prompt = build_type_b_prompt(symbol, change_rate, trade_price, high_price, ret_5m, vol_krw, ob_ratio=_ob_ratio)
        ai_client = _get_anthropic_client()
        resp = ai_client.messages.create(
            model=os.getenv("AI_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        emit, direction, confidence, reason = parse_decision_response(raw)
    except Exception as e:
        _log("type_b.claude_error", symbol=symbol, error=str(e))
        _record_scan("reject", "reject_claude_error")
        _reject(
            "reject_claude_error",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
        )
        return None

    if not emit or direction != "LONG":
        _log("type_b.reject.claude_hold", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", reason=reason[:60])
        _record_scan("reject", "reject_claude_hold")
        _reject(
            "reject_claude_hold",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
        )
        return None

    # 신호 생성
    stop_pct = Decimal(os.getenv("COIN_EXIT_STOP_LOSS_PCT", "0.030"))
    take_pct = Decimal(os.getenv("COIN_EXIT_TAKE_PROFIT_PCT", "0.150"))
    stop_price = current_price * (1 - stop_pct)

    signal_id = str(uuid.uuid4())
    ts_now = datetime.now(_KST).isoformat()
    size_cash = _calc_size_cash(market, current_price)

    try:
        signal = Signal(
            signal_id=signal_id,
            ts=ts_now,
            market=market,
            symbol=symbol,
            direction="LONG",
            entry=SignalEntry(price=current_price, size_cash=size_cash),
            stop=SignalStop(price=stop_price),
            stop_pct=stop_pct,
            take_pct=take_pct,
        )
    except Exception as e:
        _log("type_b.signal_error", symbol=symbol, error=str(e))
        _record_scan("reject", "reject_signal_error")
        _reject(
            "reject_signal_error",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
        )
        return None

    r.set(tb_cooldown_key, "1", ex=_TYPE_B_COOLDOWN_SEC)

    payload = {
        "signal_id": signal.signal_id,
        "ts": signal.ts,
        "market": market,
        "symbol": symbol,
        "direction": "LONG",
        "entry": {"price": str(current_price), "size_cash": str(size_cash)},
        "stop": {"price": str(stop_price)},
        "source": "consensus_signal_runner_type_b",
        "status": "candidate",
        "strategy": "trend_riding",
        "claude_emit": 1,
        "claude_conf": str(confidence),
        "ret_5m": ret_5m,
        "change_rate_daily": change_rate,
        "high_price": high_price,
        "near_high": near_high,
        "vol_24h": vol_krw,
        "ob_ratio": _ob_ratio,
        "signal_family": "type_b",
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
    }

    if signal_mode == "shadow":
        _record_scan("shadow_candidate")
        payload["status"] = "shadow_candidate"
        payload["shadow_stage"] = "post_consensus"
        payload["shadow_origin"] = "consensus_runner_type_b_shadow_candidate"
        save_signal_snapshot(r, payload)
        _save_audit(
            r,
            market,
            signal,
            ret_5m,
            None,
            source="consensus_signal_runner_type_b_shadow",
            stats_field="shadow_candidate",
            increment_daily_count=False,
        )
        _record_type_b_stat(r, "shadow_candidate")
        _log("type_b.shadow_only.candidate_saved", symbol=symbol, signal_id=signal_id, mode=signal_mode)
        return payload

    try:
        r.lpush("claw:signal:queue", json.dumps(payload))
    except Exception as e:
        _log("type_b.publish_failed", symbol=symbol, error=str(e))
        r.delete(tb_cooldown_key)
        _record_scan("reject", "reject_publish_failed")
        _reject(
            "reject_publish_failed",
            change_rate=change_rate,
            near_high=near_high,
            ret_5m=ret_5m,
            trade_price=trade_price,
            high_price=high_price,
            vol_24h=vol_krw,
            ob_ratio=_ob_ratio,
        )
        return None
    _record_scan("candidate")
    save_signal_snapshot(r, payload)
    _record_type_b_stat(r, "candidate")

    # 종목별 일일 진입 카운터 증가 (Type A와 공유)
    r.incr(symbol_daily_key)
    r.expire(symbol_daily_key, 86400)

    r.hset(f"claw:signal_pct:{market}:{symbol}", mapping={
        "stop_pct": str(stop_pct), "take_pct": str(take_pct),
    })
    r.expire(f"claw:signal_pct:{market}:{symbol}", 86400)

    _log("type_b.pass.signal_created", symbol=symbol, signal_id=signal_id,
         change_rate=f"{change_rate*100:.1f}%", confidence=f"{confidence:.2f}",
         entry=str(current_price), reason=reason[:60])
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
    watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US")
    watchlist_coin = load_watchlist(r, "COIN", "GEN_WATCHLIST_COIN")

    if not watchlist_kr and not watchlist_us and not watchlist_coin:
        print("consensus: all watchlists empty — exiting", flush=True)
        r.delete(_LOCK_KEY)
        sys.exit(1)

    print(
        f"consensus: started poll_sec={_POLL_SEC} strategy=momentum_breakout "
        f"prefilter_coin=ret_5m>{_MIN_SURGE_5M_COIN} "
        f"coin_min_ret1m={_MIN_RET_1M_COIN} "
        f"coin_min_accel_ratio={_COIN_MIN_RET1M_TO_RET5M_RATIO} "
        f"coin_type_a_mode={get_signal_family_mode(r, 'COIN', 'type_a')} "
        f"coin_type_b_mode={get_signal_family_mode(r, 'COIN', 'type_b')} "
        f"prefilter_us=ret_5m>{_MIN_SURGE_5M_US} "
        f"prefilter_kr=ret_5m>{_MIN_SURGE_5M_KR} "
        f"range_5m>{_MIN_RANGE_5M} "
        f"volume_ratio_coin={_VOLUME_SURGE_RATIO} "
        f"volume_ratio_kr={_VOLUME_SURGE_RATIO_KR} "
        f"volume_min_samples_coin={_VOLUME_SURGE_MIN_SAMPLES} "
        f"volume_min_samples_kr={_VOLUME_SURGE_MIN_SAMPLES_KR} "
        f"kr_stop_pct={os.getenv('EXIT_STOP_LOSS_PCT', '0.015')} "
        f"kr_take_pct={os.getenv('EXIT_TAKE_PROFIT_PCT', '0.030')} "
        f"coin_stop_pct={os.getenv('COIN_EXIT_STOP_LOSS_PCT', '0.030')} "
        f"coin_take_pct={os.getenv('COIN_EXIT_TAKE_PROFIT_PCT', '0.150')} "
        f"watchlist_kr={watchlist_kr} "
        f"watchlist_us={watchlist_us} "
        f"watchlist_coin={watchlist_coin}",
        flush=True,
    )

    _last_type_b_ts = 0.0  # Type B 마지막 체크 시각

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            if is_paused(r):
                time.sleep(_POLL_SEC)
                continue

            # KR 처리 (장중일 때만)
            if is_market_hours("KR"):
                watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
                regime = _get_regime(r, "KR", watchlist_kr)
                _log("runner.regime", market="KR", regime=regime,
                     watchlist_size=len(watchlist_kr))
                for symbol in watchlist_kr:
                    if regime == "bearish":
                        continue  # 하락장에서 LONG 억제
                    try:
                        run_once("KR", symbol, r)
                    except Exception as e:
                        _log("runner.error.unexpected", market="KR",
                             symbol=symbol, error=str(e))

            # US 처리 (장중일 때만)
            if is_market_hours("US"):
                watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US")
                for symbol in watchlist_us:
                    try:
                        run_once("US", symbol, r)
                    except Exception as e:
                        _log("runner.error.unexpected", market="US",
                             symbol=symbol, error=str(e))

            # COIN 처리 (24/7)
            watchlist_coin = load_watchlist(r, "COIN", "GEN_WATCHLIST_COIN")
            if watchlist_coin:
                regime = _get_regime(r, "COIN", watchlist_coin)
                coin_type_a_mode = get_signal_family_mode(r, "COIN", "type_a")
                coin_type_b_mode = get_signal_family_mode(r, "COIN", "type_b")
                _log("runner.regime", market="COIN", regime=regime,
                     watchlist_size=len(watchlist_coin))
                if coin_type_a_mode != "off":
                    for symbol in watchlist_coin:
                        if regime == "bearish":
                            continue
                        try:
                            run_once("COIN", symbol, r)
                        except Exception as e:
                            _log("runner.error.unexpected", market="COIN",
                                 symbol=symbol, error=str(e))

                # Type B: 5분마다 추세 탑승 신호 체크
                if (
                    coin_type_b_mode != "off"
                    and time.time() - _last_type_b_ts >= _TYPE_B_POLL_SEC
                    and regime != "bearish"
                ):
                    today = today_kst()
                    for symbol in watchlist_coin:
                        try:
                            _run_type_b_coin(symbol, r, today)
                        except Exception as e:
                            _log("runner.error.type_b", market="COIN",
                                 symbol=symbol, error=str(e))
                    _last_type_b_ts = time.time()

            time.sleep(_POLL_SEC)

    finally:
        r.delete(_LOCK_KEY)
        print("consensus: lock released", flush=True)


if __name__ == "__main__":
    main()
