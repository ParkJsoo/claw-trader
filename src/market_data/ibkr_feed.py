from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Optional

from ib_insync import IB, Stock

_IBKR_BASE_BACKOFF_SEC = 2.0
_IBKR_MAX_BACKOFF_SEC = 60.0


class IbkrFeed:
    """
    IBKR ib_insync 기반 현재가 스냅샷 피드.
    주문 실행 클라이언트(client_id=11)와 별도 연결 사용.
    env: IBKR_MD_CLIENT_ID (기본값 12)

    - reqMarketDataType(4): Delayed Frozen — 장외 시간에도 마지막 가격 반환 (Error 10089 해결)
    - reconnect 지수 백오프: 실패 시 최대 60s 대기
    """

    def __init__(self):
        self.host = os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = int(os.getenv("IBKR_PORT", "4001"))
        self.client_id = int(os.getenv("IBKR_MD_CLIENT_ID", "12"))
        self.currency = os.getenv("IBKR_CURRENCY", "USD")
        self.ib = IB()
        self._reconnect_failures = 0
        self._last_connect_attempt = 0.0

    def _get_backoff_sec(self) -> float:
        return min(_IBKR_BASE_BACKOFF_SEC ** self._reconnect_failures, _IBKR_MAX_BACKOFF_SEC)

    def _connect(self) -> bool:
        if self.ib.isConnected():
            return True

        now = time.time()
        if self._reconnect_failures > 0:
            elapsed = now - self._last_connect_attempt
            wait = self._get_backoff_sec()
            if elapsed < wait:
                return False

        self._last_connect_attempt = now
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=2)
            if self.ib.isConnected():
                self.ib.reqMarketDataType(4)  # Delayed Frozen — live 구독 활성화 전까지 유지
                if self._reconnect_failures > 0:
                    print(
                        f"ibkr_feed: reconnected after {self._reconnect_failures} failures",
                        flush=True,
                    )
                self._reconnect_failures = 0
                return True
            self._reconnect_failures += 1
            print(
                f"ibkr_feed: connect_failed attempt={self._reconnect_failures} "
                f"next_backoff={self._get_backoff_sec():.0f}s",
                flush=True,
            )
            return False
        except Exception as e:
            self._reconnect_failures += 1
            print(
                f"ibkr_feed: connect_error={type(e).__name__} "
                f"attempt={self._reconnect_failures} next_backoff={self._get_backoff_sec():.0f}s",
                flush=True,
            )
            return False

    def get_price(self, symbol: str) -> Optional[Decimal]:
        """
        스냅샷 현재가 조회.
        last > 0이면 last, 아니면 close 사용.
        실패 시 None 반환 (updater에서 무시됨).
        """
        if not self._connect():
            return None
        try:
            contract = Stock(symbol, "SMART", self.currency)
            self.ib.qualifyContracts(contract)
            [ticker] = self.ib.reqTickers(contract)
            price = ticker.last
            if price is None or price <= 0:
                price = ticker.close
            if price is not None and price > 0:
                return Decimal(str(price))
        except Exception:
            pass
        return None
