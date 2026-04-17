from __future__ import annotations

import os
import sys
import time

import fakeredis
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exchange.kis.client import KisClient


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self._payload


class _SeqSession:
    def __init__(self, responses: list[_Resp]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if not self.responses:
            raise AssertionError("no more responses")
        return self.responses.pop(0)


@pytest.fixture
def kis_env(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "test-app")
    monkeypatch.setenv("KIS_APP_SECRET", "test-secret")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
    monkeypatch.delenv("REDIS_URL", raising=False)


def test_request_with_retry_retries_on_500(kis_env):
    client = KisClient()
    client.access_token = "token"
    client._token_fetched_at = time.time()
    client.session = _SeqSession([_Resp(500), _Resp(200, {"ok": True})])

    resp = client._request_with_retry(
        "get",
        "https://example.test/balance",
        headers={"authorization": "Bearer token"},
    )

    assert resp.status_code == 200
    assert len(client.session.calls) == 2


def test_request_with_retry_refreshes_token_on_401(kis_env, monkeypatch):
    client = KisClient()
    client.access_token = "old-token"
    client._token_fetched_at = time.time()
    client.session = _SeqSession([_Resp(401), _Resp(200, {"ok": True})])

    def _refresh():
        client.access_token = "new-token"
        client._token_fetched_at = time.time()

    monkeypatch.setattr(client, "_refresh_token", _refresh)

    resp = client._request_with_retry(
        "get",
        "https://example.test/balance",
        headers={"authorization": "Bearer old-token"},
    )

    assert resp.status_code == 200
    assert len(client.session.calls) == 2
    assert client.session.calls[1]["headers"]["authorization"] == "Bearer new-token"


def test_request_with_retry_preserves_shared_redis_token_on_401(kis_env, monkeypatch):
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    fake_redis.set("kis:access_token", "shared-token", ex=3600)

    monkeypatch.setattr("exchange.kis.client.redis.from_url", lambda *args, **kwargs: fake_redis)
    monkeypatch.setenv("REDIS_URL", "redis://test")

    client = KisClient()
    client.access_token = "stale-token"
    client._token_fetched_at = time.time()
    client.session = _SeqSession([_Resp(401), _Resp(200, {"ok": True})])

    def _refresh():
        pytest.fail("_refresh_token should not run when shared redis token is available")

    monkeypatch.setattr(client, "_refresh_token", _refresh)

    resp = client._request_with_retry(
        "get",
        "https://example.test/balance",
        headers={"authorization": "Bearer stale-token"},
    )

    assert resp.status_code == 200
    assert fake_redis.get("kis:access_token") == "shared-token"
    assert client.session.calls[1]["headers"]["authorization"] == "Bearer shared-token"


def test_refresh_token_waits_for_existing_refresh_lock(kis_env, monkeypatch):
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    fake_redis.set("kis:token_refresh_lock", "1", ex=15)
    fake_redis.set("kis:access_token", "shared-token", ex=3600)

    monkeypatch.setattr("exchange.kis.client.redis.from_url", lambda *args, **kwargs: fake_redis)
    monkeypatch.setenv("REDIS_URL", "redis://test")

    client = KisClient()

    def _unexpected_post(*args, **kwargs):
        pytest.fail("token endpoint should not be called while waiting for shared token")

    client.session.post = _unexpected_post

    client._refresh_token()

    assert client.access_token == "shared-token"
