"""Qwen(Ollama) 기반 뉴스 분류/요약."""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

from .models import NewsItem

_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
_QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen2.5:7b")
_CLASSIFY_TIMEOUT = int(os.getenv("NEWS_CLASSIFY_TIMEOUT", "25"))

_PROMPT_KR = """\
당신은 주식 시장 뉴스 분류기입니다. 아래 뉴스를 주식 트레이딩 관점에서 분류하세요.

제목: {title}
내용: {excerpt}

JSON만 응답 (마크다운 없이):
{{"relevant": true/false, "sentiment": "positive"/"negative"/"neutral", "impact": "high"/"medium"/"low", "category": "earnings"/"macro"/"geopolitical"/"sector"/"regulatory"/"other", "summary": "<40자 한국어 요약>"}}"""

_PROMPT_EN = """\
You are a stock market news analyst. Classify this news item for trading relevance.

Title: {title}
Excerpt: {excerpt}

Respond with JSON only (no markdown):
{{"relevant": true/false, "sentiment": "positive"/"negative"/"neutral", "impact": "high"/"medium"/"low", "category": "earnings"/"macro"/"geopolitical"/"sector"/"regulatory"/"other", "summary": "<40 chars>"}}"""


def _call_qwen(prompt: str) -> str:
    payload = json.dumps({
        "model": _QWEN_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 120},
    }).encode()

    req = urllib.request.Request(
        f"{_OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_CLASSIFY_TIMEOUT) as resp:
        body = json.loads(resp.read().decode())
        return body.get("response", "")


def _parse_qwen_response(raw: str) -> dict:
    clean = raw.strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1:
        return {}
    return json.loads(clean[start:end + 1])


def classify_item(item: NewsItem) -> NewsItem:
    """Qwen으로 단일 뉴스 분류. 실패 시 기본값 유지."""
    template = _PROMPT_KR if item.market == "KR" else _PROMPT_EN
    prompt = template.format(
        title=item.title[:250],
        excerpt=item.excerpt[:350] if item.excerpt else "(내용 없음)" if item.market == "KR" else "(no excerpt)",
    )
    try:
        raw = _call_qwen(prompt)
        data = _parse_qwen_response(raw)
        if not data:
            return item

        item.relevant = bool(data.get("relevant", True))
        sentiment = data.get("sentiment", "neutral")
        item.sentiment = sentiment if sentiment in ("positive", "negative", "neutral") else "neutral"
        impact = data.get("impact", "medium")
        item.impact = impact if impact in ("high", "medium", "low") else "medium"
        category = data.get("category", "other")
        item.category = category if category in (
            "earnings", "macro", "geopolitical", "sector", "regulatory", "other"
        ) else "other"
        item.ai_summary = str(data.get("summary", ""))[:100]
        item.classified = True
    except (urllib.error.URLError, OSError):
        # Qwen/Ollama 미기동 시 무시 (분류 없이 저장)
        pass
    except Exception:
        pass
    return item


def classify_batch(items: list[NewsItem], enabled: bool = True) -> list[NewsItem]:
    """뉴스 목록 일괄 분류. enabled=False면 분류 건너뜀."""
    if not enabled:
        return items
    return [classify_item(item) for item in items]
