"""Upbit 거래소 클라이언트.

환경변수:
    UPBIT_ACCESS_KEY  — Upbit Open API Access Key
    UPBIT_SECRET_KEY  — Upbit Open API Secret Key
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid

logger = logging.getLogger(__name__)
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

import jwt
import requests

from exchange.base import ExchangeClient
from domain.models import (
    AccountSnapshot,
    OrderSide,
    OrderStatus,
    OrderType,
    PlaceOrderRequest,
    PlaceOrderResult,
)

_BASE_URL = "https://api.upbit.com/v1"


class UpbitClient(ExchangeClient):
    def __init__(self) -> None:
        self.access_key = os.getenv("UPBIT_ACCESS_KEY")
        self.secret_key = os.getenv("UPBIT_SECRET_KEY")
        if not self.access_key or not self.secret_key:
            raise RuntimeError("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY not set")
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # 인증
    # ------------------------------------------------------------------

    def _auth_header(self, query: Optional[dict] = None) -> dict:
        """JWT 인증 헤더 생성."""
        payload: dict = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if query:
            query_str = urlencode(query)
            query_hash = hashlib.sha512(query_str.encode()).hexdigest()
            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        headers = self._auth_header(params)
        resp = self.session.get(f"{_BASE_URL}{path}", params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        headers = self._auth_header(body)
        resp = self.session.post(f"{_BASE_URL}{path}", json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, params: dict) -> dict:
        headers = self._auth_header(params)
        resp = self.session.delete(f"{_BASE_URL}{path}", params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # ExchangeClient 구현
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            resp = self.session.get(f"{_BASE_URL}/market/all", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def get_account_snapshot(self) -> AccountSnapshot:
        accounts = self._get("/accounts")
        krw_balance = Decimal("0")
        krw_locked = Decimal("0")
        for item in accounts:
            if item.get("currency") == "KRW":
                krw_balance = Decimal(str(item.get("balance", "0")))
                krw_locked = Decimal(str(item.get("locked", "0")))
                break
        available = krw_balance - krw_locked
        return AccountSnapshot(
            equity=krw_balance,
            cash=krw_balance,
            available_cash=available,
            currency="KRW",
        )

    def get_balances(self) -> list[dict]:
        """전체 잔고 반환 (KRW + 보유 코인)."""
        return self._get("/accounts")

    def get_ticker(self, market: str) -> dict:
        """현재가 조회. market 예: 'KRW-BTC'"""
        result = self._get("/ticker", {"markets": market})
        return result[0] if result else {}

    def get_krw_markets(self) -> list[str]:
        """KRW 마켓 전체 심볼 리스트 반환. 예: ['KRW-BTC', 'KRW-ETH', ...]"""
        resp = self.session.get(f"{_BASE_URL}/market/all", params={"is_details": "false"}, timeout=10)
        resp.raise_for_status()
        return [m["market"] for m in resp.json() if m["market"].startswith("KRW-")]

    def get_tickers(self, markets: list[str]) -> list[dict]:
        """복수 종목 현재가 일괄 조회."""
        if not markets:
            return []
        resp = self.session.get(
            f"{_BASE_URL}/ticker",
            params={"markets": ",".join(markets)},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_volume_rank(self, top_n: int = 30, min_price: float = 10.0) -> list[dict]:
        """KRW 마켓 24h 거래대금 상위 N개 반환.

        Returns: [{"symbol": "KRW-BTC", "price": 104946000, "change_rate": -0.0148, "volume_krw": ...}, ...]
        """
        markets = self.get_krw_markets()
        tickers = self.get_tickers(markets)

        result = []
        for t in tickers:
            price = float(t.get("trade_price", 0))
            if price < min_price:
                continue
            result.append({
                "symbol": t["market"],
                "price": price,
                "change_rate": float(t.get("signed_change_rate", 0)),
                "volume_krw": float(t.get("acc_trade_price_24h", 0)),
                "volume_coin": float(t.get("acc_trade_volume_24h", 0)),
            })

        result.sort(key=lambda x: -x["volume_krw"])
        return result[:top_n]

    def place_order(self, request: PlaceOrderRequest) -> PlaceOrderResult:
        """주문 실행.

        request.symbol 형식: 'KRW-BTC' (Upbit market 코드)
        """
        side = "bid" if request.side == OrderSide.BUY else "ask"
        body: dict = {
            "market": request.symbol,
            "side": side,
        }

        if request.order_type == OrderType.MARKET:
            if request.side == OrderSide.BUY:
                # 시장가 매수: KRW 금액 지정
                body["ord_type"] = "price"
                body["price"] = str(request.qty)
            else:
                # 시장가 매도: 수량 지정
                body["ord_type"] = "market"
                body["volume"] = str(request.qty)
        else:
            # 지정가
            body["ord_type"] = "limit"
            body["volume"] = str(request.qty)
            body["price"] = str(request.limit_price)

        resp = self._post("/orders", body)
        order_id = resp.get("uuid", "")
        return PlaceOrderResult(
            order_id=order_id,
            status=OrderStatus.SUBMITTED,
            raw=resp,
        )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._delete("/order", {"uuid": order_id})
            return True
        except Exception as e:
            logger.warning("cancel_order failed: order_id=%s error=%s", order_id, e)
            return False

    def get_order(self, order_id: str) -> dict:
        """주문 상태 조회."""
        return self._get("/order", {"uuid": order_id})
