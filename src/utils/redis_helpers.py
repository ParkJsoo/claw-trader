"""공통 Redis/운영 헬퍼."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def parse_watchlist(env_key: str) -> list[str]:
    raw = os.getenv(env_key, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def today_kst() -> str:
    return datetime.now(_KST).strftime("%Y%m%d")


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
