from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from redis import Redis


@dataclass
class GuardDecision:
    allow: bool
    severity: str          # "OK" | "WARN" | "BLOCK"
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


class DataGuard:
    """
    Market Data staleness 감지.
    md:last_update:{market} ts_ms 기준으로 stale 여부 판단.

    v1 정책:
    - hard block 기본 OFF (env: MD_STALE_HARD_BLOCK=0)
    - stale이면 allow=True, severity="WARN" → 로그만
    - hard block ON 시 allow=False → runner에서 신호 차단

    stale 기준: MD_STALE_SEC (기본값 30초)
    장 시간 외 체크는 v2로 미룸.
    """

    def __init__(self, redis: Redis):
        self.redis = redis
        self.stale_sec = int(os.getenv("MD_STALE_SEC", "30"))
        self.hard_block = (
            os.getenv("MD_STALE_HARD_BLOCK", "0").strip().lower()
            in ("1", "true", "yes")
        )

    def check(self, market: str) -> GuardDecision:
        key = f"md:last_update:{market}"
        raw = self.redis.get(key)

        if raw is None:
            # 아직 한 번도 업데이트 없음 (market data runner 미실행)
            meta = {"market": market}
            if self.hard_block:
                return GuardDecision(allow=False, severity="BLOCK", reason="MD_NO_DATA", meta=meta)
            return GuardDecision(allow=True, severity="WARN", reason="MD_NO_DATA", meta=meta)

        try:
            last_ts_ms = int(raw.decode())
            elapsed_sec = (int(time.time() * 1000) - last_ts_ms) / 1000
        except Exception:
            meta = {"market": market}
            if self.hard_block:
                return GuardDecision(allow=False, severity="BLOCK", reason="MD_PARSE_ERROR", meta=meta)
            return GuardDecision(allow=True, severity="WARN", reason="MD_PARSE_ERROR", meta=meta)

        if elapsed_sec > self.stale_sec:
            meta = {
                "market": market,
                "elapsed_sec": int(elapsed_sec),
                "stale_sec": self.stale_sec,
            }
            if self.hard_block:
                return GuardDecision(allow=False, severity="BLOCK", reason="MD_STALE", meta=meta)
            return GuardDecision(allow=True, severity="WARN", reason="MD_STALE", meta=meta)

        return GuardDecision(
            allow=True,
            severity="OK",
            reason="MD_OK",
            meta={"market": market, "elapsed_sec": int(elapsed_sec)},
        )
