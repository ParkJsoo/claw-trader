"""뉴스 아이템 모델."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NewsItem:
    title: str
    url: str
    source: str        # dart / google_kr / google_us / yahoo_us
    published_at: str  # ISO datetime string
    market: str        # KR / US / GLOBAL
    excerpt: str = ""
    symbols: list[str] = field(default_factory=list)  # 관련 종목코드

    # 소스 신뢰도 (수집 시 자동 설정)
    reliability: float = 0.65    # dart=0.95 / yahoo=0.80 / google=0.65

    # Qwen 분류 결과
    relevant: bool = True
    sentiment: str = "neutral"   # positive / negative / neutral
    impact: str = "medium"       # high / medium / low
    category: str = "other"      # earnings/macro/geopolitical/sector/regulatory/other
    ai_summary: str = ""
    classified: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "market": self.market,
            "excerpt": self.excerpt[:300],
            "symbols": ",".join(self.symbols),
            "reliability": str(round(self.reliability, 2)),
            "relevant": "1" if self.relevant else "0",
            "sentiment": self.sentiment,
            "impact": self.impact,
            "category": self.category,
            "ai_summary": self.ai_summary,
            "classified": "1" if self.classified else "0",
        }
