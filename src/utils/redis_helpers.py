"""공통 Redis/운영 헬퍼."""
import os
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")
_VALID_SIGNAL_MODES = {"live", "shadow", "off"}


def secs_until_kst_midnight() -> int:
    """오늘 자정 KST까지 남은 초 (최소 60초)."""
    now = datetime.now(_KST)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()), 60)


def parse_watchlist(env_key: str) -> list[str]:
    raw = os.getenv(env_key, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def today_kst() -> str:
    return datetime.now(_KST).strftime("%Y%m%d")


def is_market_hours(market: str) -> bool:
    """장중 여부 확인 (버퍼 ±10분).

    KR: 평일 08:50~15:40 KST (정규장 09:00~15:30)
    US: 평일 09:20~16:10 ET  (정규장 09:30~16:00)
    주말은 양 시장 모두 False.
    """
    if market == "KR":
        now = datetime.now(_KST)
        if now.weekday() >= 5:
            return False
        return dtime(8, 50) <= now.time() <= dtime(15, 40)
    if market == "US":
        now = datetime.now(_ET)
        if now.weekday() >= 5:
            return False
        return dtime(9, 20) <= now.time() <= dtime(16, 10)
    if market == "COIN":
        return True  # 업비트 코인 24/7
    return True


def load_watchlist(r, market: str, env_key: str) -> list[str]:
    """동적 워치리스트 우선 조회 → fallback으로 env var 사용.

    Redis SET `dynamic:watchlist:{market}` 가 있으면 해당 심볼 사용,
    없으면 기존 env var (GEN_WATCHLIST_KR 등) fallback.
    """
    redis_key = f"dynamic:watchlist:{market}"
    try:
        members = r.smembers(redis_key)
        if members:
            symbols = sorted(
                m.decode() if isinstance(m, bytes) else m
                for m in members
            )
            return symbols
    except Exception:
        pass
    return parse_watchlist(env_key)


def get_config(r, market: str, key: str, default: float) -> float:
    """Redis claw:config:{market} hash에서 파라미터 읽기. 없으면 default 반환."""
    val = r.hget(f"claw:config:{market}", key)
    if val:
        return float(val.decode() if isinstance(val, bytes) else val)
    return default


def _is_truthy_redis_value(val) -> bool:
    if val is None:
        return False
    s = val.decode() if isinstance(val, bytes) else str(val)
    return s.lower() in ("true", "1", "yes")


def infer_signal_family(
    signal_family: str | None = None,
    *,
    strategy: str | None = None,
    source: str | None = None,
) -> str:
    family = (signal_family or "").strip().lower()
    if family:
        return family

    strategy_norm = (strategy or "").strip().lower()
    source_norm = (source or "").strip().lower()
    if "type_b" in source_norm or strategy_norm == "trend_riding":
        return "type_b"
    if strategy_norm == "momentum_breakout":
        return "type_a"
    return ""


def _normalize_signal_mode(value, default: str = "live") -> str:
    raw = value.decode() if isinstance(value, bytes) else str(value or "")
    mode = raw.strip().lower()
    if mode in _VALID_SIGNAL_MODES:
        return mode
    return default


def get_signal_family_mode(
    r,
    market: str,
    signal_family: str | None = None,
    *,
    strategy: str | None = None,
    source: str | None = None,
    default: str = "live",
) -> str:
    market_norm = (market or "").strip().upper()
    family = infer_signal_family(signal_family, strategy=strategy, source=source)
    if not market_norm or not family:
        return default

    mode = _normalize_signal_mode(os.getenv(f"{market_norm}_{family.upper()}_MODE"), default)
    try:
        override = r.hget(f"claw:signal_mode:{market_norm}", family)
        if override is not None:
            mode = _normalize_signal_mode(override, mode)
    except Exception:
        pass
    return mode


def get_signal_family_modes(r, market: str, families: tuple[str, ...] = ("type_a", "type_b")) -> dict[str, str]:
    return {family: get_signal_family_mode(r, market, family) for family in families}


def is_paused(r) -> bool:
    """claw:pause:global 상태 확인 (true/1/yes 모두 처리)."""
    try:
        return _is_truthy_redis_value(r.get("claw:pause:global"))
    except Exception:
        return False


def is_market_paused(r, market: str) -> bool:
    """시장별 pause 상태 확인. 예: claw:pause:COIN=true."""
    try:
        return _is_truthy_redis_value(r.get(f"claw:pause:{market}"))
    except Exception:
        return False
