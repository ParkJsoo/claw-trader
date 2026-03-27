from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

from pydantic import BaseModel, Field

_LUA_CAP_INCR = """
local v = redis.call('INCR', KEYS[1])
if v == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if v > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return -1
end
return v
"""
from redis import Redis

from domain.models import Signal


@dataclass
class MarketStrategyConfig:
    cooldown_sec: int = 300           # 5분 쿨다운 (동일 종목 재진입 간격)
    daily_cap: int = 20               # 일 최대 처리 신호 수 (시장별)
    dedupe_ttl_sec: int = 7 * 86400   # 7일 dedupe (Executor 6h idempo보다 앞단 보장)


@dataclass
class StrategyConfig:
    kr: MarketStrategyConfig = field(default_factory=MarketStrategyConfig)
    us: MarketStrategyConfig = field(default_factory=MarketStrategyConfig)
    coin: MarketStrategyConfig = field(default_factory=MarketStrategyConfig)

    def for_market(self, market: str) -> MarketStrategyConfig:
        if market == "KR":
            return self.kr
        if market == "US":
            return self.us
        if market == "COIN":
            return self.coin
        raise ValueError(f"Unknown market: {market}")


class StrategyDecision(BaseModel):
    allow: bool
    reason: str
    meta: dict[str, Any] = Field(default_factory=dict)


class StrategyEngine:
    """
    신호 품질 필터 (Phase 6).
    브로커 API 호출 없음 — Redis + signal 데이터만 사용.
    역할: 중복/쿨다운/일일 캡 제어 (RiskEngine과 역할 분리)
    """

    def __init__(self, redis: Redis, cfg: StrategyConfig):
        self.redis = redis
        self.cfg = cfg

    # -------------------------
    # 규칙 (비용 기준 순서)
    # -------------------------

    def _rule_dedupe(self, signal: Signal, cfg: MarketStrategyConfig) -> Optional[StrategyDecision]:
        """
        동일 signal_id 재처리 방지.
        SET NX 성공 = 첫 처리 → 통과
        SET NX 실패 = 이미 처리됨 → DUP_SIGNAL
        TTL 7d: Executor idempo(6h)보다 길게 유지해 앞단 의미 확보.
        """
        key = f"strategy:dedupe:{signal.market}:{signal.signal_id}"
        set_ok = self.redis.set(key, "1", nx=True, ex=cfg.dedupe_ttl_sec)
        if not set_ok:
            return StrategyDecision(
                allow=False,
                reason="DUP_SIGNAL",
                meta={"signal_id": signal.signal_id, "market": signal.market},
            )
        return None

    def _rule_cooldown(self, signal: Signal, cfg: MarketStrategyConfig) -> Optional[StrategyDecision]:
        """
        동일 종목 재진입 쿨다운.
        last_ts_ms 기준 경과 시간이 cooldown_sec 미만이면 차단.
        corrupt 값 → 쿨다운 미적용 (방어적 통과).
        """
        key = f"strategy:cooldown:{signal.market}:{signal.symbol}"
        raw = self.redis.get(key)
        if raw is not None:
            try:
                last_ms = int(raw.decode())
                now_ms = int(time.time() * 1000)
                elapsed_ms = now_ms - last_ms
                cooldown_ms = cfg.cooldown_sec * 1000
                if elapsed_ms < cooldown_ms:
                    return StrategyDecision(
                        allow=False,
                        reason="COOLDOWN",
                        meta={
                            "symbol": signal.symbol,
                            "elapsed_sec": elapsed_ms // 1000,
                            "cooldown_sec": cfg.cooldown_sec,
                            "remaining_sec": (cooldown_ms - elapsed_ms) // 1000,
                        },
                    )
            except (ValueError, Exception):
                pass  # corrupt → 통과
        return None

    def _rule_daily_cap(self, signal: Signal, cfg: MarketStrategyConfig) -> Optional[StrategyDecision]:
        """
        시장별 일일 최대 신호 수 제한.
        Lua 원자적 INCR+cap check (multi-process safe, DECR 롤백 내장).
        """
        today = datetime.now(_KST).strftime("%Y%m%d")
        key = f"strategy:daily_count:{signal.market}:{today}"
        cnt = self.redis.eval(_LUA_CAP_INCR, 1, key, cfg.daily_cap, 3 * 86400)
        if cnt == -1:
            return StrategyDecision(
                allow=False,
                reason="DAILY_CAP",
                meta={
                    "cap": cfg.daily_cap,
                    "market": signal.market,
                    "date": today,
                },
            )
        return None

    # -------------------------
    # 부가 처리
    # -------------------------

    def _set_cooldown(self, signal: Signal, cfg: MarketStrategyConfig) -> None:
        """통과 시에만 쿨다운 타임스탬프 갱신."""
        key = f"strategy:cooldown:{signal.market}:{signal.symbol}"
        now_ms = str(int(time.time() * 1000))
        self.redis.set(key, now_ms, ex=cfg.cooldown_sec)

    def _record_counters(self, signal: Signal, decision: StrategyDecision) -> None:
        """관측성 카운터 (6-5). pass/reject 비율 모니터링용."""
        today = datetime.now(_KST).strftime("%Y%m%d")
        if decision.allow:
            key = f"strategy:pass_count:{signal.market}:{today}"
            self.redis.incr(key)
            self.redis.expire(key, 7 * 86400)
        else:
            key = f"strategy:reject_count:{signal.market}:{today}"
            self.redis.hincrby(key, decision.reason, 1)
            self.redis.expire(key, 7 * 86400)

    # -------------------------
    # 진입점
    # -------------------------

    def check(self, signal: Signal) -> StrategyDecision:
        cfg = self.cfg.for_market(signal.market)
        rules = [
            lambda: self._rule_dedupe(signal, cfg),
            lambda: self._rule_cooldown(signal, cfg),
            lambda: self._rule_daily_cap(signal, cfg),
        ]
        for rule in rules:
            decision = rule()
            if decision is not None:
                self._record_counters(signal, decision)
                return decision

        # 전체 통과 → 쿨다운 갱신 후 반환
        self._set_cooldown(signal, cfg)
        result = StrategyDecision(allow=True, reason="STRATEGY_OK", meta={})
        self._record_counters(signal, result)
        return result
