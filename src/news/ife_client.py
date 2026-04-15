"""investing-for-everyone.com (IFE) 데이터 클라이언트.

HTML 파싱으로 최근 변경사건 + 종목 위키 리포트 수집.
robots.txt: Allow: / (HTML 파싱 허용), Disallow: /api/ (API 직접 호출 금지)
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .models import NewsItem

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_BASE_URL = "https://investing-for-everyone.com"
_TIMEOUT = 10
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_IFE_ENABLED = os.getenv("IFE_ENABLED", "true").lower() in ("true", "1", "yes")

# change_type → sentiment/impact 매핑
_POSITIVE_TYPES = {"급등", "상한가", "강세", "상승", "급상승"}
_NEGATIVE_TYPES = {"급락", "하락", "약세", "하한가", "급하락"}
_HIGH_IMPACT_TYPES = {"급등", "상한가", "급상승"}

# 브로커 리포트 패턴: "투자의견: XXX 적정가격: NNN원"
_REPORT_RE = re.compile(
    r"투자의견\s*:\s*(?P<rating>[^\s,]+)"
    r".*?적정가격\s*:\s*(?P<target>[0-9,]+)원",
    re.DOTALL,
)

# 카드 내 타임어고 패턴: "N분 전", "N시간 전", "N일 전"
_TIMEAGO_RE = re.compile(r"\d+\s*(?:분|시간|일|초)\s*전")


@dataclass
class IFEEvent:
    symbol_name: str          # 종목명 (wiki 링크에서 추출)
    reason: str               # 사유 텍스트
    themes: list[str]         # 테마 목록
    change_type: str          # 급등/상한가/강세/하락/약세 등
    date_str: str             # YYYY-MM-DD
    time_ago: str             # "24분 전" 등

    def to_news_item(self, symbol_code: str | None = None) -> NewsItem:
        """IFEEvent → NewsItem 변환 (이미 분류됨으로 표시)."""
        ct = self.change_type

        # sentiment
        if ct in _POSITIVE_TYPES:
            sentiment = "positive"
        elif ct in _NEGATIVE_TYPES:
            sentiment = "negative"
        else:
            sentiment = "neutral"

        # impact
        if ct in _HIGH_IMPACT_TYPES:
            impact = "high"
        elif sentiment == "positive":
            impact = "medium"
        else:
            impact = "medium"

        # scope / symbols
        symbols: list[str] = []
        scope = "macro"
        if symbol_code:
            symbols = [symbol_code]
            scope = "symbol"

        title = f"[IFE][{ct}] {self.symbol_name}: {self.reason[:80]}"
        excerpt = f"{self.reason} (테마: {', '.join(self.themes)})" if self.themes else self.reason
        url = f"{_BASE_URL}/wiki/{urllib.parse.quote(self.symbol_name)}"

        item = NewsItem(
            title=title[:300],
            url=url,
            source="ife_home",
            published_at=self.date_str,
            market="KR",
            excerpt=excerpt[:500],
            symbols=symbols,
            reliability=0.85,
            relevant=True,
            sentiment=sentiment,
            impact=impact,
            category="sector",
            ai_summary=f"[IFE] {self.symbol_name} {ct}",
            classified=True,
        )
        item.scope = scope
        return item


@dataclass
class IFEWiki:
    symbol_name: str
    themes: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)  # {broker, rating, target_price, date}


# ---------------------------------------------------------------------------
# HTML 파싱 유틸 (bs4 없이 정규식 기반)
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """HTML 태그 제거 후 공백 정리."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#x27;|&#39;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#[0-9]+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_text(html_fragment: str) -> str:
    return _strip_tags(html_fragment)


def _find_card_blocks(html: str) -> list[str]:
    """
    data-slot="badge" 자식이 "사건"인 카드 블록을 추출.
    실제 SSR 컨테이너: <div class="flex flex-wrap items-center gap-1.5 rounded-md neo-border bg-neo-bg/50 px-2 py-1.5 text-xs">
    """
    # 카드 div 시작 위치 수집 (neo-border + bg-neo-bg/50 + px-2 조합)
    card_start_re = re.compile(
        r'<div[^>]+neo-border[^>]+bg-neo-bg/50[^>]+px-2[^>]*>'
    )
    card_starts = [m.start() for m in card_start_re.finditer(html)]

    results: list[str] = []
    for i, start in enumerate(card_starts):
        end = card_starts[i + 1] if i + 1 < len(card_starts) else start + 1500
        chunk = html[start:min(end, start + 1500)]
        # "사건" 배지가 포함된 카드만 수집
        if "bg-neo-coral-light" in chunk and "사건" in chunk:
            results.append(chunk)

    return results


def _parse_card(card_html: str) -> IFEEvent | None:
    """카드 HTML에서 IFEEvent 추출."""
    try:
        # badge type
        badge_m = re.search(r'data-slot="badge"[^>]*>([^<]+)<', card_html)
        if not badge_m or "사건" not in badge_m.group(1):
            return None

        # 사유 텍스트: class에 font-semibold 포함한 span
        reason_m = re.search(
            r'<span[^>]*font-semibold[^>]*>([^<]+)</span>',
            card_html,
        )
        reason = _extract_text(reason_m.group(1)) if reason_m else ""
        if not reason:
            return None

        # 날짜: text-[0.625rem] span (또는 두 번째 span in flex-1 div)
        date_m = re.search(
            r'<span[^>]*text-\[0\.625rem\][^>]*>([^<]+)</span>',
            card_html,
        )
        date_str = _extract_text(date_m.group(1)) if date_m else ""

        # wiki 링크들
        wiki_links: list[str] = re.findall(r'href="/wiki/([^"]+)"', card_html)
        decoded_links = [urllib.parse.unquote(lk) for lk in wiki_links]

        # time_ago: "N분 전" / "N시간 전" 패턴
        timeago_m = _TIMEAGO_RE.search(card_html)
        time_ago = timeago_m.group(0) if timeago_m else ""

        # change_type: badge 다음 텍스트 또는 reason에서 추출
        change_type = _infer_change_type(reason)

        # symbol_name: 가장 짧은 링크 (보통 종목명이 테마보다 짧음)
        # 또는 reason에 포함된 링크
        symbol_name = ""
        themes: list[str] = []

        for lk in decoded_links:
            matched = False
            for ct in list(_POSITIVE_TYPES) + list(_NEGATIVE_TYPES):
                if ct in lk:
                    matched = True
                    break
            if matched:
                change_type = lk if lk in (list(_POSITIVE_TYPES) + list(_NEGATIVE_TYPES)) else change_type
            # 종목명 vs 테마 구분: 종목명에 "반도체", "2차전지" 같은 테마어 없으면 종목으로
            if _is_likely_theme(lk):
                themes.append(lk)
            else:
                if not symbol_name:
                    symbol_name = lk

        # symbol_name 폴백: reason 첫 단어 또는 첫 링크
        if not symbol_name and decoded_links:
            symbol_name = decoded_links[0]

        if not symbol_name:
            return None

        return IFEEvent(
            symbol_name=symbol_name,
            reason=reason,
            themes=themes,
            change_type=change_type,
            date_str=date_str,
            time_ago=time_ago,
        )
    except Exception as e:
        logger.debug("ife_client: _parse_card error: %s", e)
        return None


# 테마로 판단할 키워드
_THEME_KEYWORDS = {
    "반도체", "2차전지", "배터리", "AI", "인공지능", "바이오", "제약", "방산",
    "에너지", "화학", "금융", "은행", "증권", "철강", "자동차", "부동산",
    "IT", "소프트웨어", "게임", "엔터", "미디어", "유통", "식품", "화장품",
    "로봇", "우주", "항공", "조선", "건설", "통신", "전기", "의료",
}


def _is_likely_theme(name: str) -> bool:
    return any(kw in name for kw in _THEME_KEYWORDS) or len(name) > 6


def _infer_change_type(reason: str) -> str:
    """reason 텍스트에서 change_type 추론."""
    for ct in sorted(list(_POSITIVE_TYPES) + list(_NEGATIVE_TYPES), key=len, reverse=True):
        if ct in reason:
            return ct
    if "상승" in reason or "↑" in reason or "%↑" in reason:
        return "강세"
    if "하락" in reason or "약세" in reason or "↓" in reason or "%↓" in reason:
        return "약세"
    return "변동"


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def fetch_home_events(max_items: int = 60) -> list[IFEEvent]:
    """IFE 홈에서 최근 변경사건 카드 파싱."""
    if not _IFE_ENABLED:
        return []
    try:
        resp = requests.get(_BASE_URL, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text

        cards = _find_card_blocks(html)
        if not cards:
            logger.debug("ife_client: no card blocks found in home HTML")
            return []

        events: list[IFEEvent] = []
        for card_html in cards[:max_items]:
            ev = _parse_card(card_html)
            if ev:
                events.append(ev)

        logger.info("ife_client: fetched home events=%d", len(events))
        return events
    except Exception as e:
        logger.warning("ife_client: fetch_home_events error: %s", e)
        return []


def fetch_wiki(symbol_name: str) -> IFEWiki | None:
    """IFE 위키 페이지에서 오늘 날짜 사건/리포트 추출."""
    if not _IFE_ENABLED:
        return None
    try:
        encoded = urllib.parse.quote(symbol_name)
        url = f"{_BASE_URL}/wiki/{encoded}"
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        html = resp.text

        # 테마 추출: wiki 링크 중 테마 링크
        theme_links = re.findall(r'href="/wiki/([^"]+)"', html)
        themes = [urllib.parse.unquote(lk) for lk in theme_links if _is_likely_theme(urllib.parse.unquote(lk))]
        themes = list(dict.fromkeys(themes))[:10]  # 중복 제거

        # 리포트 파싱: "투자의견: XXX 적정가격: NNN원" 패턴
        today_str = datetime.now(_KST).strftime("%Y-%m-%d")
        reports: list[dict] = []
        text_body = _strip_tags(html)

        for m in _REPORT_RE.finditer(text_body):
            rating = m.group("rating").strip()
            target_price = m.group("target").replace(",", "")
            # 브로커명: 패턴 앞 50자에서 추출 시도
            prefix = text_body[max(0, m.start() - 80):m.start()]
            broker_m = re.search(r"([가-힣A-Za-z]{2,}(?:증권|투자|리서치|금융))", prefix)
            broker = broker_m.group(1) if broker_m else "알수없음"

            reports.append({
                "broker": broker,
                "rating": rating,
                "target_price": target_price,
                "date": today_str,
            })

        return IFEWiki(
            symbol_name=symbol_name,
            themes=themes,
            reports=reports,
        )
    except Exception as e:
        logger.warning("ife_client: fetch_wiki error symbol=%r: %s", symbol_name, e)
        return None


def resolve_symbol_code(name: str, redis_client=None) -> str | None:
    """종목명 → 종목코드 변환.

    1. 내부 역매핑 dict (collector._DEFAULT_KR_NAMES 기반)
    2. Redis watchlist:KR:name_to_code 해시 조회
    3. 실패 시 None
    """
    # 1. 내부 역매핑
    from .collector import _DEFAULT_KR_NAMES
    name_to_code = {v: k for k, v in _DEFAULT_KR_NAMES.items()}
    if name in name_to_code:
        return name_to_code[name]

    # 2. Redis 조회
    if redis_client is not None:
        try:
            val = redis_client.hget("watchlist:KR:name_to_code", name)
            if val:
                return val.decode() if isinstance(val, bytes) else val
        except Exception as e:
            logger.debug("ife_client: redis name_to_code lookup error: %s", e)

    return None
