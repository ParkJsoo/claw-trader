"""Qwen(Ollama) 기반 뉴스 분류/요약."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import NewsItem

logger = logging.getLogger(__name__)

_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
_QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen2.5:7b")
_CLASSIFY_TIMEOUT = int(os.getenv("NEWS_CLASSIFY_TIMEOUT", "25"))
_CLASSIFY_WORKERS = int(os.getenv("NEWS_CLASSIFY_WORKERS", "4"))

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# ai_summary allowlist: 한글, 영문, 숫자, 공백, 기본 문장부호만 허용
_SUMMARY_ALLOWLIST_RE = re.compile(r"[^\uAC00-\uD7AF\u3130-\u318F\w\s\.,\-!?%()]")

# 프롬프트 인젝션 패턴: 지시 덮어쓰기 시도 감지
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(previous|above|all)|forget\s+instructions?|"
    r"new\s+instruction|disregard|override|system\s*prompt|"
    r"json만\s*응답|위\s*내용\s*무시|지시\s*무시)",
    re.IGNORECASE,
)


def _sanitize_input(text: str) -> str:
    """프롬프트 인젝션 가능성 있는 라인 제거 후 반환."""
    lines = text.splitlines()
    clean = [ln for ln in lines if not _INJECTION_PATTERNS.search(ln)]
    return " ".join(clean).strip()


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
    title_clean = _sanitize_input(item.title[:250])
    excerpt_raw = item.excerpt[:350] if item.excerpt else ("(내용 없음)" if item.market == "KR" else "(no excerpt)")
    excerpt_clean = _sanitize_input(excerpt_raw)
    prompt = template.format(title=title_clean, excerpt=excerpt_clean)
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
        raw_summary = str(data.get("summary", ""))[:100]
        cleaned = _CONTROL_CHARS_RE.sub("", raw_summary)
        item.ai_summary = _SUMMARY_ALLOWLIST_RE.sub("", cleaned)
        item.classified = True
    except (urllib.error.URLError, OSError):
        # Qwen/Ollama 미기동 시 무시 (분류 없이 저장)
        pass
    except json.JSONDecodeError as e:
        logger.debug("Qwen response JSON parse error: %s", e)
    except Exception as e:
        logger.warning("classify_item unexpected error title=%r: %s", item.title[:50], e)
    return item


def classify_batch(items: list[NewsItem], enabled: bool = True) -> list[NewsItem]:
    """뉴스 목록 병렬 분류. enabled=False면 분류 건너뜀.

    이미 classified=True인 아이템(IFE 등 사전 분류)은 Qwen 전달 없이 통과.
    """
    if not enabled or not items:
        return items

    already = [i for i in items if getattr(i, "classified", False)]
    to_classify = [i for i in items if not getattr(i, "classified", False)]

    logger.info(
        "news: classify_start items=%d to_classify=%d",
        len(items),
        len(to_classify),
    )
    print(
        f"news: classify_start items={len(items)} to_classify={len(to_classify)}",
        flush=True,
    )

    if not to_classify:
        return items

    classified_results: list[NewsItem] = [None] * len(to_classify)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=_CLASSIFY_WORKERS) as executor:
        future_to_idx = {executor.submit(classify_item, item): idx for idx, item in enumerate(to_classify)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                classified_results[idx] = future.result()
            except Exception as e:
                logger.warning("classify_batch worker error idx=%d: %s", idx, e)
                classified_results[idx] = to_classify[idx]  # 원본 유지

    return already + classified_results
