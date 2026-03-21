"""Phase 17 신호 품질 강화 테스트."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import fakeredis
import pytest

from app.consensus_signal_runner import (
    _get_news_score, _has_volume_surge,
)


class TestGetNewsScore:
    def test_no_news_returns_none(self):
        r = fakeredis.FakeRedis()
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            assert _get_news_score(r, "KR", "005930") == "none"

    def test_high_news_returns_high(self):
        r = fakeredis.FakeRedis()
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "high"}))
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            assert _get_news_score(r, "KR", "005930") == "high"

    def test_medium_news_returns_medium(self):
        r = fakeredis.FakeRedis()
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "medium"}))
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            assert _get_news_score(r, "KR", "005930") == "medium"

    def test_high_takes_priority_over_medium(self):
        r = fakeredis.FakeRedis()
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "medium"}))
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "positive", "impact": "high"}))
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            assert _get_news_score(r, "KR", "005930") == "high"

    def test_negative_news_returns_none(self):
        r = fakeredis.FakeRedis()
        r.lpush("news:symbol:KR:005930:20260318",
                json.dumps({"sentiment": "negative", "impact": "high"}))
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            assert _get_news_score(r, "KR", "005930") == "none"


class TestHasVolumeSurge:
    def _set_vol(self, r, market, symbol, date_str, vol):
        r.set(f"vol:{market}:{symbol}:{date_str}", str(vol))

    def test_no_data_returns_true(self):
        """데이터 없으면 통과 (permissive)."""
        r = fakeredis.FakeRedis()
        with patch("app.consensus_signal_runner.today_kst", return_value="20260318"):
            assert _has_volume_surge(r, "KR", "005930") is True

    def test_surge_detected(self):
        """오늘 거래량이 평균의 1.5배 이상이면 True."""
        r = fakeredis.FakeRedis()
        today = "20260318"
        self._set_vol(r, "KR", "005930", today, 1500000)
        for i in range(1, 6):
            dt = datetime(2026, 3, 18) - timedelta(days=i)
            self._set_vol(r, "KR", "005930", dt.strftime("%Y%m%d"), 1000000)
        with patch("app.consensus_signal_runner.today_kst", return_value=today):
            assert _has_volume_surge(r, "KR", "005930") is True

    def test_no_surge(self):
        """오늘 거래량이 평균의 1.5배 미만이면 False."""
        r = fakeredis.FakeRedis()
        today = "20260318"
        self._set_vol(r, "KR", "005930", today, 900000)
        for i in range(1, 6):
            dt = datetime(2026, 3, 18) - timedelta(days=i)
            self._set_vol(r, "KR", "005930", dt.strftime("%Y%m%d"), 1000000)
        with patch("app.consensus_signal_runner.today_kst", return_value=today):
            assert _has_volume_surge(r, "KR", "005930") is False

    def test_insufficient_history_returns_true(self):
        """과거 데이터 3개 미만이면 통과."""
        r = fakeredis.FakeRedis()
        today = "20260318"
        self._set_vol(r, "KR", "005930", today, 500000)
        self._set_vol(r, "KR", "005930", "20260317", 1000000)
        self._set_vol(r, "KR", "005930", "20260316", 1000000)
        # 과거 2개만 → 3개 미만 → True
        with patch("app.consensus_signal_runner.today_kst", return_value=today):
            assert _has_volume_surge(r, "KR", "005930") is True


class TestRet15mFeature:
    """generator._compute_features에 ret_15m이 추가됐는지 검증."""

    def test_ret_15m_included_in_features(self):
        import fakeredis
        from ai.generator import AISignalGenerator
        r = fakeredis.FakeRedis()
        gen = AISignalGenerator(r)
        now_ms = int(time.time() * 1000)
        # 20분치 데이터 생성 (3초 간격, 400개)
        entries = []
        for i in range(400):
            ts = now_ms - i * 3000
            price = 50000 + i * 10
            entries.append(f"{ts}:{price}")
        features = gen._compute_features(entries, now_ms)
        assert features is not None
        assert "ret_15m" in features

    def test_ret_15m_none_when_insufficient_data(self):
        """데이터가 15분치 미만이면 ret_15m=None."""
        import fakeredis
        from ai.generator import AISignalGenerator
        r = fakeredis.FakeRedis()
        gen = AISignalGenerator(r)
        now_ms = int(time.time() * 1000)
        # 5분치 데이터만 (100개 × 3초 = 5분)
        entries = []
        for i in range(100):
            ts = now_ms - i * 3000
            entries.append(f"{ts}:50000")
        features = gen._compute_features(entries, now_ms)
        assert features is not None
        assert features["ret_15m"] is None
