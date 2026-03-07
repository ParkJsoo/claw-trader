"""Claude(Anthropic) 판단 Provider."""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .base import DecisionProvider, DecisionResult, build_dual_prompt

_OVERLOADED_MAX_RETRIES = 2
_OVERLOADED_BACKOFF_SEC = 1.0


class ClaudeProvider(DecisionProvider):
    def __init__(self) -> None:
        self.model = os.getenv("AI_MODEL", "claude-haiku-4-5-20251001")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic()
        return self._client

    def _parse(self, text: str) -> tuple[bool, str, float, str]:
        """Returns (emit, direction, confidence, reason)."""
        clean = text.strip()
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end + 1]
        data = json.loads(clean)

        emit = bool(data.get("emit", False))
        direction = data.get("direction", "HOLD")
        if direction not in ("LONG", "EXIT", "HOLD"):
            direction = "HOLD"
            emit = False

        try:
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        reason = str(data.get("reason", ""))[:100]
        return emit, direction, confidence, reason

    def evaluate(self, market: str, symbol: str, features: dict[str, Any]) -> DecisionResult:
        prompt = build_dual_prompt(market, symbol, features)
        raw = ""
        for attempt in range(_OVERLOADED_MAX_RETRIES + 1):
            try:
                client = self._get_client()
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = resp.content[0].text
                emit, direction, confidence, reason = self._parse(raw)
                return DecisionResult(
                    emit=emit,
                    direction=direction,
                    confidence=confidence,
                    reason=reason,
                    model=self.model,
                    raw_response=raw,
                )
            except Exception as e:
                err_name = type(e).__name__
                if err_name == "OverloadedError" and attempt < _OVERLOADED_MAX_RETRIES:
                    wait = _OVERLOADED_BACKOFF_SEC * (2 ** attempt)
                    time.sleep(wait)
                    continue
                return DecisionResult(
                    emit=False, direction="HOLD", confidence=0.0,
                    reason="", model=self.model,
                    raw_response=raw, error=f"{err_name}:{e}",
                )
        return DecisionResult(
            emit=False, direction="HOLD", confidence=0.0,
            reason="", model=self.model, error="max_retries_exceeded",
        )
