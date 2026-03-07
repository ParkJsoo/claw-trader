"""Qwen(Ollama) 판단 Provider — urllib 전용, 외부 의존성 없음."""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any

from .base import DecisionProvider, DecisionResult, build_dual_prompt, parse_decision_response

_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
_QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen2.5:7b")
_QWEN_TIMEOUT = int(os.getenv("QWEN_TIMEOUT", "30"))
_QWEN_TEMPERATURE = float(os.getenv("QWEN_TEMPERATURE", "0.1"))
_QWEN_MAX_RETRIES = 1


class QwenProvider(DecisionProvider):
    def __init__(self) -> None:
        self.model = _QWEN_MODEL

    def _call_ollama(self, prompt: str) -> str:
        """Ollama /api/generate 호출 → response text 반환."""
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": _QWEN_TEMPERATURE,
                "num_predict": 128,
            },
        }).encode()

        req = urllib.request.Request(
            f"{_OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_QWEN_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
            return body.get("response", "")

    def evaluate(self, market: str, symbol: str, features: dict[str, Any]) -> DecisionResult:
        prompt = build_dual_prompt(market, symbol, features)
        raw = ""
        for attempt in range(_QWEN_MAX_RETRIES + 1):
            try:
                raw = self._call_ollama(prompt)
                emit, direction, confidence, reason = parse_decision_response(raw)
                return DecisionResult(
                    emit=emit,
                    direction=direction,
                    confidence=confidence,
                    reason=reason,
                    model=self.model,
                    raw_response=raw,
                )
            except urllib.error.URLError as e:
                if attempt < _QWEN_MAX_RETRIES:
                    time.sleep(2.0)
                    continue
                return DecisionResult(
                    emit=False, direction="HOLD", confidence=0.0,
                    reason="", model=self.model,
                    raw_response=raw, error=f"URLError:{e}",
                )
            except Exception as e:
                return DecisionResult(
                    emit=False, direction="HOLD", confidence=0.0,
                    reason="", model=self.model,
                    raw_response=raw, error=f"{type(e).__name__}:{e}",
                )
        return DecisionResult(
            emit=False, direction="HOLD", confidence=0.0,
            reason="", model=self.model, error="max_retries_exceeded",
        )
