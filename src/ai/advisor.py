from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from redis import Redis

from domain.models import Signal

_KST = ZoneInfo("Asia/Seoul")
_ADVICE_TTL = 30 * 86400  # 30일


class AdvisoryDecision(BaseModel):
    recommend: Literal["ALLOW", "BLOCK", "WARN", "ERROR"]
    confidence: float = 0.0
    reason: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class AIAdvisor:
    """
    AI 기반 신호 추천 (Shadow mode — 실행 흐름에 영향 없음).
    Strategy 통과 신호에 대해 추천만 기록 (runner에서 Strategy 통과 후 호출).
    advise() 실패 시 ERROR decision 반환 — 예외 전파 없음.

    환경변수:
    - ANTHROPIC_API_KEY: 필수. runner.py에서 키 없으면 advisor=None으로 생성 스킵.
      (정책: 키 존재 여부 체크는 runner 진입 시 1회 수행 — advisor 내부 이중 체크 없음)
    - AI_MODEL: 사용 모델 (기본값: claude-haiku-4-5-20251001)
    """

    def __init__(self, redis: Redis):
        self.redis = redis
        self.model = os.getenv("AI_MODEL", "claude-haiku-4-5-20251001")
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic()
        return self._client

    def _build_prompt(self, signal: Signal, strategy_reason: Optional[str]) -> str:
        lines = [
            "You are a trading signal advisor for a cash-only automated trading system.",
            "Analyze this signal and give a shadow recommendation (no effect on execution).",
            "",
            "Signal:",
            f"- Market: {signal.market}",
            f"- Symbol: {signal.symbol}",
            f"- Direction: {signal.direction}",
            f"- Size (cash): {signal.entry.size_cash}",
        ]
        if strategy_reason:
            lines.append(f"- Strategy filter result: {strategy_reason}")
        lines += [
            "",
            "Respond with JSON only (no markdown):",
            '{"recommend": "ALLOW|BLOCK|WARN", "confidence": 0.0-1.0, "reason": "under 100 chars"}',
        ]
        return "\n".join(lines)

    def _parse_response(self, text: str) -> AdvisoryDecision:
        # 첫 { ~ 마지막 } substring 추출 (markdown/설명 텍스트 무시)
        clean = text.strip()
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end + 1]

        data = json.loads(clean)

        recommend = data.get("recommend", "WARN")
        if recommend not in ("ALLOW", "BLOCK", "WARN"):
            recommend = "WARN"

        try:
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.5

        return AdvisoryDecision(
            recommend=recommend,
            confidence=confidence,
            reason=str(data.get("reason", ""))[:100],
        )

    def _save(self, signal: Signal, decision: AdvisoryDecision, strategy_reason: Optional[str]) -> None:
        ts_ms = str(int(time.time() * 1000))
        today = datetime.now(_KST).strftime("%Y%m%d")

        # 추천 상세 HASH
        key = f"ai:advice:{signal.market}:{signal.signal_id}"
        self.redis.hset(key, mapping={
            "ts_ms": ts_ms,
            "recommend": decision.recommend,
            "confidence": str(decision.confidence),
            "reason": decision.reason,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "strategy_reason": strategy_reason or "",
            "model": self.model,
            "provider": "anthropic",
        })
        self.redis.expire(key, _ADVICE_TTL)

        # ZSET index (일별, score=ts_ms)
        idx_key = f"ai:advice_index:{signal.market}:{today}"
        self.redis.zadd(idx_key, {signal.signal_id: int(ts_ms)})
        self.redis.expire(idx_key, _ADVICE_TTL)

        # 통계 카운터 (일별 recommend별)
        stats_key = f"ai:advice_stats:{signal.market}:{today}"
        self.redis.hincrby(stats_key, decision.recommend, 1)
        self.redis.expire(stats_key, _ADVICE_TTL)

    def advise(self, signal: Signal, strategy_reason: Optional[str] = None) -> AdvisoryDecision:
        """
        신호에 대한 AI 추천.
        실패해도 ERROR decision 반환, 예외 전파 없음.
        """
        try:
            client = self._get_client()
            prompt = self._build_prompt(signal, strategy_reason)
            response = client.messages.create(
                model=self.model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            decision = self._parse_response(response.content[0].text)
        except Exception as e:
            decision = AdvisoryDecision(
                recommend="ERROR",
                confidence=0.0,
                reason=f"{type(e).__name__}",
            )

        try:
            self._save(signal, decision, strategy_reason)
        except Exception:
            pass  # 저장 실패해도 결과 반환

        return decision
