from __future__ import annotations

import os
import redis
import requests
import time
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
_REDIS_TOKEN_BLOCKED_KEY = "kis:token_refresh_blocked"
_REDIS_TOKEN_BLOCKED_TTL = 3600  # 403 발생 시 1시간 재시도 차단
_REDIS_TOKEN_RETRY_KEY = "kis:token_refresh_retry_after"
_REDIS_TOKEN_RETRY_TTL = 30  # 일시적 5xx/네트워크 실패 시 30초 재시도 지연
_REQUEST_RETRY_ATTEMPTS = 3
_REQUEST_RETRY_BASE_SEC = 0.3


def _kr_tick_size(price: int) -> int:
    """KIS 국내주식 호가단위 반환."""
    if price < 2000:    return 1
    if price < 5000:    return 5
    if price < 20000:   return 10
    if price < 50000:   return 50
    if price < 200000:  return 100
    if price < 500000:  return 500
    return 1000


def _round_to_tick(price: Decimal) -> Decimal:
    """KR 지정가 주문 가격을 호가단위에 맞게 내림 처리."""
    p = int(price)
    tick = _kr_tick_size(p)
    return Decimal(p - (p % tick))


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
        self._token_fetched_at: float = 0.0  # Unix timestamp, 토큰 발급 시각
        self._redis: redis.Redis | None = None
        try:
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                self._redis = redis.from_url(redis_url, decode_responses=True)
        except Exception:
            pass

    def _ensure_token(self):
        import time as _time
        # 22시간 경과 시 선제 갱신 (KIS 토큰 유효기간 24h, Redis TTL 23h)
        if self.access_token and _time.time() - self._token_fetched_at < 22 * 3600:
            return
        # Redis 캐시 조회 (다른 프로세스가 이미 갱신했을 수 있음)
        if self._redis:
            try:
                cached = self._redis.get(_REDIS_TOKEN_KEY)
                if cached:
                    self.access_token = cached
                    # Redis TTL 기반으로 발급 시각 추정
                    ttl = self._redis.ttl(_REDIS_TOKEN_KEY)
                    self._token_fetched_at = _time.time() - (_REDIS_TOKEN_TTL - max(ttl, 0))
                    return
            except Exception:
                pass
            # 403 rate limit 쿨다운 중이면 재시도 차단
            try:
                blocked_ttl = self._redis.ttl(_REDIS_TOKEN_BLOCKED_KEY)
                if blocked_ttl > 0:
                    raise RuntimeError(f"KIS token refresh blocked (rate limited, retry in {blocked_ttl}s)")
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
            if resp.status_code == 403:
                # 다른 프로세스가 이미 발급 — Redis에서 재조회
                if self._redis:
                    try:
                        cached = self._redis.get(_REDIS_TOKEN_KEY)
                        if cached:
                            self.access_token = cached.decode() if isinstance(cached, bytes) else cached
                            return
                    except Exception:
                        pass
                    # 재조회도 실패 → 1시간 쿨다운 등록 (30초마다 무한 재시도 방지)
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
        import time as _time
        self.access_token = data["access_token"]
        self._token_fetched_at = _time.time()

        # Redis에 캐시 저장
        if self._redis:
            try:
                self._redis.set(_REDIS_TOKEN_KEY, self.access_token, ex=_REDIS_TOKEN_TTL)
                self._redis.delete(_REDIS_TOKEN_RETRY_KEY)
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
        """API 호출 + 인증 갱신 + 일시적 5xx 재시도."""
        auth_refreshed = False
        req_headers = dict(headers)
        last_error: str | None = None

        for attempt in range(1, _REQUEST_RETRY_ATTEMPTS + 1):
            try:
                resp = getattr(self.session, method)(url, headers=req_headers, timeout=10, **kwargs)
            except Exception as e:
                last_error = f"KIS API {method.upper()} request failed: {type(e).__name__}"
                if attempt < _REQUEST_RETRY_ATTEMPTS:
                    time.sleep(_REQUEST_RETRY_BASE_SEC * attempt)
                    continue
                raise RuntimeError(last_error) from None

            if resp.status_code in (401, 403) and not auth_refreshed:
                self.access_token = None
                self._token_fetched_at = 0.0
                if self._redis:
                    try:
                        self._redis.delete(_REDIS_TOKEN_KEY)
                    except Exception:
                        pass
                self._refresh_token()
                req_headers["authorization"] = f"Bearer {self.access_token}"
                auth_refreshed = True
                continue

            if resp.status_code >= 500 and attempt < _REQUEST_RETRY_ATTEMPTS:
                time.sleep(_REQUEST_RETRY_BASE_SEC * attempt)
                continue

            try:
                resp.raise_for_status()
            except Exception as e:
                last_error = f"KIS API {method.upper()} status={resp.status_code}: {type(e).__name__}"
                raise RuntimeError(last_error) from None
            return resp

        raise RuntimeError(last_error or f"KIS API {method.upper()} request failed")

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

        def _dec(val: str) -> Decimal:
            return Decimal(str(val).replace(",", "") or "0")

        equity = _dec(output2.get("tot_evlu_amt", "0"))
        cash = _dec(output2.get("dnca_tot_amt", "0"))
        available = _dec(output2.get("ord_psbl_cash") or output2.get("prvs_rcdl_excc_amt") or output2.get("dnca_tot_amt", "0"))

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

        # KR 지정가 주문: 호가단위에 맞게 내림 처리 (KisClient는 KR 전용)
        ord_unpr = "0"
        if request.limit_price is not None:
            ord_unpr = str(int(_round_to_tick(request.limit_price)))

        payload = {
            "CANO": self.account_no.replace("-", "")[:8],
            "ACNT_PRDT_CD": self.product_code,
            "PDNO": request.symbol,
            "ORD_DVSN": "00" if request.limit_price is not None else "01",
            "ORD_QTY": str(qty_int),
            "ORD_UNPR": ord_unpr,
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
            print(f"KIS order rejected: msg_cd={data.get('msg_cd')} msg1={data.get('msg1')} symbol={request.symbol} side={request.side} qty={qty_int} price={payload['ORD_UNPR']}", flush=True)
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

    def get_volume_rank(
        self,
        price_min: int = 1000,
        price_max: int = 50000,
        min_vol: int = 100000,
    ) -> list[dict]:
        """거래량 순위 상위 종목 조회 (FHPST01740000).

        Returns: [{"symbol": str, "name": str, "price": int, "volume": int}, ...]
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        resp = self._request_with_retry(
            "get",
            url,
            headers=self._auth_headers("FHPST01740000"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": str(price_min),
                "FID_INPUT_PRICE_2": str(price_max),
                "FID_VOL_CNT": str(min_vol),
                "FID_INPUT_DATE_1": "",
            },
        )
        data = resp.json()
        result = []
        for item in (data.get("output") or []):
            symbol = (item.get("mksc_shrn_iscd") or "").strip()
            if not symbol:
                continue
            try:
                result.append({
                    "symbol": symbol,
                    "name": item.get("hts_kor_isnm", ""),
                    "price": int(item.get("stck_prpr", "0").replace(",", "") or 0),
                    "volume": int(item.get("acml_vol", "0").replace(",", "") or 0),
                })
            except (ValueError, TypeError):
                continue
        return result

    def get_fluctuation_rank(
        self,
        price_min: int = 1000,
        price_max: int = 50000,
        min_rate: float = 1.0,
    ) -> list[dict]:
        """등락률(상승률) 순위 상위 종목 조회 (FHPST01700000).

        Returns: [{"symbol": str, "name": str, "price": int, "change_rate": float}, ...]
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"
        resp = self._request_with_retry(
            "get",
            url,
            headers=self._auth_headers("FHPST01700000"),
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": "0000",
                "fid_rank_sort_cls_code": "0",
                "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "1",
                "fid_input_price_1": str(price_min),
                "fid_input_price_2": str(price_max),
                "fid_vol_cnt": "100000",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",
                "fid_rsfl_rate1": str(min_rate),
                "fid_rsfl_rate2": "30",
            },
        )
        data = resp.json()
        result = []
        for item in (data.get("output") or []):
            # 실제 응답 필드명: stck_shrn_iscd (등락률 API)
            symbol = (item.get("stck_shrn_iscd") or "").strip()
            if not symbol:
                continue
            try:
                result.append({
                    "symbol": symbol,
                    "name": item.get("hts_kor_isnm", ""),
                    "price": int(item.get("stck_prpr", "0").replace(",", "") or 0),
                    "change_rate": float(item.get("prdy_ctrt", "0") or 0),
                })
            except (ValueError, TypeError):
                continue
        return result

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_token()

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"

        payload = {
            "CANO": self.account_no.replace("-", "")[:8],
            "ACNT_PRDT_CD": self.product_code,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }

        try:
            resp = self._request_with_retry(
                "post", url,
                headers=self._auth_headers("TTTC0803U"),
                json=payload,
            )
        except Exception as e:
            # status=404: 주문 없음 (이미 체결/취소) → False 반환
            if "status=404" in str(e):
                return False
            raise

        data = resp.json()

        return data.get("rt_cd") == "0"
