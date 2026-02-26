from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests


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

    def _ensure_token(self) -> None:
        if self.access_token:
            return
        resp = self.session.post(
            f"{self.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        self.access_token = resp.json()["access_token"]

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
        # 401/403: 토큰 만료 신호 — 로그 후 None 반환, 호출자가 재발급 처리
        if resp.status_code in (401, 403):
            print(f"kis_token_expired: {symbol} status={resp.status_code}")
            return None

        resp.raise_for_status()

        raw = resp.json().get("output", {}).get("stck_prpr", "")
        price_str = str(raw).replace(",", "").strip() if raw else ""
        if not price_str:
            return None

        try:
            return Decimal(price_str)
        except InvalidOperation:
            print(f"kis_price_parse_error: {symbol} raw={price_str!r}")
            return None

    def get_price(self, symbol: str) -> Optional[Decimal]:
        """
        현재가 조회. 401/403 시 토큰 재발급 후 1회 재시도.
        실패 시 None 반환.
        """
        try:
            self._ensure_token()
            price = self._fetch_price(symbol)
            if price is not None:
                return price

            # 401/403으로 None 반환됐을 가능성 → 토큰 재발급 후 재시도
            self.access_token = None
            self._ensure_token()
            return self._fetch_price(symbol)

        except Exception as e:
            print(f"kis_price_error: {symbol} {e}")
            return None
