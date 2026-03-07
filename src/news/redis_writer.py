"""뉴스 Redis 저장 — dedup + TTL 관리."""
from __future__ import annotations

import json
import logging
import re

from .collector import url_hash
from .models import NewsItem

logger = logging.getLogger(__name__)

# Claude 프롬프트에 삽입되는 summary 필터: 제어문자 → allowlist
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SUMMARY_SAFE_RE = re.compile(r"[^\uAC00-\uD7AF\u3130-\u318F\w\s\.,\-!?%()]")


def _safe_summary(text: str) -> str:
    """제어문자 제거 후 허용 문자만 남김."""
    text = _CONTROL_CHARS_RE.sub("", text)
    return _SUMMARY_SAFE_RE.sub("", text)[:80]


_RAW_TTL = 2 * 86400       # 2일
_SYMBOL_TTL = 1 * 86400    # 1일
_MACRO_TTL = 1 * 86400     # 1일
_SEEN_TTL = 2 * 86400      # 2일
_STATS_TTL = 7 * 86400     # 7일

_RAW_MAX = 200
_SYMBOL_MAX = 50
_MACRO_MAX = 50


def _seen_key(market: str, today: str) -> str:
    return f"news:seen:{market}:{today}"


def is_seen(r, item: NewsItem, today: str) -> bool:
    """URL 해시 또는 content_hash 중 하나라도 seen이면 중복."""
    key = _seen_key(item.market, today)
    url_h = url_hash(item.url)
    return bool(r.sismember(key, url_h) or r.sismember(key, item.content_hash))


def mark_seen(r, item: NewsItem, today: str) -> None:
    """URL 해시와 content_hash 둘 다 seen으로 등록."""
    key = _seen_key(item.market, today)
    url_h = url_hash(item.url)
    r.sadd(key, url_h, item.content_hash)
    r.expire(key, _SEEN_TTL)


def write_item(r, item: NewsItem, today: str) -> bool:
    """뉴스 아이템 Redis 저장 (dedup 포함). 저장됐으면 True, 스킵이면 False."""
    if is_seen(r, item, today):
        return False

    payload = json.dumps(item.to_dict())

    # 1. 전체 raw 목록
    raw_key = f"news:raw:{item.market}:{today}"
    r.lpush(raw_key, payload)
    r.ltrim(raw_key, 0, _RAW_MAX - 1)
    r.expire(raw_key, _RAW_TTL)

    # 2. relevant=false면 dedup만 등록하고 종료
    if not item.relevant:
        mark_seen(r, item, today)
        return True

    # 3. scope에 따라 종목별 or 매크로 저장
    if item.scope == "symbol" and item.symbols:
        for symbol in item.symbols:
            sym_key = f"news:symbol:{item.market}:{symbol}:{today}"
            r.lpush(sym_key, payload)
            r.ltrim(sym_key, 0, _SYMBOL_MAX - 1)
            r.expire(sym_key, _SYMBOL_TTL)
    else:
        macro_key = f"news:macro:{item.market}:{today}"
        r.lpush(macro_key, payload)
        r.ltrim(macro_key, 0, _MACRO_MAX - 1)
        r.expire(macro_key, _MACRO_TTL)

    # 4. 통계
    stats_key = f"news:stats:{item.market}:{today}"
    r.hincrby(stats_key, f"impact_{item.impact}", 1)
    r.hincrby(stats_key, f"sent_{item.sentiment}", 1)
    r.hincrby(stats_key, f"src_{item.source}", 1)
    r.hincrby(stats_key, f"scope_{item.scope}", 1)
    r.hincrby(stats_key, "total", 1)
    r.expire(stats_key, _STATS_TTL)

    mark_seen(r, item, today)
    return True


def write_batch(r, items: list[NewsItem], today: str) -> tuple[int, int]:
    """일괄 저장. (저장 건수, 스킵 건수) 반환."""
    saved = skipped = 0
    for item in items:
        if write_item(r, item, today):
            saved += 1
        else:
            skipped += 1
    return saved, skipped


def get_symbol_context(r, market: str, symbol: str, today: str, max_items: int = 5) -> str:
    """Claude 프롬프트용 종목별 뉴스 컨텍스트 문자열 반환."""
    sym_key = f"news:symbol:{market}:{symbol}:{today}"
    macro_key = f"news:macro:{market}:{today}"

    lines: list[str] = []

    # 종목별 뉴스
    raw_items = r.lrange(sym_key, 0, max_items - 1)
    for raw in raw_items:
        try:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            impact = d.get("impact", "medium").upper()
            sentiment = d.get("sentiment", "neutral")
            summary = _safe_summary(d.get("ai_summary") or d.get("title", "")[:60])
            source = d.get("source", "")
            lines.append(f"[SYMBOL][{impact}][{sentiment}][{source}] {summary}")
        except Exception as e:
            logger.debug("get_symbol_context parse error: %s", e)

    # 매크로 뉴스 (최대 3건)
    macro_items = r.lrange(macro_key, 0, 2)
    for raw in macro_items:
        try:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            impact = d.get("impact", "medium").upper()
            summary = _safe_summary(d.get("ai_summary") or d.get("title", "")[:60])
            source = d.get("source", "")
            lines.append(f"[MACRO][{impact}][{source}] {summary}")
        except Exception as e:
            logger.debug("get_symbol_context macro parse error: %s", e)

    return "\n".join(lines) if lines else ""
