"""Provider 추상화 — 듀얼런 공통 인터페이스."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class DecisionResult:
    emit: bool
    direction: str        # LONG / EXIT / HOLD
    confidence: float     # 0.0~1.0
    reason: str
    model: str
    raw_response: str = ""
    error: str = ""       # 비어있으면 정상, non-empty면 평가 실패


def parse_decision_response(text: str) -> tuple[bool, str, float, str]:
    """AI 응답 JSON 파싱 → (emit, direction, confidence, reason).

    Claude/Qwen 공통 파서. JSONDecodeError 시 상위에서 처리.
    """
    clean = text.strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start != -1 and end != -1:
        clean = clean[start:end + 1]

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        logger.debug("parse_decision_response JSON error: %s text=%r", e, clean[:200])
        raise

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


class DecisionProvider:
    """판단 Provider 추상 기반 클래스."""

    def evaluate(self, market: str, symbol: str, features: dict[str, Any]) -> "DecisionResult":
        raise NotImplementedError


def build_dual_prompt(market: str, symbol: str, features: dict[str, Any]) -> str:
    """Claude/Qwen 공통 프롬프트 — mean reversion bad-news filter."""

    def fmt(v: Optional[float]) -> str:
        return f"{v:.4f}" if v is not None else "N/A"

    if market == "KR":
        market_ctx = (
            "Market: KR (KOSPI/KOSDAQ, Korean Won, session 09:00-15:30 KST)\n"
            "This stock has dropped recently. Your job is to detect BAD NEWS only.\n"
            "Emit LONG if the drop appears technical/temporary (profit-taking, sector rotation, "
            "no fundamental problem). "
            "Return HOLD if there is negative news that could extend the drop "
            "(earnings miss, scandal, regulatory action, credit risk, delisting risk, etc.)."
        )
    else:
        market_ctx = (
            "Market: US (NYSE/NASDAQ, USD, session 09:30-16:00 ET)\n"
            "This stock has dropped recently. Emit LONG if no bad fundamental news. "
            "Return HOLD if negative catalyst detected."
        )

    # 뉴스 컨텍스트 (있으면 추가)
    news_summary = features.get("news_summary", "")

    lines = [
        "You are a cash-only equity trading signal evaluator.",
        "Strategy: mean reversion — buy temporary dips, avoid fundamental deterioration.",
        "",
        market_ctx,
        f"Symbol: {symbol}",
        f"Current price: {features['current_price']}",
        f"1-min return: {fmt(features['ret_1m'])}",
        f"5-min return: {fmt(features['ret_5m'])}",
        f"5-min range: {fmt(features['range_5m'])}",
    ]

    if news_summary:
        lines.append("")
        lines.append("최근 뉴스:")
        for news_line in news_summary.split("\n"):
            if news_line.strip():
                lines.append(f"- {news_line.strip()}")

    lines.extend([
        "",
        "Constraints: cash-only, no short selling.",
        "emit=true → LONG (drop is temporary, safe to enter for recovery)",
        "emit=false → HOLD (bad news detected, avoid entry)",
        "confidence must be between 0.0 and 1.0.",
        "",
        "Respond with JSON only (no markdown, no extra text):",
        '{"emit": true|false, "direction": "LONG|HOLD", "confidence": 0.0-1.0, "reason": "<100 chars"}',
    ])
    return "\n".join(lines)
