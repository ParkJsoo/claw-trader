"""뉴스 -> AI 프롬프트 통합 테스트."""
from __future__ import annotations

import fakeredis

from ai.providers.base import build_dual_prompt
from utils.redis_helpers import load_watchlist


# ---------------------------------------------------------------------------
# build_dual_prompt — news_summary 포함
# ---------------------------------------------------------------------------

class TestBuildDualPromptWithNews:
    def test_no_news_no_section(self):
        features = {
            "current_price": "70000",
            "ret_1m": 0.001,
            "ret_5m": 0.003,
            "range_5m": 0.005,
        }
        prompt = build_dual_prompt("KR", "005930", features)

        assert "최근 뉴스" not in prompt

    def test_news_summary_included(self):
        features = {
            "current_price": "70000",
            "ret_1m": 0.001,
            "ret_5m": 0.003,
            "range_5m": 0.005,
            "news_summary": "[SYMBOL][HIGH][positive][google] 삼성전자 수요 회복",
        }
        prompt = build_dual_prompt("KR", "005930", features)

        assert "최근 뉴스" in prompt
        assert "삼성전자 수요 회복" in prompt

    def test_multi_line_news(self):
        features = {
            "current_price": "70000",
            "ret_1m": 0.001,
            "ret_5m": 0.003,
            "range_5m": 0.005,
            "news_summary": (
                "[SYMBOL][HIGH][positive][google] Line 1\n"
                "[MACRO][MEDIUM][reuters] Line 2"
            ),
        }
        prompt = build_dual_prompt("KR", "005930", features)

        assert "- [SYMBOL][HIGH][positive][google] Line 1" in prompt
        assert "- [MACRO][MEDIUM][reuters] Line 2" in prompt

    def test_news_instruction_present(self):
        features = {
            "current_price": "70000",
            "ret_1m": 0.001,
            "ret_5m": 0.003,
            "range_5m": 0.005,
            "news_summary": "[SYMBOL][HIGH][positive][google] test",
        }
        prompt = build_dual_prompt("KR", "005930", features)

        assert "최근 뉴스" in prompt
        assert "Your role: BAD NEWS FILTER" in prompt

    def test_us_market_with_news(self):
        features = {
            "current_price": "150.00",
            "ret_1m": 0.002,
            "ret_5m": 0.004,
            "range_5m": 0.006,
            "news_summary": "[SYMBOL][HIGH][positive][yahoo] AAPL rally",
        }
        prompt = build_dual_prompt("US", "AAPL", features)

        assert "최근 뉴스" in prompt
        assert "AAPL rally" in prompt


# ---------------------------------------------------------------------------
# load_watchlist — 동적 워치리스트
# ---------------------------------------------------------------------------

class TestLoadWatchlist:
    def test_redis_set_takes_priority(self):
        r = fakeredis.FakeRedis()
        r.sadd("dynamic:watchlist:KR", "005930", "000660")

        import os
        os.environ["GEN_WATCHLIST_KR"] = "035720,032640"
        try:
            result = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
            assert set(result) == {"005930", "000660"}
        finally:
            os.environ.pop("GEN_WATCHLIST_KR", None)

    def test_fallback_to_env(self):
        r = fakeredis.FakeRedis()
        # Redis에 동적 워치리스트 없음

        import os
        os.environ["GEN_WATCHLIST_KR"] = "035720,032640"
        try:
            result = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
            assert result == ["035720", "032640"]
        finally:
            os.environ.pop("GEN_WATCHLIST_KR", None)

    def test_empty_redis_set_falls_back(self):
        r = fakeredis.FakeRedis()
        # 빈 SET (키 존재하지만 멤버 없음) → smembers returns empty set

        import os
        os.environ["GEN_WATCHLIST_KR"] = "005930"
        try:
            result = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
            assert result == ["005930"]
        finally:
            os.environ.pop("GEN_WATCHLIST_KR", None)
