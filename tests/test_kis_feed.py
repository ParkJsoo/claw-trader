from __future__ import annotations

import os
import sys
from decimal import Decimal

import fakeredis
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_data.kis_feed import KisFeed


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}", response=self)

    def json(self):
        return self._payload


class _SeqSession:
    def __init__(self, responses: list[_Resp]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("no more responses")
        return self.responses.pop(0)


@pytest.fixture
def kis_feed_env(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "test-app")
    monkeypatch.setenv("KIS_APP_SECRET", "test-secret")
    monkeypatch.delenv("REDIS_URL", raising=False)


def test_ensure_token_waits_for_existing_refresh_lock(kis_feed_env, monkeypatch):
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    fake_redis.set("kis:token_refresh_lock", "1", ex=15)
    fake_redis.set("kis:access_token", "shared-token", ex=3600)

    monkeypatch.setattr("market_data.kis_feed._redis.from_url", lambda *args, **kwargs: fake_redis)
    monkeypatch.setenv("REDIS_URL", "redis://test")

    feed = KisFeed()

    def _unexpected_post(*args, **kwargs):
        pytest.fail("token endpoint should not be called while waiting for shared token")

    feed.session.post = _unexpected_post

    feed._ensure_token()

    assert feed.access_token == "shared-token"


def test_get_price_retries_transient_http_500(kis_feed_env, monkeypatch):
    feed = KisFeed()
    feed.access_token = "token"
    feed.session = _SeqSession(
        [
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "temporary"}),
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "temporary"}),
            _Resp(200, {"output": {"stck_prpr": "12345", "acml_vol": "1000"}}),
        ]
    )

    monkeypatch.setattr(feed, "_ensure_token", lambda: None)
    monkeypatch.setattr("market_data.kis_feed.time.sleep", lambda *_args, **_kwargs: None)

    price = feed.get_price("005930")

    assert price == Decimal("12345")
    assert len(feed.session.calls) == 3


def test_get_price_logs_status_details_without_clearing_token(kis_feed_env, monkeypatch, capsys):
    feed = KisFeed()
    feed.access_token = "shared-token"
    feed.session = _SeqSession(
        [
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "temporary"}),
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "temporary"}),
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "temporary"}),
        ]
    )

    monkeypatch.setattr(feed, "_ensure_token", lambda: None)
    monkeypatch.setattr("market_data.kis_feed.time.sleep", lambda *_args, **_kwargs: None)

    price = feed.get_price("005930")

    captured = capsys.readouterr().out
    assert price is None
    assert feed.access_token == "shared-token"
    assert "status=500" in captured
    assert "msg_cd=EGW00123" in captured
    assert "msg1=temporary" in captured


def test_get_price_marks_rate_limit_and_shared_backoff(kis_feed_env, monkeypatch):
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("market_data.kis_feed._redis.from_url", lambda *args, **kwargs: fake_redis)
    monkeypatch.setenv("REDIS_URL", "redis://test")

    feed = KisFeed()
    feed.access_token = "shared-token"
    feed.session = _SeqSession(
        [
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate"}),
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate"}),
            _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate"}),
        ]
    )

    monkeypatch.setattr(feed, "_ensure_token", lambda: None)
    monkeypatch.setattr("market_data.kis_feed.time.sleep", lambda *_args, **_kwargs: None)

    price = feed.get_price("005930")

    assert price is None
    assert feed.last_error_reason == "kis_price_rate_limit"
    assert fake_redis.ttl("kis:price_backoff") > 0

    feed.session = _SeqSession([_Resp(200, {"output": {"stck_prpr": "54321"}})])
    second = feed.get_price("000660")

    assert second is None
    assert feed.last_error_reason == "kis_price_backoff_active"
    assert len(feed.session.calls) == 0
