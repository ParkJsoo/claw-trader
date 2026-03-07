"""뉴스 Redis 저장 — dedup + TTL 관리."""
from __future__ import annotations

import json
import time

from .collector import url_hash
from .models import NewsItem

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
    key = _seen_key(item.market, today)
    h = url_hash(item.url)
    return bool(r.sismember(key, h))


def mark_seen(r, item: NewsItem, today: str) -> None:
    key = _seen_key(item.market, today)
    h = url_hash(item.url)
    r.sadd(key, h)
    r.expire(key, _SEEN_TTL)


def write_item(r, item: NewsItem, today: str) -> None:
    """뉴스 아이템 Redis 저장 (dedup 포함)."""
    if is_seen(r, item, today):
        return

    payload = json.dumps(item.to_dict())

    # 1. 전체 raw 목록
    raw_key = f"news:raw:{item.market}:{today}"
    r.lpush(raw_key, payload)
    r.ltrim(raw_key, 0, _RAW_MAX - 1)
    r.expire(raw_key, _RAW_TTL)

    # 2. relevant=false 면 여기서 종료
    if not item.relevant:
        mark_seen(r, item, today)
        return

    # 3. 종목별 저장
    if item.symbols:
        for symbol in item.symbols:
            sym_key = f"news:symbol:{item.market}:{symbol}:{today}"
            r.lpush(sym_key, payload)
            r.ltrim(sym_key, 0, _SYMBOL_MAX - 1)
            r.expire(sym_key, _SYMBOL_TTL)
    else:
        # 종목 없으면 매크로
        macro_key = f"news:macro:{item.market}:{today}"
        r.lpush(macro_key, payload)
        r.ltrim(macro_key, 0, _MACRO_MAX - 1)
        r.expire(macro_key, _MACRO_TTL)

    # 4. 통계
    stats_key = f"news:stats:{item.market}:{today}"
    r.hincrby(stats_key, f"impact_{item.impact}", 1)
    r.hincrby(stats_key, f"sent_{item.sentiment}", 1)
    r.hincrby(stats_key, f"src_{item.source}", 1)
    r.hincrby(stats_key, "total", 1)
    r.expire(stats_key, _STATS_TTL)

    mark_seen(r, item, today)


def write_batch(r, items: list[NewsItem], today: str) -> tuple[int, int]:
    """일괄 저장. (저장 건수, 스킵 건수) 반환."""
    saved = skipped = 0
    for item in items:
        if is_seen(r, item, today):
            skipped += 1
        else:
            write_item(r, item, today)
            saved += 1
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
            sentiment = d.get("sentiment", "neutral")
            impact = d.get("impact", "medium")
            summary = d.get("ai_summary") or d.get("title", "")[:60]
            lines.append(f"[{impact.upper()}][{sentiment}] {summary}")
        except Exception:
            pass

    # 매크로 뉴스 (최대 3건)
    macro_items = r.lrange(macro_key, 0, 2)
    for raw in macro_items:
        try:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            summary = d.get("ai_summary") or d.get("title", "")[:60]
            lines.append(f"[MACRO] {summary}")
        except Exception:
            pass

    return "\n".join(lines) if lines else ""
