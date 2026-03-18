"""공통 Redis/운영 헬퍼."""
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


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


def is_paused(r) -> bool:
    """claw:pause:global 상태 확인 (true/1/yes 모두 처리)."""
    try:
        val = r.get("claw:pause:global")
        if val is None:
            return False
        s = val.decode() if isinstance(val, bytes) else val
        return s.lower() in ("true", "1", "yes")
    except Exception:
        return False
