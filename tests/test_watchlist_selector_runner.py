"""watchlist_selector_runner 단위 테스트."""
from __future__ import annotations

import json

import fakeredis
import pytest

from app.watchlist_selector_runner import score_symbol, select_watchlist, write_watchlist


# ---------------------------------------------------------------------------
# score_symbol
# ---------------------------------------------------------------------------

class TestScoreSymbol:
    def test_no_news_no_mark_returns_zero(self):
        r = fakeredis.FakeRedis()
        assert score_symbol(r, "KR", "005930", "20260318") == 0.0

    def test_positive_high_news_adds_2(self):
        r = fakeredis.FakeRedis()
        item = json.dumps({"sentiment": "positive", "impact": "high", "title": "good"})
        r.lpush("news:symbol:KR:005930:20260318", item)

        assert score_symbol(r, "KR", "005930", "20260318") == 2.0

    def test_negative_high_news_subtracts_2(self):
        r = fakeredis.FakeRedis()
        item = json.dumps({"sentiment": "negative", "impact": "high", "title": "bad"})
        r.lpush("news:symbol:KR:005930:20260318", item)

        assert score_symbol(r, "KR", "005930", "20260318") == -2.0

    def test_multiple_news_accumulated(self):
        r = fakeredis.FakeRedis()
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "high"}))
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "medium"}))

        assert score_symbol(r, "KR", "005930", "20260318") == 3.0  # 2 + 1

    def test_yesterday_news_included(self):
        r = fakeredis.FakeRedis()
        # 어제 뉴스만 있음
        r.lpush("news:symbol:KR:005930:20260317",
                json.dumps({"sentiment": "positive", "impact": "high"}))

        assert score_symbol(r, "KR", "005930", "20260318") == 2.0

    def test_momentum_bonus(self):
        r = fakeredis.FakeRedis()
        r.hset("mark:KR:005930", mapping={"ret_5m": "0.003"})

        assert score_symbol(r, "KR", "005930", "20260318") == 1.0

    def test_neutral_news_zero_score(self):
        r = fakeredis.FakeRedis()
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "neutral", "impact": "medium"}))

        assert score_symbol(r, "KR", "005930", "20260318") == 0.0


# ---------------------------------------------------------------------------
# select_watchlist
# ---------------------------------------------------------------------------

class TestSelectWatchlist:
    def test_selects_top_n(self):
        r = fakeredis.FakeRedis()
        # A: score 2, B: score 0, C: score 1 — 모두 5만원 이하 mark 설정
        r.set("mark:KR:A", "30000")
        r.set("mark:KR:B", "30000")
        r.set("mark:KR:C", "30000")
        r.lpush("news:symbol:KR:A:20260318",
                json.dumps({"sentiment": "positive", "impact": "high"}))
        r.lpush("news:symbol:KR:C:20260318",
                json.dumps({"sentiment": "positive", "impact": "medium"}))

        from unittest.mock import patch
        with patch("app.watchlist_selector_runner.today_kst", return_value="20260318"):
            result = select_watchlist(r, "KR", ["A", "B", "C"], 2)

        assert result == ["A", "C"]

    def test_returns_all_if_universe_smaller_than_count(self):
        r = fakeredis.FakeRedis()
        r.set("mark:KR:A", "30000")
        r.set("mark:KR:B", "30000")

        from unittest.mock import patch
        with patch("app.watchlist_selector_runner.today_kst", return_value="20260318"):
            result = select_watchlist(r, "KR", ["A", "B"], 5)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# write_watchlist
# ---------------------------------------------------------------------------

class TestWriteWatchlist:
    def test_writes_to_redis_set(self):
        r = fakeredis.FakeRedis()
        write_watchlist(r, "KR", ["005930", "000660"])

        members = r.smembers("dynamic:watchlist:KR")
        assert {m.decode() for m in members} == {"005930", "000660"}

    def test_set_has_ttl(self):
        r = fakeredis.FakeRedis()
        write_watchlist(r, "KR", ["005930"])

        ttl = r.ttl("dynamic:watchlist:KR")
        assert ttl > 0

    def test_overwrites_previous(self):
        r = fakeredis.FakeRedis()
        write_watchlist(r, "KR", ["005930"])
        write_watchlist(r, "KR", ["000660"])

        members = r.smembers("dynamic:watchlist:KR")
        assert {m.decode() for m in members} == {"000660"}
