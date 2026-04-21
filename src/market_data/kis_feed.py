from __future__ import annotations

import os
import redis as _redis
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

_REDIS_TOKEN_KEY = "kis:access_token"
_REDIS_TOKEN_TTL = 23 * 3600  # 23시간
_REDIS_TOKEN_BLOCKED_KEY = "kis:token_refresh_blocked"
_REDIS_TOKEN_BLOCKED_TTL = 3600
_REDIS_TOKEN_RETRY_KEY = "kis:token_refresh_retry_after"
_REDIS_TOKEN_RETRY_TTL = 30
_REDIS_TOKEN_LOCK_KEY = "kis:token_refresh_lock"
_REDIS_TOKEN_LOCK_TTL = 15
_TOKEN_LOCK_WAIT_SEC = 8.0
_TOKEN_LOCK_POLL_SEC = 0.2
_PRICE_RETRY_ATTEMPTS = 3
_PRICE_RETRY_BASE_SEC = 0.2
_PRICE_BACKOFF_KEY = "kis:price_backoff"
_PRICE_BACKOFF_SEC = max(1, int(os.getenv("KIS_PRICE_BACKOFF_SEC", "3")))

_TOKEN_EXPIRED = object()  # sentinel: 401/403 토큰 만료 신호


class KisFeed:
    """KIS REST 기반 현재가 폴링 피드."""

    def __init__(self):
        self.app_key = os.getenv("KIS_APP_KEY")
        self.app_secret = os.getenv("KIS_APP_SECRET")
        self.base_url = os.getenv(
            "KIS_BASE_URL",
            "https://openapi.koreainvestment.com:9443",
        )

        if not all([self.app_key, self.app_secret]):
            raise RuntimeError("KIS env is not fully set")

        self.session = requests.Session()
        self.access_token: Optional[str] = None
        self.last_error_reason: Optional[str] = None
        self._local_price_backoff_until: float = 0.0
        self._redis: Optional[_redis.Redis] = None
        try:
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                self._redis = _redis.from_url(redis_url, decode_responses=True)
        except Exception:
            pass

    def _ensure_token(self) -> None:
        if self.access_token:
            return
        # Redis 캐시 조회
        if self._redis:
            try:
                cached = self._redis.get(_REDIS_TOKEN_KEY)
                if cached:
                    self.access_token = cached
                    return
            except Exception:
                pass
            try:
                blocked_ttl = self._redis.ttl(_REDIS_TOKEN_BLOCKED_KEY)
                if blocked_ttl > 0:
                    raise RuntimeError(f"KIS token refresh blocked (retry in {blocked_ttl}s)")
            except RuntimeError:
                raise
            except Exception:
                pass
            try:
                retry_ttl = self._redis.ttl(_REDIS_TOKEN_RETRY_KEY)
                if retry_ttl > 0:
                    raise RuntimeError(f"KIS token refresh deferred (retry in {retry_ttl}s)")
            except RuntimeError:
                raise
            except Exception:
                pass
        lock_acquired = False
        if self._redis:
            try:
                lock_acquired = bool(
                    self._redis.set(
                        _REDIS_TOKEN_LOCK_KEY,
                        "1",
                        nx=True,
                        ex=_REDIS_TOKEN_LOCK_TTL,
                    )
                )
            except Exception:
                lock_acquired = False
            if not lock_acquired:
                if self._wait_for_shared_token():
                    return
                raise RuntimeError("KIS token refresh in progress by another process")
        try:
            resp = self.session.post(
                f"{self.base_url}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
                timeout=10,
            )
            if resp.status_code == 403:
                if self._redis:
                    try:
                        cached = self._redis.get(_REDIS_TOKEN_KEY)
                        if cached:
                            self.access_token = cached
                            return
                    except Exception:
                        pass
                    try:
                        self._redis.set(_REDIS_TOKEN_BLOCKED_KEY, "1", ex=_REDIS_TOKEN_BLOCKED_TTL, nx=True)
                    except Exception:
                        pass
                raise RuntimeError("KIS token refresh failed: 403 Forbidden (rate limited)")
            resp.raise_for_status()
        except RuntimeError:
            raise
        except Exception as e:
            if self._redis:
                try:
                    self._redis.set(_REDIS_TOKEN_RETRY_KEY, "1", ex=_REDIS_TOKEN_RETRY_TTL, nx=True)
                except Exception:
                    pass
            raise RuntimeError(f"KIS token refresh failed: {type(e).__name__}") from None
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"KIS token refresh: unexpected response rt_cd={data.get('rt_cd')}")
        self.access_token = data["access_token"]
        # Redis에 캐시 저장
        if self._redis:
            try:
                self._redis.set(_REDIS_TOKEN_KEY, self.access_token, ex=_REDIS_TOKEN_TTL)
                self._redis.delete(_REDIS_TOKEN_BLOCKED_KEY)
                self._redis.delete(_REDIS_TOKEN_RETRY_KEY)
            except Exception:
                pass
            finally:
                try:
                    if lock_acquired:
                        self._redis.delete(_REDIS_TOKEN_LOCK_KEY)
                except Exception:
                    pass

    def _wait_for_shared_token(self, timeout_sec: float = _TOKEN_LOCK_WAIT_SEC) -> bool:
        if not self._redis:
            return False

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                cached = self._redis.get(_REDIS_TOKEN_KEY)
                if cached:
                    self.access_token = cached
                    return True
            except Exception:
                return False
            time.sleep(_TOKEN_LOCK_POLL_SEC)
        return False

    def _format_price_error(self, symbol: str, resp: requests.Response) -> str:
        parts = [f"status={resp.status_code}", f"symbol={symbol}"]
        try:
            payload = resp.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            for key in ("rt_cd", "msg_cd", "msg1"):
                value = payload.get(key)
                if value not in (None, ""):
                    parts.append(f"{key}={value}")
        else:
            text = (resp.text or "").strip()
            if text:
                parts.append(f"body={text[:160]}")

        return "KIS price request failed: " + " ".join(parts)

    def _classify_price_error(self, detail: str) -> str:
        text = (detail or "").lower()
        if "msg_cd=egw00201" in text or "status=429" in text:
            return "kis_price_rate_limit"
        if "connecttimeout" in text or "readtimeout" in text or "request_error=timeout" in text:
            return "kis_price_timeout"
        if "token refresh blocked" in text:
            return "kis_token_refresh_blocked"
        if "token refresh deferred" in text:
            return "kis_token_refresh_deferred"
        if "token refresh in progress" in text:
            return "kis_token_refresh_in_progress"
        if "status=500" in text:
            return "kis_price_http_500"
        if "status=403" in text:
            return "kis_price_http_403"
        if "status=401" in text:
            return "kis_price_http_401"
        return "kis_price_error"

    def _active_price_backoff_ttl(self) -> int:
        ttl = 0
        if self._local_price_backoff_until > time.time():
            ttl = max(ttl, int(self._local_price_backoff_until - time.time()))
        if self._redis:
            try:
                ttl = max(ttl, self._redis.ttl(_PRICE_BACKOFF_KEY))
            except Exception:
                pass
        return max(ttl, 0)

    def _activate_price_backoff(self, seconds: int = _PRICE_BACKOFF_SEC) -> None:
        self._local_price_backoff_until = max(self._local_price_backoff_until, time.time() + seconds)
        if self._redis:
            try:
                self._redis.set(_PRICE_BACKOFF_KEY, "1", ex=seconds)
            except Exception:
                pass

    def _fetch_price(self, symbol: str) -> Optional[Decimal]:
        """단일 가격 요청. 호출 전 토큰이 유효해야 함."""
        for attempt in range(1, _PRICE_RETRY_ATTEMPTS + 1):
            try:
                resp = self.session.get(
                    f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                    headers={
                        "authorization": f"Bearer {self.access_token}",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret,
                        "tr_id": "FHKST01010100",
                        "content-type": "application/json",
                    },
                    params={
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": symbol,
                    },
                    timeout=5,
                )
            except requests.RequestException as e:
                if attempt < _PRICE_RETRY_ATTEMPTS:
                    time.sleep(_PRICE_RETRY_BASE_SEC * attempt)
                    continue
                raise RuntimeError(
                    f"KIS price request failed: request_error={type(e).__name__} symbol={symbol}"
                ) from None

            # 401/403: 토큰 만료 신호 — sentinel 반환, 호출자가 재발급 처리
            if resp.status_code in (401, 403):
                print(f"kis_token_expired: {symbol} status={resp.status_code}")
                return _TOKEN_EXPIRED  # type: ignore[return-value]

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _PRICE_RETRY_ATTEMPTS:
                    time.sleep(_PRICE_RETRY_BASE_SEC * attempt)
                    continue
                raise RuntimeError(self._format_price_error(symbol, resp))

            try:
                resp.raise_for_status()
            except Exception:
                raise RuntimeError(self._format_price_error(symbol, resp)) from None
            break

        output = resp.json().get("output", {})
        raw = output.get("stck_prpr", "")
        price_str = str(raw).replace(",", "").strip() if raw else ""
        if not price_str:
            return None

        # acml_vol(누적거래량) Redis 저장 (volume surge 필터용)
        try:
            vol_raw = output.get("acml_vol", "")
            vol_str = str(vol_raw).replace(",", "").strip() if vol_raw else ""
            if vol_str and self._redis:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
                vol_key = f"vol:KR:{symbol}:{today}"
                self._redis.set(vol_key, vol_str, ex=25 * 3600)  # 다음날 자정까지 유지
        except Exception:
            pass

        try:
            return Decimal(price_str)
        except InvalidOperation:
            print(f"kis_price_parse_error: {symbol} raw={price_str!r}")
            return None

    def _clear_token(self) -> None:
        """in-memory 토큰만 삭제. 공유 Redis 캐시는 보존."""
        self.access_token = None

    def get_price(self, symbol: str) -> Optional[Decimal]:
        """
        현재가 조회.
        - 401/403: 공유 토큰 재사용 또는 재발급 후 1회 재시도
        - 429/5xx/네트워크: 가격 요청 자체에서 짧게 재시도
        - 기타 HTTP 오류/데이터 없음: 로그 후 None 반환
        """
        self.last_error_reason = None
        backoff_ttl = self._active_price_backoff_ttl()
        if backoff_ttl > 0:
            self.last_error_reason = "kis_price_backoff_active"
            return None

        def _retry() -> Optional[Decimal]:
            self._clear_token()
            if self._wait_for_shared_token(timeout_sec=1.0):
                result = self._fetch_price(symbol)
                return None if result is _TOKEN_EXPIRED else result  # type: ignore[comparison-overlap]
            self._ensure_token()
            result = self._fetch_price(symbol)
            return None if result is _TOKEN_EXPIRED else result  # type: ignore[comparison-overlap]

        try:
            self._ensure_token()
            result = self._fetch_price(symbol)
            if result is _TOKEN_EXPIRED:
                # 401/403 토큰 만료 → 재발급 후 재시도
                return _retry()
            self.last_error_reason = None
            return result  # type: ignore[return-value]
        except Exception as e:
            detail = str(e)
            self.last_error_reason = self._classify_price_error(detail)
            if self.last_error_reason == "kis_price_rate_limit":
                self._activate_price_backoff()
            print(f"kis_price_error: {symbol} {detail}")
            return None
