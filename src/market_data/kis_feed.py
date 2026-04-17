from __future__ import annotations

import os
import redis as _redis
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

_REDIS_TOKEN_KEY = "kis:access_token"
_REDIS_TOKEN_TTL = 23 * 3600  # 23시간
_REDIS_TOKEN_BLOCKED_KEY = "kis:token_refresh_blocked"
_REDIS_TOKEN_BLOCKED_TTL = 3600
_REDIS_TOKEN_RETRY_KEY = "kis:token_refresh_retry_after"
_REDIS_TOKEN_RETRY_TTL = 30

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
                self._redis.delete(_REDIS_TOKEN_RETRY_KEY)
            except Exception:
                pass

    def _fetch_price(self, symbol: str) -> Optional[Decimal]:
        """단일 가격 요청. 호출 전 토큰이 유효해야 함."""
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
        # 401/403: 토큰 만료 신호 — sentinel 반환, 호출자가 재발급 처리
        if resp.status_code in (401, 403):
            print(f"kis_token_expired: {symbol} status={resp.status_code}")
            return _TOKEN_EXPIRED  # type: ignore[return-value]

        try:
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"KIS price request failed: {type(e).__name__}") from None

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
        """in-memory + Redis 토큰 캐시 삭제."""
        self.access_token = None
        if self._redis:
            try:
                self._redis.delete(_REDIS_TOKEN_KEY)
            except Exception:
                pass

    def get_price(self, symbol: str) -> Optional[Decimal]:
        """
        현재가 조회. 401/403(토큰 만료) 또는 HTTPError 시 토큰 재발급 후 1회 재시도.
        데이터 없음(거래정지 등)은 토큰 재발급 없이 None 반환.
        """
        def _retry() -> Optional[Decimal]:
            self._clear_token()
            self._ensure_token()
            result = self._fetch_price(symbol)
            return None if result is _TOKEN_EXPIRED else result  # type: ignore[comparison-overlap]

        try:
            self._ensure_token()
            result = self._fetch_price(symbol)
            if result is _TOKEN_EXPIRED:
                # 401/403 토큰 만료 → 재발급 후 재시도
                return _retry()
            return result  # type: ignore[return-value]

        except Exception:
            # HTTPError 등 → 토큰 재발급 후 1회 재시도
            try:
                return _retry()
            except Exception as e:
                print(f"kis_price_error: {symbol} {e}")
                return None
