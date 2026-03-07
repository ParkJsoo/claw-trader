"""Provider 추상화 — 듀얼런 공통 인터페이스."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class DecisionResult:
    emit: bool
    direction: str        # LONG / EXIT / HOLD
    confidence: float     # 0.0~1.0
    reason: str
    model: str
    raw_response: str = ""
    error: str = ""       # 비어있으면 정상, non-empty면 평가 실패


class DecisionProvider:
    """판단 Provider 추상 기반 클래스."""

    def evaluate(self, market: str, symbol: str, features: dict[str, Any]) -> "DecisionResult":
        raise NotImplementedError


def build_dual_prompt(market: str, symbol: str, features: dict[str, Any]) -> str:
    """Claude/Qwen 공통 프롬프트 (동일 입력, 동일 출력 포맷)."""

    def fmt(v: Optional[float]) -> str:
        return f"{v:.4f}" if v is not None else "N/A"

    if market == "KR":
        market_ctx = (
            "Market: KR (KOSPI/KOSDAQ, Korean Won, session 09:00-15:30 KST)\n"
            "Signal on clear 1-5min momentum with range_5m > 0.002."
        )
    else:
        market_ctx = (
            "Market: US (NYSE/NASDAQ, USD, session 09:30-16:00 ET)\n"
            "Signal on clear 1-5min momentum with range_5m > 0.003."
        )

    lines = [
        "You are a cash-only equity trading signal evaluator.",
        "Decide whether to emit a trading signal based on recent price momentum.",
        "",
        market_ctx,
        f"Symbol: {symbol}",
        f"Current price: {features['current_price']}",
        f"1-min return: {fmt(features['ret_1m'])}",
        f"5-min return: {fmt(features['ret_5m'])}",
        f"5-min range: {fmt(features['range_5m'])}",
        "",
        "Constraints: cash-only, no short selling.",
        "direction must be LONG (enter), EXIT (close position), or HOLD (do nothing).",
        "confidence must be between 0.0 and 1.0.",
        "",
        "Respond with JSON only (no markdown, no extra text):",
        '{"emit": true|false, "direction": "LONG|EXIT|HOLD", "confidence": 0.0-1.0, "reason": "<100 chars"}',
    ]
    return "\n".join(lines)
