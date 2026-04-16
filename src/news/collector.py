"""뉴스/공시 수집 — DART + Google News RSS + Yahoo Finance RSS."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import defusedxml.ElementTree as SafeET
import requests

from .models import NewsItem

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

_DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_GOOGLE_RSS_KR = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
_GOOGLE_RSS_US = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
_YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"

_RCEPT_NO_RE = re.compile(r"^\d{14}$")  # DART 접수번호 형식 검증

_SOURCE_RELIABILITY: dict[str, float] = {
    "dart": 0.95,
    "yahoo_us": 0.80,
    "google_kr": 0.65,
    "google_us": 0.65,
    "ife_home": 0.85,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 10

# 기본 KR 종목코드 → 회사명 매핑 (env NEWS_KR_NAMES 으로 추가/재정의 가능)
_DEFAULT_KR_NAMES: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "005380": "현대자동차",
    "051910": "LG화학",
    "006400": "삼성SDI",
    "207940": "삼성바이오로직스",
    "068270": "셀트리온",
    "035720": "카카오",
    "000270": "기아",
    "005490": "POSCO홀딩스",
    "096770": "SK이노베이션",
}

_DEFAULT_US_NAMES: dict[str, str] = {
    "AAPL": "Apple",
    "NVDA": "NVIDIA",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta",
    "TSLA": "Tesla",
    "TSM": "TSMC",
}

_DEFAULT_MACRO_KR = [
    "코스피", "금리", "환율", "반도체 수출규제",
    "트럼프 관세", "연준 금리", "원달러", "한국은행 기준금리",
]
_DEFAULT_MACRO_US = [
    "fed interest rate", "S&P 500", "tariff trade war",
    "semiconductor export", "nasdaq outlook", "inflation CPI",
    "treasury yield", "geopolitical risk stock market",
]


def _load_kr_names() -> dict[str, str]:
    mapping = dict(_DEFAULT_KR_NAMES)
    raw = os.getenv("NEWS_KR_NAMES", "")
    for entry in raw.split(","):
        if ":" in entry:
            code, name = entry.split(":", 1)
            mapping[code.strip()] = name.strip()
    return mapping


def _load_us_names() -> dict[str, str]:
    mapping = dict(_DEFAULT_US_NAMES)
    raw = os.getenv("NEWS_US_NAMES", "")
    for entry in raw.split(","):
        if ":" in entry:
            code, name = entry.split(":", 1)
            mapping[code.strip()] = name.strip()
    return mapping


def _load_macro_keywords(env_var: str, default: list[str]) -> list[str]:
    """env 변수에서 매크로 키워드 목록 로드. 비어있으면 default 반환."""
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    keywords = [kw.strip() for kw in raw.split(",") if kw.strip()]
    return keywords if keywords else default


def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _has_korean(text: str) -> bool:
    return any("\uAC00" <= c <= "\uD7AF" for c in text)


def _has_english(text: str) -> bool:
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return latin >= 3


def _is_language_match(title: str, market: str) -> bool:
    """KR 뉴스는 한글 포함 필수, US 뉴스는 영어(ASCII) 포함 필수."""
    if market == "KR":
        return _has_korean(title)
    return _has_english(title)


def _parse_rss(xml_text: str, source: str, market: str,
               symbols: list[str]) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = SafeET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return items
        for item in channel.findall("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            pub_el = item.find("pubDate")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            url = link_el.text.strip() if link_el is not None and link_el.text else ""
            excerpt = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            pub = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

            if not title or not url:
                continue

            # 언어 필터: KR=한글 포함 필수, US=영어 포함 필수
            if not _is_language_match(title, market):
                continue

            # HTML 태그 제거
            excerpt = re.sub(r"<[^>]+>", "", excerpt)[:500]

            items.append(NewsItem(
                title=title[:300],
                url=url,
                source=source,
                published_at=pub,
                market=market,
                excerpt=excerpt,
                symbols=list(symbols),
                reliability=_SOURCE_RELIABILITY.get(source, 0.65),
            ))
    except SafeET.ParseError as e:
        logger.debug("RSS ParseError source=%s: %s", source, e)
    except Exception as e:
        logger.warning("RSS parse unexpected error source=%s: %s", source, e)
    return items


def collect_dart(api_key: str, date_str: str) -> list[NewsItem]:
    """DART 공시 수집 (오늘 날짜 기준, 최대 40건)."""
    if not api_key:
        return []
    try:
        resp = requests.get(
            _DART_LIST_URL,
            params={
                "crtfc_key": api_key,
                "bgn_de": date_str,
                "end_de": date_str,
                "page_count": "40",
                "sort": "date",
                "sort_mth": "desc",
            },
            timeout=_TIMEOUT,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return []

        items: list[NewsItem] = []
        kr_names = _load_kr_names()
        name_to_code = {v: k for k, v in kr_names.items()}

        for row in data.get("list", []):
            corp_name = row.get("corp_name", "")
            report_nm = row.get("report_nm", "")
            rcept_no = row.get("rcept_no", "")
            rcept_dt = row.get("rcept_dt", date_str)

            # DART 접수번호 형식 검증 (14자리 숫자)
            if not _RCEPT_NO_RE.match(rcept_no):
                logger.warning("DART invalid rcept_no=%r, skipping", rcept_no)
                continue

            title = f"[DART] {corp_name} — {report_nm}"
            url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

            # 종목코드 매핑 시도
            symbol = name_to_code.get(corp_name, "")
            symbols = [symbol] if symbol else []

            items.append(NewsItem(
                title=title,
                url=url,
                source="dart",
                published_at=rcept_dt,
                market="KR",
                excerpt=report_nm,
                symbols=symbols,
                reliability=_SOURCE_RELIABILITY["dart"],
            ))
        return items
    except Exception as e:
        logger.warning("DART collect error: %s", e)
        return []


def collect_google_rss(query: str, market: str,
                       symbols: list[str], max_items: int = 10) -> list[NewsItem]:
    """Google News RSS 수집."""
    encoded = urllib.parse.quote(query)
    if market == "KR":
        url = _GOOGLE_RSS_KR.format(query=encoded)
        source = "google_kr"
    else:
        url = _GOOGLE_RSS_US.format(query=encoded)
        source = "google_us"
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        items = _parse_rss(resp.text, source, market, symbols)
        return items[:max_items]
    except Exception as e:
        logger.debug("Google RSS error query=%r: %s", query, e)
        return []


def collect_yahoo_rss(symbol: str, max_items: int = 5) -> list[NewsItem]:
    """Yahoo Finance RSS 수집 (US 종목)."""
    url = _YAHOO_RSS.format(symbol=symbol)
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        items = _parse_rss(resp.text, "yahoo_us", "US", [symbol])
        return items[:max_items]
    except Exception as e:
        logger.debug("Yahoo RSS error symbol=%r: %s", symbol, e)
        return []


_GOOGLE_KR_SYMBOL_ENABLED = os.getenv("NEWS_GOOGLE_KR_SYMBOL_ENABLED", "false").lower() in ("true", "1", "yes")


def collect_all(
    dart_api_key: str,
    kr_watchlist: list[str],
    us_watchlist: list[str],
    date_str: str,
    max_per_query: int = 8,
    redis_client=None,
) -> list[NewsItem]:
    """전체 수집 — IFE + DART + Google News (종목별 + 매크로) + Yahoo."""
    all_items: list[NewsItem] = []
    kr_names = _load_kr_names()
    us_names = _load_us_names()

    # 0. IFE 홈 최근 변경사건 (장 시간에만, 우선순위 높음 → prepend)
    now_kst = datetime.now(_KST)
    _market_open = now_kst.replace(hour=8, minute=50, second=0, microsecond=0)
    _market_close = now_kst.replace(hour=15, minute=40, second=0, microsecond=0)
    _is_market = _market_open <= now_kst <= _market_close and now_kst.weekday() < 5

    if _is_market:
        try:
            from .ife_client import fetch_home_events
            ife_events = fetch_home_events(max_items=60)
            ife_items: list[NewsItem] = []
            # kr_names(code→name) 역매핑 — env 추가분 포함, resolve_symbol_code보다 풍부
            kr_name_to_code = {v: k for k, v in kr_names.items()}
            for ev in ife_events:
                code = kr_name_to_code.get(ev.symbol_name)
                if not code and redis_client is not None:
                    try:
                        val = redis_client.hget("watchlist:KR:name_to_code", ev.symbol_name)
                        if val:
                            code = val.decode() if isinstance(val, bytes) else val
                    except Exception:
                        pass
                ife_items.append(ev.to_news_item(symbol_code=code))
            # prepend — IFE 아이템을 앞에 추가해 우선순위 부여
            all_items = ife_items + all_items
            print(f"news:collect ife_home={len(ife_items)}", flush=True)
        except Exception as e:
            logger.warning("collect_all: IFE step error: %s", e)
    else:
        print("news:collect ife_home=skip (off-hours)", flush=True)

    # 1. DART 공시 (KR)
    dart_items = collect_dart(dart_api_key, date_str)
    all_items.extend(dart_items)
    print(f"news:collect dart={len(dart_items)}", flush=True)

    # 2. Google News — KR 종목별 (NEWS_GOOGLE_KR_SYMBOL_ENABLED=true 일 때만)
    if _GOOGLE_KR_SYMBOL_ENABLED:
        for symbol in kr_watchlist:
            name = kr_names.get(symbol, symbol)
            items = collect_google_rss(name, "KR", [symbol], max_per_query)
            all_items.extend(items)

    print(f"news:collect google_kr_symbol={sum(1 for i in all_items if i.source == 'google_kr')}", flush=True)

    # 3. Google News — KR 매크로
    macro_kr = _load_macro_keywords("NEWS_MACRO_KR", _DEFAULT_MACRO_KR)
    for kw in macro_kr[:5]:
        items = collect_google_rss(kw, "KR", [], max_per_query // 2)
        all_items.extend(items)

    # 4. Yahoo Finance + Google News — US 종목별
    for symbol in us_watchlist:
        name = us_names.get(symbol, symbol)
        yahoo_items = collect_yahoo_rss(symbol, max_per_query // 2)
        all_items.extend(yahoo_items)
        google_items = collect_google_rss(f"{name} stock", "US", [symbol], max_per_query // 2)
        all_items.extend(google_items)

    # 5. Google News — US 매크로
    macro_us = _load_macro_keywords("NEWS_MACRO_US", _DEFAULT_MACRO_US)
    for kw in macro_us[:4]:
        items = collect_google_rss(kw, "US", [], max_per_query // 2)
        all_items.extend(items)

    print(
        f"news:collect total={len(all_items)} "
        f"dart={len(dart_items)} "
        f"kr_watchlist={len(kr_watchlist)} us_watchlist={len(us_watchlist)}",
        flush=True,
    )
    return all_items
