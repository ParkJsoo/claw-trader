"""뉴스 아이템 모델."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


def _content_hash(title: str, excerpt: str, source: str) -> str:
    """제목+내용+소스 기반 중복 감지용 해시 (URL 달라도 같은 기사 dedup)."""
    raw = f"{title[:200]}|{excerpt[:300]}|{source}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class NewsItem:
    title: str
    url: str
    source: str        # dart / google_kr / google_us / yahoo_us
    published_at: str  # 원문 게재 시각 (ISO or RFC2822 string)
    market: str        # KR / US / GLOBAL
    excerpt: str = ""
    symbols: list[str] = field(default_factory=list)  # 관련 종목코드

    # 수집 메타 — __post_init__에서 자동 설정 (빈 문자열/0이 기본값)
    content_hash: str = ""    # sha256(title+excerpt+source)[:16] — 내용 기반 dedup
    ingested_at: int = 0      # 수집 시각 (epoch ms)
    scope: str = ""           # "symbol" or "macro" — symbols 유무로 자동 결정

    # 소스 신뢰도
    reliability: float = 0.65  # dart=0.95 / yahoo=0.80 / google=0.65

    # Qwen 분류 결과
    relevant: bool = True
    sentiment: str = "neutral"   # positive / negative / neutral
    impact: str = "medium"       # high / medium / low
    category: str = "other"      # earnings/macro/geopolitical/sector/regulatory/other
    ai_summary: str = ""
    classified: bool = False

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = _content_hash(self.title, self.excerpt, self.source)
        if not self.ingested_at:
            self.ingested_at = int(time.time() * 1000)
        if not self.scope:
            self.scope = "symbol" if self.symbols else "macro"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "market": self.market,
            "excerpt": self.excerpt[:300],
            "symbols": ",".join(self.symbols),
            "content_hash": self.content_hash,
            "ingested_at": str(self.ingested_at),
            "scope": self.scope,
            "reliability": str(round(self.reliability, 2)),
            "relevant": "1" if self.relevant else "0",
            "sentiment": self.sentiment,
            "impact": self.impact,
            "category": self.category,
            "ai_summary": self.ai_summary,
            "classified": "1" if self.classified else "0",
        }
