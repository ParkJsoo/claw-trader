from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from zoneinfo import ZoneInfo

from redis import Redis

_KST = ZoneInfo("Asia/Seoul")
_GEN_TTL = 7 * 86400  # 7일
_GEN_COOLDOWN_SEC = int(os.getenv("GEN_COOLDOWN_SEC", "300"))
_GEN_DAILY_EMIT_CAP = int(os.getenv("GEN_DAILY_EMIT_CAP", "20"))
_GEN_MIN_HIST = int(os.getenv("GEN_MIN_HIST", "20"))
_GEN_STOP_PCT = Decimal(os.getenv("GEN_STOP_PCT", "0.03"))
_GEN_DAILY_CALL_CAP = int(os.getenv("GEN_DAILY_CALL_CAP", "1000"))

from utils.redis_helpers import secs_until_kst_midnight as _secs_until_kst_midnight


_LUA_CAP_INCR = """
local v = redis.call('INCR', KEYS[1])
if v == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if v > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return -1
end
return v
"""


class _GeneratorDecision:
    def __init__(self, emit: bool, direction: str, size_cash: Decimal, reason: str):
        self.emit = emit
        self.direction = direction
        self.size_cash = size_cash
        self.reason = reason


class AISignalGenerator:
    """
    AI 기반 신호 생성기 (Phase 8 v2).
    mark_hist 기반 모멘텀 피처를 계산하여 AI에게 신호 생성 여부를 물어봄.
    결과는 claw:signal:queue에 push (기존 파이프라인 재사용).

    환경변수:
    - ANTHROPIC_API_KEY: 필수 (runner에서 체크 후 생성)
    - AI_MODEL: 모델 (기본값: claude-haiku-4-5-20251001)
    - GEN_COOLDOWN_SEC: 심볼별 쿨다운 초 (기본값: 300)
    - GEN_DAILY_EMIT_CAP: 시장별 일일 발행 한도 (기본값: 20)
    - GEN_MIN_HIST: cold start 가드 최소 히스토리 개수 (기본값: 20)
    - GEN_STOP_PCT: stop loss 비율 (기본값: 0.03 = 3%)
    """

    def __init__(self, redis: Redis):
        self.redis = redis
        self.model = os.getenv("AI_MODEL", "claude-haiku-4-5-20251001")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic()
        return self._client

    def _set_auto_pause(self, reason: str, market: str, detail: str) -> None:
        """전역 일시정지 설정 (NX: 첫 발동만 기록) + TG 알림."""
        from guards.notifier import send_telegram
        set_ok = self.redis.set("claw:pause:global", "true", nx=True, ex=_secs_until_kst_midnight())
        if set_ok:
            ts_ms = str(int(time.time() * 1000))
            self.redis.set("claw:pause:reason", reason)
            self.redis.hset("claw:pause:meta", mapping={
                "reason": reason, "market": market, "detail": detail,
                "ts_ms": ts_ms, "source": "ai_generator",
            })
            sent = send_telegram(f"[CLAW] AUTO-PAUSE: {reason}\nmarket={market}\n{detail}")
            print(f"generator: auto_pause reason={reason} market={market} detail={detail} tg_sent={sent}", flush=True)
        else:
            print(f"generator: auto_pause already active; skip telegram reason={reason} market={market}", flush=True)

    def _get_hist(self, market: str, symbol: str) -> list[str]:
        key = f"mark_hist:{market}:{symbol}"
        raw = self.redis.lrange(key, 0, 299)
        return [r.decode() if isinstance(r, bytes) else r for r in raw]

    def _compute_features(self, entries: list[str], now_ms: int) -> Optional[dict[str, Any]]:
        """mark_hist 엔트리에서 모멘텀 피처 계산. 실패 시 None 반환."""
        parsed: list[tuple[int, Decimal]] = []
        for e in entries:
            try:
                ts_str, price_str = e.split(":", 1)
                parsed.append((int(ts_str), Decimal(price_str)))
            except Exception:
                continue
        if not parsed:
            return None

        current_price = parsed[0][1]  # LPUSH 기준 인덱스 0이 최신

        def price_near(target_ms: int) -> Optional[Decimal]:
            best_ts, best_p = None, None
            for ts, p in parsed:
                if best_ts is None or abs(ts - target_ms) < abs(best_ts - target_ms):
                    best_ts, best_p = ts, p
            # 2분 초과 시 유효 데이터 없음으로 처리 (잘못된 피처 방지)
            if best_ts is None or abs(best_ts - target_ms) > 120_000:
                return None
            return best_p

        def ret(p_old: Optional[Decimal]) -> Optional[float]:
            if p_old is None or p_old == 0:
                return None
            return float((current_price - p_old) / p_old)

        p1m = price_near(now_ms - 60_000)
        p5m = price_near(now_ms - 300_000)

        prices_5m = [p for ts, p in parsed if ts >= now_ms - 300_000]
        range_5m: Optional[float] = None
        if prices_5m and current_price > 0:
            range_5m = float((max(prices_5m) - min(prices_5m)) / current_price)

        return {
            "current_price": str(current_price),
            "ret_1m": ret(p1m),
            "ret_5m": ret(p5m),
            "range_5m": range_5m,
        }

    def _build_prompt(self, market: str, symbol: str, features: dict[str, Any]) -> str:
        def fmt(v: Optional[float]) -> str:
            return f"{v:.4f}" if v is not None else "N/A"

        if market == "KR":
            market_ctx = (
                "Market: KR (KOSPI/KOSDAQ, Korean Won, session 09:00-15:30 KST)\n"
                "Typical size_cash range: 100000-500000 (KRW). "
                "Emit a LONG signal only when BOTH conditions are clearly satisfied: "
                "1. ret_5m > 0  2. range_5m > 0.004. "
                "If ret_5m <= 0, do NOT emit LONG. "
                "If volatility exists but directional momentum is unclear, return HOLD. "
                "Prefer HOLD over weak or ambiguous setups."
            )
        else:
            market_ctx = (
                "Market: US (NYSE/NASDAQ, USD, session 09:30-16:00 ET)\n"
                "Typical size_cash range: 100-1000 (USD). "
                "Signal on clear 1-5min momentum with range_5m > 0.003."
            )

        lines = [
            "You are a cash-only equity trading signal generator.",
            "Decide whether to emit a trading signal based on recent price momentum.",
            "",
            market_ctx,
            f"Symbol: {symbol}",
            f"Current price: {features['current_price']}",
            f"1-min return: {fmt(features['ret_1m'])}",
            f"5-min return: {fmt(features['ret_5m'])}",
            f"5-min range: {fmt(features['range_5m'])}",
            "",
            "Constraints: cash-only, direction must be LONG or EXIT only.",
            "size_cash must be in the market currency shown above.",
            "",
            "Respond with JSON only (no markdown):",
            '{"emit": true|false, "direction": "LONG|EXIT", "size_cash": <number>, "reason": "<100 chars"}',
        ]
        return "\n".join(lines)

    def _parse_response(self, text: str, max_size_cash: Decimal) -> _GeneratorDecision:
        clean = text.strip()
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end + 1]

        data = json.loads(clean)

        emit = bool(data.get("emit", False))
        direction = data.get("direction", "LONG")
        if direction not in ("LONG", "EXIT"):
            direction = "LONG"

        try:
            size_cash = Decimal(str(data.get("size_cash", "0")))
            size_cash = max(Decimal("0"), min(size_cash, max_size_cash))
        except (InvalidOperation, Exception):
            size_cash = Decimal("0")
            emit = False

        reason = str(data.get("reason", ""))[:100]
        return _GeneratorDecision(emit=emit, direction=direction, size_cash=size_cash, reason=reason)

    def _save_audit(
        self,
        market: str,
        signal_id: str,
        symbol: str,
        features: dict[str, Any],
        decision: _GeneratorDecision,
        raw_response: str,
        emit_blocked: bool = False,
        block_reason: str = "",
        stop_adjusted: bool = False,
    ) -> None:
        ts_ms = int(time.time() * 1000)
        today = datetime.now(_KST).strftime("%Y%m%d")

        gen_key = f"ai:gen:{market}:{signal_id}"
        self.redis.hset(gen_key, mapping={
            "ts_ms": str(ts_ms),
            "symbol": symbol,
            "direction": decision.direction,
            "size_cash": str(decision.size_cash),
            "emit": "1" if decision.emit else "0",
            "emit_blocked": "1" if emit_blocked else "0",
            "block_reason": block_reason,
            "stop_adjusted": "1" if stop_adjusted else "0",
            "features_json": json.dumps({k: str(v) if v is not None else None for k, v in features.items()}),
            "raw_response": raw_response[:2000],
            "reason": decision.reason,
            "model": self.model,
            "provider": "anthropic",
        })
        self.redis.expire(gen_key, _GEN_TTL)

        idx_key = f"ai:gen_index:{market}:{today}"
        self.redis.zadd(idx_key, {signal_id: ts_ms})
        self.redis.expire(idx_key, _GEN_TTL)

        stats_key = f"ai:gen_stats:{market}:{today}"
        if emit_blocked:
            stat_field = f"skip_{block_reason}"
        elif decision.emit:
            stat_field = "generated"
        else:
            stat_field = "no_emit"
        self.redis.hincrby(stats_key, stat_field, 1)
        self.redis.expire(stats_key, _GEN_TTL)

    def generate(self, market: str, symbol: str, max_size_cash: Decimal) -> Optional[dict]:
        """
        심볼에 대한 신호 생성 시도.
        Returns signal dict if should emit to queue, None otherwise.
        항상 ai:gen_stats에 기록 (cold_start 포함).
        """
        now_ms = int(time.time() * 1000)
        today = datetime.now(_KST).strftime("%Y%m%d")

        # cold start 가드 — 히스토리 부족 시 스킵
        entries = self._get_hist(market, symbol)
        if len(entries) < _GEN_MIN_HIST:
            stats_key = f"ai:gen_stats:{market}:{today}"
            self.redis.hincrby(stats_key, "skip_cold_start", 1)
            self.redis.expire(stats_key, _GEN_TTL)
            return None

        features = self._compute_features(entries, now_ms)
        if not features:
            stats_key = f"ai:gen_stats:{market}:{today}"
            self.redis.hincrby(stats_key, "skip_feature_error", 1)
            self.redis.expire(stats_key, _GEN_TTL)
            return None

        signal_id = str(uuid.uuid4())

        # AI 호출 하드 캡 (Lua 원자적 INCR + cap check)
        call_key = f"ai:call_count:{market}:{today}"
        call_count = self.redis.eval(_LUA_CAP_INCR, 1, call_key, _GEN_DAILY_CALL_CAP, 3 * 86400)
        if call_count == -1:
            self._set_auto_pause(
                "AI_CALL_CAP_EXCEEDED", market,
                f"call_count={call_count} cap={_GEN_DAILY_CALL_CAP}",
            )
            stats_key = f"ai:gen_stats:{market}:{today}"
            self.redis.hincrby(stats_key, "skip_call_cap", 1)
            self.redis.expire(stats_key, _GEN_TTL)
            return None

        raw_response = ""
        try:
            client = self._get_client()
            prompt = self._build_prompt(market, symbol, features)
            response = client.messages.create(
                model=self.model,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_response = response.content[0].text
            decision = self._parse_response(raw_response, max_size_cash)
        except Exception as e:
            stats_key = f"ai:gen_stats:{market}:{today}"
            self.redis.hincrby(stats_key, f"error_{type(e).__name__}", 1)
            self.redis.expire(stats_key, _GEN_TTL)
            return None

        if not decision.emit:
            self._save_audit(market, signal_id, symbol, features, decision, raw_response)
            return None

        # 보완 1: EXIT는 포지션 있을 때만
        if decision.direction == "EXIT":
            raw_qty = self.redis.hget(f"position:{market}:{symbol}", "qty")
            has_position = False
            if raw_qty is not None:
                try:
                    has_position = Decimal(raw_qty.decode() if isinstance(raw_qty, bytes) else raw_qty) > 0
                except Exception:
                    pass
            if not has_position:
                self._save_audit(market, signal_id, symbol, features, decision, raw_response,
                                 emit_blocked=True, block_reason="no_position")
                return None

        # 심볼별 쿨다운 먼저 체크 (daily_cap 소비 전에 차단 — cap 낭비 방지)
        cooldown_key = f"gen:cooldown:{market}:{symbol}"
        if not self.redis.set(cooldown_key, "1", nx=True, ex=_GEN_COOLDOWN_SEC):
            self._save_audit(market, signal_id, symbol, features, decision, raw_response,
                             emit_blocked=True, block_reason="cooldown")
            return None

        # daily_cap: Lua 원자적 INCR + cap check (multi-process safe)
        daily_key = f"gen:daily_emit:{market}:{today}"
        count = self.redis.eval(_LUA_CAP_INCR, 1, daily_key, _GEN_DAILY_EMIT_CAP, 3 * 86400)
        if count == -1:
            # daily_cap 초과 시 쿨다운 키 삭제 (다음 루프에서 재시도 가능하게)
            self.redis.delete(cooldown_key)
            self._save_audit(market, signal_id, symbol, features, decision, raw_response,
                             emit_blocked=True, block_reason="daily_cap")
            return None

        # 시장별 stop_price 라운딩 (KR=원 단위, US=센트 단위) + <= 0 방어
        current_price = Decimal(features["current_price"])
        stop_quantize = Decimal("1") if market == "KR" else Decimal("0.01")
        stop_price = (current_price * (1 - _GEN_STOP_PCT)).quantize(stop_quantize)
        stop_adjusted = False
        if stop_price <= 0:
            stop_price = stop_quantize  # 최솟값 1단위
            stop_adjusted = True

        signal = {
            "signal_id": signal_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "ts_ms": str(now_ms),
            "market": market,
            "symbol": symbol,
            "direction": decision.direction,
            "entry": {
                "price": str(current_price),
                "size_cash": str(decision.size_cash),
            },
            "stop": {"price": str(stop_price)},
        }

        self._save_audit(market, signal_id, symbol, features, decision, raw_response,
                         stop_adjusted=stop_adjusted)
        return signal
