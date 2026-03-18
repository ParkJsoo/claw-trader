from __future__ import annotations

import os
import redis
import requests
from decimal import Decimal

from exchange.base import ExchangeClient
from domain.models import (
    PlaceOrderRequest,
    PlaceOrderResult,
    OrderStatus,
    AccountSnapshot,
    OrderType,
    OrderSide,
)

_REDIS_TOKEN_KEY = "kis:access_token"
_REDIS_TOKEN_TTL = 23 * 3600  # 23시간 (KIS 토큰 유효기간 24시간)


class KisClient(ExchangeClient):
    def __init__(self):
        self.app_key = os.getenv("KIS_APP_KEY")
        self.app_secret = os.getenv("KIS_APP_SECRET")
        self.account_no = os.getenv("KIS_ACCOUNT_NO")
        self.product_code = os.getenv("KIS_ACCOUNT_PRODUCT_CODE")
        self.base_url = os.getenv(
            "KIS_BASE_URL",
            "https://openapi.koreainvestment.com:9443",
        )

        if not all([self.app_key, self.app_secret, self.account_no, self.product_code]):
            raise RuntimeError("KIS env is not fully set")

        self.session = requests.Session()
        self.access_token: str | None = None
        self._redis: redis.Redis | None = None
        try:
            redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
            self._redis = redis.from_url(redis_url, decode_responses=True)
        except Exception:
            pass

    def _ensure_token(self):
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
        self._refresh_token()

    def _refresh_token(self):
        url = f"{self.base_url}/oauth2/tokenP"

        try:
            resp = self.session.post(
                url,
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"KIS token refresh failed: {type(e).__name__}") from None

        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"KIS token refresh: unexpected response rt_cd={data.get('rt_cd')}")
        self.access_token = data["access_token"]

        # Redis에 캐시 저장
        if self._redis:
            try:
                self._redis.set(_REDIS_TOKEN_KEY, self.access_token, ex=_REDIS_TOKEN_TTL)
            except Exception:
                pass

    def _auth_headers(self, tr_id: str):
        self._ensure_token()

        return {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "content-type": "application/json",
        }

    def _request_with_retry(self, method: str, url: str, headers: dict, **kwargs):
        """API 호출 + 401 시 토큰 갱신 후 1회 재시도. 예외 시 시크릿 마스킹."""
        try:
            resp = getattr(self.session, method)(url, headers=headers, timeout=10, **kwargs)
        except Exception as e:
            raise RuntimeError(f"KIS API {method.upper()} request failed: {type(e).__name__}") from None
        if resp.status_code == 401:
            self.access_token = None
            if self._redis:
                try:
                    self._redis.delete(_REDIS_TOKEN_KEY)
                except Exception:
                    pass
            self._refresh_token()
            headers["authorization"] = f"Bearer {self.access_token}"
            try:
                resp = getattr(self.session, method)(url, headers=headers, timeout=10, **kwargs)
            except Exception as e:
                raise RuntimeError(f"KIS API {method.upper()} retry failed: {type(e).__name__}") from None
        resp.raise_for_status()
        return resp

    def ping(self) -> bool:
        try:
            self._ensure_token()
            return True
        except Exception:
            return False

    def get_account_snapshot(self) -> AccountSnapshot:
        self._ensure_token()

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"

        resp = self._request_with_retry(
            "get", url,
            headers=self._auth_headers("TTTC8434R"),
            params={
                "CANO": self.account_no.replace("-", "")[:8],
                "ACNT_PRDT_CD": self.product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "N",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "Y",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )

        data = resp.json()

        output2_list = data.get("output2") or [{}]
        output2 = output2_list[0] if output2_list else {}

        equity = Decimal(output2.get("tot_evlu_amt", "0"))
        cash = Decimal(output2.get("dnca_tot_amt", "0"))
        available = Decimal(output2.get("ord_psbl_cash") or output2.get("dnca_tot_amt", "0"))

        return AccountSnapshot(
            equity=equity,
            cash=cash,
            available_cash=available,
            currency="KRW",
        )

    def place_order(self, request: PlaceOrderRequest) -> PlaceOrderResult:
        self._ensure_token()

        qty_int = int(request.qty)

        if qty_int <= 0:
            return PlaceOrderResult(
                order_id="INVALID_QTY",
                status=OrderStatus.REJECTED,
            )

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"

        payload = {
            "CANO": self.account_no.replace("-", "")[:8],
            "ACNT_PRDT_CD": self.product_code,
            "PDNO": request.symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty_int),
            "ORD_UNPR": str(request.limit_price),
        }

        # KIS tr_id: TTTC0802U=매수, TTTC0801U=매도
        tr_id = "TTTC0802U" if request.side == OrderSide.BUY else "TTTC0801U"
        resp = self._request_with_retry(
            "post", url,
            headers=self._auth_headers(tr_id),
            json=payload,
        )

        data = resp.json()

        if data.get("rt_cd") != "0":
            return PlaceOrderResult(
                order_id=f"REJECTED:{data.get('msg_cd','UNKNOWN')}",
                status=OrderStatus.REJECTED,
                raw=data,
            )

        order_id = data["output"]["ODNO"]

        return PlaceOrderResult(
            order_id=str(order_id),
            status=OrderStatus.SUBMITTED,
            raw=data,
        )

    def get_kr_holdings(self) -> list[dict]:
        """KIS 잔고조회 output1 → 보유종목 리스트.
        Returns: [{"symbol": str, "qty": Decimal, "avg_price": Decimal}, ...]
        """
        resp = self._request_with_retry(
            "get",
            f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._auth_headers("TTTC8434R"),
            params={
                "CANO": self.account_no.replace("-", "")[:8],
                "ACNT_PRDT_CD": self.product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "N",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "Y",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        data = resp.json()
        result = []
        for item in (data.get("output1") or []):
            symbol = (item.get("pdno") or "").strip()
            qty_str = (item.get("hldg_qty") or "0").replace(",", "").strip()
            avg_str = (item.get("pchs_avg_pric") or "0").replace(",", "").strip()
            if not symbol:
                continue
            try:
                qty = Decimal(qty_str)
                avg_price = Decimal(avg_str)
                if qty > 0 and avg_price > 0:
                    result.append({"symbol": symbol, "qty": qty, "avg_price": avg_price})
            except Exception:
                continue
        return result

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_token()

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cancel"

        payload = {
            "CANO": self.account_no.replace("-", "")[:8],
            "ACNT_PRDT_CD": self.product_code,
            "ODNO": order_id,
            "ORD_DVSN": "00",
            "QTY_ALL_ORD_YN": "Y",
        }

        try:
            resp = self._request_with_retry(
                "post", url,
                headers=self._auth_headers("TTTC0803U"),
                json=payload,
            )
        except Exception as e:
            # 404: 주문 없음 (이미 체결/취소) → True 반환
            if "404" in str(e):
                return True
            raise

        data = resp.json()

        return data.get("rt_cd") == "0"
