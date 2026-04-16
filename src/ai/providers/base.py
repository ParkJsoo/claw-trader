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
                        high_price: float, ret_5m: float, volume_krw: float,
                        ob_ratio: float = None) -> str:
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
    ]
    if ob_ratio is not None:
        lines.append(f"Orderbook pressure: {ob_ratio:.2f} (bid/ask ratio — >1.2 strong buy pressure, <0.8 strong sell pressure)")
    lines.extend([
        "confidence must be between 0.0 and 1.0.",
        "",
        "Respond with JSON only (no markdown, no extra text):",
        '{"emit": true|false, "direction": "LONG|HOLD", "confidence": 0.0-1.0, "reason": "<100 chars"}',
    ])
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
            "\n"
            "Strategy: momentum breakout — ride the start of big moves (+5~10% target).\n"
            "Risk management in play:\n"
            "  - Hard stop: -2.5% from entry (limits downside)\n"
            "  - Trailing stop: -2.0% from high water mark\n"
            "  - At +3% gain, trailing stop activates (trail-only mode, protect profits)\n"
            "  - Hold time: 15-60 minutes\n"
            "\n"
            "Your role: BAD NEWS FILTER — block entries only if you detect a SPECIFIC risk.\n"
            "Default bias: LONG. Do NOT require news catalyst to emit LONG.\n"
            "\n"
            "Emit LONG if:\n"
            "  - 1-min return is positive (momentum still active, not reversing)\n"
            "  - Volume is surging (genuine buying pressure, not thin air)\n"
            "  - Sector/theme rotation is in play — Korean market frequently moves by theme\n"
            "    (defense, secondary batteries, semiconductors, AI, bio). Sector-wide moves\n"
            "    are VALID entries even without stock-specific news.\n"
            "  - This looks like the START of a move (not already exhausted)\n"
            "\n"
            "Return HOLD ONLY if you detect a specific risk:\n"
            "  - 1-min return is NEGATIVE after the 5-min surge (momentum reversing = late entry)\n"
            "  - Negative news for this stock (fraud, delisting, earnings miss, scandal)\n"
            "  - Price already up +10% or more today (bulk of move is behind us)\n"
            "  - Clear exhaustion: 1-min and 5-min returns nearly identical (no acceleration)\n"
            "  - Known pump-and-dump pattern for this symbol\n"
            "\n"
            "No stock-specific news catalyst required. Sector and macro-driven moves are valid.\n"
            "When momentum is positive and no specific risk detected, emit LONG."
        )
    elif market == "COIN":
        market_ctx = (
            "Market: COIN (Upbit KRW crypto market, 24/7 trading)\n"
            "This cryptocurrency is surging right now (positive 5-min return + volume spike).\n"
            "CORE QUESTION: Is this a BIG MOVER with +10% or more upside potential in the next 1-4 hours?\n"
            "We only want to ride large moves (+10% to +30%). Small/flat moves lose money.\n"
            "\n"
            "Risk management in play (so judge accordingly):\n"
            "  - Hard stop: -3% from entry (tight; reject trades that risk immediate -3%)\n"
            "  - Trailing stop: -4% from high water mark (-3% once +5% is reached)\n"
            "  - Hold time: 1-4 hours — NOT a scalp, NOT a swing\n"
            "  - We need the move to run at LEAST +5% (trail catches +1% profit) to break even\n"
            "\n"
            "Emit LONG ONLY if:\n"
            "  - Clear evidence this is the START of a big move, not the END of one\n"
            "  - Volume accelerating (not just spiking once), 1-min return still positive\n"
            "  - Higher lows on short-timeframe price action (not exhaustion candles)\n"
            "  - Room to run another +5% to +15% — NOT already parabolic/extended\n"
            "  - Orderbook shows buy-side pressure (ob_ratio > 1.2 preferred, > 1.5 very strong)\n"
            "\n"
            "Return HOLD if:\n"
            "  - 1-min is reversing after the 5-min surge (late entry)\n"
            "  - Volume fading right after the initial spike\n"
            "  - Price action looks exhausted or this is clearly a pump-and-dump\n"
            "  - Move is already too extended — the bulk of the run is behind us\n"
            "  - Ambiguous / no clear continuation signal — when in doubt, HOLD\n"
            "\n"
            "No news catalyst required — crypto momentum is often catalyst-free.\n"
            "BE VERY SELECTIVE: only the clearest, earliest big-mover setups qualify."
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
        "Strategy: momentum breakout — ride strong surges early, exit with trailing stop.",
        "",
        market_ctx,
        f"Symbol: {symbol}",
        f"Current price: {features['current_price']}",
        f"1-min return: {fmt(features['ret_1m'])}",
        f"5-min return: {fmt(features['ret_5m'])}",
        f"5-min range: {fmt(features['range_5m'])}",
    ]
    if features.get("market_time"):
        lines.append(f"Market time: {features['market_time']}")

    # 오더북 데이터 (있으면 추가)
    ob_ratio = features.get("ob_ratio")
    if ob_ratio is not None:
        try:
            ob_val = float(ob_ratio)
            lines.append(f"Orderbook pressure: {ob_val:.2f} (bid/ask ratio — >1.2 strong buy pressure, <0.8 strong sell pressure)")
        except (TypeError, ValueError):
            pass

    if news_summary:
        lines.append("")
        lines.append("최근 뉴스:")
        for news_line in news_summary.split("\n"):
            if news_line.strip():
                lines.append(f"- {news_line.strip()}")

    if market == "KR":
        footer_hold_time = "Hold time: 15-60 minutes."
        footer_emit_false = "emit=false → HOLD (reversal risk / negative news / exhaustion detected)"
    else:
        footer_hold_time = "Hold time: 1-4 hours (Big Mover Ride)."
        footer_emit_false = "emit=false → HOLD (pump-dump risk / ambiguous / no continuation)"

    lines.extend([
        "",
        f"Constraints: cash-only, no short selling. {footer_hold_time}",
        "emit=true → LONG (momentum likely continues, no specific risk detected)",
        footer_emit_false,
        "confidence must be between 0.0 and 1.0.",
        "",
        "Respond with JSON only (no markdown, no extra text):",
        '{"emit": true|false, "direction": "LONG|HOLD", "confidence": 0.0-1.0, "reason": "<100 chars"}',
    ])
    return "\n".join(lines)
