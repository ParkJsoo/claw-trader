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


def build_type_b_prompt(symbol: str, change_rate: float, trade_price: float,
                        high_price: float, ret_5m: float, volume_krw: float) -> str:
    """Type B (추세 탑승) Claude 프롬프트 — 일간 서서히 오르는 추세 평가."""
    near_high_pct = (trade_price - high_price) / high_price * 100  # 음수
    lines = [
        "You are a cash-only short-term momentum trading signal evaluator.",
        "Strategy: TREND RIDING — this coin has been gradually rising all day, NOT a flash pump.",
        "",
        "Market: COIN (Upbit KRW crypto market, 24/7 trading)",
        f"Symbol: {symbol}",
        f"Daily change vs yesterday: {change_rate*100:+.2f}%",
        f"Current price: {trade_price}",
        f"Today's high: {high_price}  (current is {near_high_pct:.1f}% from today's high)",
        f"5-min return right now: {ret_5m*100:+.3f}%",
        f"24h volume (KRW): {volume_krw/1e8:.0f}억",
        "",
        "Context: This coin has been gradually rising all day (slow trend, not a spike).",
        "Your job: judge if the UPTREND has 2+ more hours of continuation potential.",
        "Emit LONG if: trend looks sustained, price near high (not exhausted), volume strong,",
        "and the move has room to continue — not yet overextended.",
        "Return HOLD if: trend looks exhausted, price has fallen significantly from high,",
        "volume fading, or the daily move is already too extended to chase.",
        "",
        "Hold time: 2-6 hours (Trend Riding). No news catalyst required — judge price action.",
        "confidence must be between 0.0 and 1.0.",
        "",
        "Respond with JSON only (no markdown, no extra text):",
        '{"emit": true|false, "direction": "LONG|HOLD", "confidence": 0.0-1.0, "reason": "<100 chars"}',
    ]
    return "\n".join(lines)


class DecisionProvider:
    """판단 Provider 추상 기반 클래스."""

    def evaluate(self, market: str, symbol: str, features: dict[str, Any]) -> "DecisionResult":
        raise NotImplementedError


def build_dual_prompt(market: str, symbol: str, features: dict[str, Any]) -> str:
    """Claude 공통 프롬프트 — momentum breakout catalyst quality filter."""

    def fmt(v: Optional[float]) -> str:
        return f"{v:.4f}" if v is not None else "N/A"

    if market == "KR":
        market_ctx = (
            "Market: KR (KOSPI/KOSDAQ, Korean Won, session 09:00-15:30 KST)\n"
            "This stock is surging right now (strong 5-min positive return + high volume).\n"
            "Your job: judge if this is REAL buying pressure (positive catalyst, breakout, "
            "institutional accumulation, sector rotation into this stock) "
            "OR a short-lived spike with no substance (retail panic buying, no catalyst, "
            "likely to reverse immediately).\n"
            "Emit LONG if there is a credible reason for momentum to continue (news catalyst, "
            "technical breakout above resistance, strong sector tailwind, earnings beat, etc.). "
            "Return HOLD if no catalyst found OR if signals suggest pump-and-dump / FOMO spike."
        )
    elif market == "COIN":
        market_ctx = (
            "Market: COIN (Upbit KRW crypto market, 24/7 trading)\n"
            "This cryptocurrency is surging right now (positive 5-min return + volume spike).\n"
            "Your job: judge if this is GENUINE momentum with staying power "
            "OR a fake pump that will reverse quickly.\n"
            "This is a Big Mover Ride strategy — hold time is 1-4 hours. "
            "Only emit LONG for moves likely to sustain +5% or more over the next 1-2 hours.\n"
            "Emit LONG only when you see clear evidence of sustained momentum: "
            "volume increasing (not just a spike), 1-min return still positive and accelerating, "
            "price action suggesting continuation (higher lows, not exhaustion candles), "
            "and the move has room to run (not already extended/parabolic).\n"
            "Return HOLD if: 1-min is reversing after the 5-min surge, volume is fading, "
            "the move looks like a short pump-and-dump, or the price action looks exhausted. "
            "Do NOT require news catalyst — crypto momentum is often catalyst-free. "
            "Be selective: only the strongest, cleanest momentum setups qualify."
        )
    else:
        market_ctx = (
            "Market: US (NYSE/NASDAQ, USD, session 09:30-16:00 ET)\n"
            "This stock is surging. Emit LONG if credible positive catalyst exists and momentum "
            "is likely to continue. Return HOLD if no catalyst or looks like a short-squeeze / "
            "pump with no fundamentals."
        )

    # 뉴스 컨텍스트 (있으면 추가)
    news_summary = features.get("news_summary", "")

    lines = [
        "You are a cash-only short-term momentum trading signal evaluator.",
        "Strategy: momentum breakout — ride strong surges with real catalysts, avoid fake pumps.",
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
        "Constraints: cash-only, no short selling. Hold time: 1-4 hours (Big Mover Ride).",
        "emit=true → LONG (real catalyst detected, momentum likely continues)",
        "emit=false → HOLD (no catalyst / pump-dump risk / ambiguous)",
        "confidence must be between 0.0 and 1.0.",
        "",
        "Respond with JSON only (no markdown, no extra text):",
        '{"emit": true|false, "direction": "LONG|HOLD", "confidence": 0.0-1.0, "reason": "<100 chars"}',
    ])
    return "\n".join(lines)
