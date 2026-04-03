"""consensus_signal_runner — Phase 10

dual-LLM 합의 결과를 읽어 최소 prefilter 통과 시
전체 Signal 객체를 생성하고 claw:signal:queue에 push한다.

책임 범위:
  - dual consensus 확인 (claude_emit == 1 AND qwen_emit == 1 AND 방향 일치)
  - entry prefilter (ret_5m > 0, range_5m > 0.004)
  - 데이터 무결성 확인
  - Signal 정규화 (Pydantic 검증 포함)
  - claw:signal:queue enqueue
  - 감사 로그 / 통계

책임 외:
  - session gating (StrategyEngine)
  - cooldown / re-entry (StrategyEngine)
  - position 존재 여부 (RiskEngine)
  - max_positions / daily loss (RiskEngine)
  - 주문 실행 (OrderExecutor)
"""
from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

import redis

from domain.models import Signal, SignalEntry, SignalStop
from utils.redis_helpers import parse_watchlist, load_watchlist, today_kst, is_market_hours, is_paused

# H1: 잔고 비율 기반 size_cash 계산
_SIZE_CASH_PCT_KR = float(os.getenv("CONSENSUS_KR_SIZE_CASH_PCT", "0.30"))
_SIZE_CASH_PCT_US = float(os.getenv("CONSENSUS_US_SIZE_CASH_PCT", "0.30"))
_SIZE_CASH_PCT_COIN = float(os.getenv("CONSENSUS_COIN_SIZE_CASH_PCT", "0.30"))

# KisClient / IbkrClient 싱글톤 캐시 (프로세스 재시작 시 초기화)
_client_cache: dict[str, object] = {}

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "consensus:runner:lock"
_LOCK_TTL = 120  # seconds

_POLL_SEC = float(os.getenv("CONSENSUS_POLL_SEC", "30"))

# momentum breakout prefilter: 5분 급등 최소 기준
_MIN_SURGE_5M = float(os.getenv("MB_MIN_SURGE_5M", "0.020"))       # COIN 2.0% 급등 최소
_MIN_SURGE_5M_KR = float(os.getenv("MB_MIN_SURGE_5M_KR", "0.030"))  # KR 3.0% (노이즈 필터링)
_MIN_RANGE_5M = float(os.getenv("MB_MIN_RANGE_5M", "0.004"))

# Phase 11: symbol-level cooldown (같은 종목 N초 내 재emit 방지)
_SYMBOL_COOLDOWN_SEC = int(os.getenv("CONSENSUS_SYMBOL_COOLDOWN_SEC", "180"))

# momentum breakout prefilter: 1분 급등 최소 기준
_MIN_RET_1M = float(os.getenv("MB_MIN_RET_1M", "0.002"))  # 0.2%

# Phase 17: 신호 품질 강화
_MIN_RET_15M = float(os.getenv("CONSENSUS_MIN_RET_15M", "0.0"))  # 15분 추세: 0 이상이어야 함
_VOLUME_SURGE_RATIO = float(os.getenv("CONSENSUS_VOLUME_SURGE_RATIO", "1.5"))  # 거래량 배수
_VOLUME_LOOKBACK_DAYS = int(os.getenv("VOLUME_LOOKBACK_DAYS", "7"))  # 5 → 7 (주말 포함)
_CLAUDE_ONLY = os.getenv("EXECUTION_MODE", "dual").lower() == "claude_only"  # Qwen 무시

_BULLISH_THRESHOLD = float(os.getenv("REGIME_BULLISH_THRESHOLD", "0.30"))  # bearish 비율 < 30% → bullish

# Type B: 추세 탑승 신호 파라미터
_TYPE_B_POLL_SEC = float(os.getenv("TYPE_B_POLL_SEC", "300"))           # 5분마다 체크
_TYPE_B_COOLDOWN_SEC = int(os.getenv("TYPE_B_COOLDOWN_SEC", "14400"))   # 4시간 쿨다운
_TYPE_B_MIN_CHANGE_RATE = float(os.getenv("TYPE_B_MIN_CHANGE_RATE", "0.05"))   # 전일대비 +5%
_TYPE_B_MIN_VOL_KRW = float(os.getenv("TYPE_B_MIN_VOL_KRW", "10000000000"))    # 24h 거래대금 100억
_TYPE_B_NEAR_HIGH_RATIO = float(os.getenv("TYPE_B_NEAR_HIGH_RATIO", "0.97"))   # 당일 고점 -3% 이내
_TYPE_B_MIN_RET_5M = float(os.getenv("TYPE_B_MIN_RET_5M", "0.005"))            # 5분 ret 0.5% 이상

_AUDIT_TTL = 7 * 86400   # 7일
_STATS_TTL = 30 * 86400


# ---------------------------------------------------------------------------
# 동적 stop/take pct 계산
# ---------------------------------------------------------------------------

def _dynamic_pcts(range_5m: float, market: str = "KR") -> tuple:
    """모멘텀 브레이크아웃: 시장별 stop/take 분기."""
    if market == "COIN":
        stop = Decimal(os.getenv("COIN_EXIT_STOP_LOSS_PCT", "0.040"))   # -4.0% (COIN 전용)
        take = Decimal(os.getenv("COIN_EXIT_TAKE_PROFIT_PCT", "0.200")) # +20.0% (Big Mover Ride)
    else:
        stop = Decimal("0.025")  # -2.5%
        take = Decimal("0.050")  # +5.0%
    return stop, take


# ---------------------------------------------------------------------------
# KR 호가 단위 정규화 (내림)
# ---------------------------------------------------------------------------

def normalize_kr_price_tick(price: Decimal) -> Decimal:
    """KR 주식 호가 단위에 맞게 내림 처리."""
    p = int(price)
    if p < 1_000:
        tick = 1
    elif p < 5_000:
        tick = 5
    elif p < 10_000:
        tick = 10
    elif p < 50_000:
        tick = 50
    elif p < 100_000:
        tick = 100
    elif p < 500_000:
        tick = 500
    else:
        tick = 1_000
    return Decimal((p // tick) * tick)


def _normalize_price(market: str, price: Decimal) -> Decimal:
    """market에 따라 가격 정규화: KR=호가단위, COIN=원본 유지, US=소수점 2자리."""
    if market == "KR":
        return normalize_kr_price_tick(price)
    if market == "COIN":
        return price  # 업비트 코인 가격은 원본 그대로 사용
    return price.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 로깅 헬퍼
# ---------------------------------------------------------------------------

def _get_client(market: str):
    """KisClient/IbkrClient/UpbitClient 싱글톤 캐시."""
    if market not in _client_cache:
        try:
            if market == "KR":
                from exchange.kis.client import KisClient
                _client_cache[market] = KisClient()
            elif market == "COIN":
                from exchange.upbit.client import UpbitClient
                _client_cache[market] = UpbitClient()
            else:
                from exchange.ibkr.client import IbkrClient
                _client_cache[market] = IbkrClient()
        except Exception as e:
            _log("client_init_failed", market=market, error=str(e))
            return None
    return _client_cache.get(market)


def _calc_size_cash(market: str, current_price: Decimal) -> Decimal:
    """H1: 잔고 비율 기반 size_cash 계산. 실패 시 1주(current_price) fallback."""
    if market == "KR":
        pct = _SIZE_CASH_PCT_KR
    elif market == "COIN":
        pct = _SIZE_CASH_PCT_COIN
    else:
        pct = _SIZE_CASH_PCT_US
    try:
        client = _get_client(market)
        if client is None:
            return current_price  # fallback: 1주
        snapshot = client.get_account_snapshot()
        available = snapshot.available_cash
        if available <= 0:
            return current_price  # fallback: 1주
        size_cash = Decimal(str(float(available) * pct))
        # qty = size_cash / price 가 1 미만이면 1주 fallback
        # COIN(Upbit)은 소수점 매수 지원 → 1주 fallback 불필요
        if market != "COIN" and size_cash < current_price:
            return current_price
        return size_cash
    except Exception as e:
        _log("size_cash_error", market=market, error=str(e), exc_type=type(e).__name__)
        if market == "COIN":
            return Decimal("5000")  # Upbit 최소 주문금액
        min_size = Decimal("100000") if market == "KR" else Decimal("100")
        return max(current_price, min_size)


def _log(event: str, **kwargs) -> None:
    parts = [event] + [f"{k}={v}" for k, v in kwargs.items()]
    print(f"consensus: {' '.join(parts)}", flush=True)


# ---------------------------------------------------------------------------
# Redis 유틸
# ---------------------------------------------------------------------------

def _decode(v) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v) if v is not None else ""


def _hgetall_str(r, key: str) -> dict:
    """hgetall 결과를 str:str dict로 반환."""
    raw = r.hgetall(key)
    if not raw:
        return {}
    return {_decode(k): _decode(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# 감사 / 통계
# ---------------------------------------------------------------------------

def _save_audit(r, market: str, signal: Signal, ret_5m: float, range_5m: float) -> None:
    today = today_kst()
    payload = {
        "signal_id": signal.signal_id,
        "market": market,
        "symbol": signal.symbol,
        "ts": signal.ts,
        "direction": signal.direction,
        "entry_price": str(signal.entry.price),
        "stop_price": str(signal.stop.price),
        "ret_5m": str(ret_5m),
        "range_5m": str(range_5m),
        "source": "consensus_signal_runner",
    }
    audit_key = f"consensus:audit:{market}:{signal.signal_id}"
    r.set(audit_key, json.dumps(payload), ex=_AUDIT_TTL)

    # 일별 통계 카운터
    stats_key = f"consensus:stats:{market}:{today}"
    r.hincrby(stats_key, "candidate", 1)
    r.expire(stats_key, _STATS_TTL)

    # 일별 candidate 생성 수
    daily_key = f"consensus:daily_count:{market}:{today}"
    r.incr(daily_key)
    r.expire(daily_key, _STATS_TTL)


def _record_reject(r, market: str, reason_code: str) -> None:
    today = today_kst()
    stats_key = f"consensus:stats:{market}:{today}"
    r.hincrby(stats_key, reason_code, 1)
    r.expire(stats_key, _STATS_TTL)


def _get_dates_for_news(today: str) -> list:
    """오늘과 어제 날짜 반환 (뉴스 조회용)."""
    try:
        dt = datetime.strptime(today, "%Y%m%d")
        yesterday = (dt - timedelta(days=1)).strftime("%Y%m%d")
        return [today, yesterday]
    except ValueError:
        return [today]


def _has_positive_news(r, market: str, symbol: str) -> bool:
    """오늘/어제 뉴스 중 positive+high/medium 뉴스가 있으면 True."""
    today = today_kst()
    for date_str in _get_dates_for_news(today):
        news_key = f"news:symbol:{market}:{symbol}:{date_str}"
        items = r.lrange(news_key, 0, 9)
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                sentiment = d.get("sentiment", "").lower()
                impact = d.get("impact", "").lower()
                if sentiment == "positive" and impact in ("high", "medium"):
                    return True
            except Exception:
                continue
    return False


def _get_news_score(r, market: str, symbol: str) -> str:
    """오늘/어제 뉴스 최고 임팩트 반환: 'high', 'medium', 'none'."""
    today = today_kst()
    for date_str in _get_dates_for_news(today):
        news_key = f"news:symbol:{market}:{symbol}:{date_str}"
        items = r.lrange(news_key, 0, 9)
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                sentiment = d.get("sentiment", "").lower()
                impact = d.get("impact", "").lower()
                if sentiment == "positive" and impact == "high":
                    return "high"
            except Exception:
                continue
        for raw in items:
            try:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                sentiment = d.get("sentiment", "").lower()
                impact = d.get("impact", "").lower()
                if sentiment == "positive" and impact == "medium":
                    return "medium"
            except Exception:
                continue
    return "none"


def _has_volume_surge(r, market: str, symbol: str) -> bool:
    """오늘 거래량이 최근 LOOKBACK일 평균 대비 SURGE_RATIO 이상이면 True.
    데이터 부족 시 True 반환 (permissive default)."""
    today = today_kst()
    today_raw = r.get(f"vol:{market}:{symbol}:{today}")
    if not today_raw:
        return True  # 데이터 없으면 통과 (현재 장 초반 등)
    try:
        today_vol = int(today_raw.decode() if isinstance(today_raw, bytes) else today_raw)
    except (ValueError, TypeError):
        return True

    vols = []
    for i in range(1, _VOLUME_LOOKBACK_DAYS + 1):
        try:
            dt = datetime.strptime(today, "%Y%m%d")
            past_date = (dt - timedelta(days=i)).strftime("%Y%m%d")
            raw = r.get(f"vol:{market}:{symbol}:{past_date}")
            if raw:
                v = int(raw.decode() if isinstance(raw, bytes) else raw)
                if v > 0:
                    vols.append(v)
        except Exception:
            continue

    if len(vols) < 3:
        return True  # 과거 데이터 부족 → 통과
    avg_vol = sum(vols) / len(vols)
    return today_vol >= avg_vol * _VOLUME_SURGE_RATIO


def _get_regime(r, market: str, watchlist: list) -> str:
    """워치리스트 mark_hist 기반 실시간 ret_5m으로 regime 판별.

    AI eval features_json 대신 mark_hist를 사용해 stale 데이터 문제 방지.
    Returns: "bearish" | "bullish" | "neutral"
    """
    if not watchlist:
        return "neutral"

    now_ms = int(time.time() * 1000)
    target_ms = now_ms - 5 * 60 * 1000  # 5분 전
    stale_threshold_ms = 5 * 60 * 1000  # 최신 시세가 5분 이상 오래됐으면 skip

    bearish = 0
    total = 0
    for symbol in watchlist:
        try:
            entries = r.lrange(f"mark_hist:{market}:{symbol}", 0, 20)
            if not entries:
                continue
            ts_str, price_str = entries[0].decode().split(":", 1)
            latest_ts = int(ts_str)
            latest_price = float(price_str)

            if now_ms - latest_ts > stale_threshold_ms:
                continue  # 시세 오래됨 — 무시

            past_price = None
            for entry in entries[1:]:
                t, p = entry.decode().split(":", 1)
                if int(t) <= target_ms:
                    past_price = float(p)
                    break

            if past_price is None or past_price == 0:
                continue

            ret_5m = (latest_price - past_price) / past_price
            total += 1
            if ret_5m < 0:
                bearish += 1
        except Exception:
            continue

    if total < 3:
        return "neutral"
    ratio = bearish / total
    if ratio > 0.6:
        return "bearish"
    if ratio < _BULLISH_THRESHOLD:
        return "bullish"
    return "neutral"


def _is_bearish_regime(r, market: str, watchlist: list) -> bool:
    """backward compat wrapper."""
    return _get_regime(r, market, watchlist) == "bearish"


def _get_live_ret_5m(r, market: str, symbol: str) -> Optional[tuple]:
    """mark_hist에서 실시간 ret_5m, latest_price 계산.

    Returns: (ret_5m: float, latest_price: float) or None if insufficient data.
    최신 시세가 2분 이상 오래됐으면 None 반환 (stale 거부).
    """
    now_ms = int(time.time() * 1000)
    target_ms = now_ms - 5 * 60 * 1000  # 5분 전
    stale_threshold_ms = 2 * 60 * 1000  # 최신 시세 2분 초과 시 skip

    entries = r.lrange(f"mark_hist:{market}:{symbol}", 0, 30)
    if not entries:
        return None
    try:
        ts_str, price_str = entries[0].decode().split(":", 1)
        latest_ts = int(ts_str)
        latest_price = float(price_str)
    except Exception:
        return None

    if now_ms - latest_ts > stale_threshold_ms:
        return None  # 시세 2분 이상 오래됨

    past_price = None
    for entry in entries[1:]:
        try:
            t, p = entry.decode().split(":", 1)
            if int(t) <= target_ms:
                past_price = float(p)
                break
        except Exception:
            continue

    if past_price is None or past_price == 0:
        return None

    ret_5m = (latest_price - past_price) / past_price
    return ret_5m, latest_price


# ---------------------------------------------------------------------------
# 핵심 처리: 심볼 1개
# ---------------------------------------------------------------------------

def run_once(market: str, symbol: str, r) -> Optional[dict]:
    """
    dual eval Redis 결과를 읽어 candidate Signal을 생성하고 queue에 push.

    Returns:
        signal dict (push 성공 시) or None (reject / 오류 시)
    """
    # 1. eval 결과 읽기 (Claude only)
    claude = _hgetall_str(r, f"ai:dual:last:claude:{market}:{symbol}")

    if not claude:
        return None  # Claude 결과 없음 — cold start

    # 2. dedup: 이미 이 ts_ms로 처리한 결과면 스킵 (중복 push 방지)
    c_ts_ms = claude.get("ts_ms", "")
    seen_key = f"consensus:seen:{market}:{symbol}:{c_ts_ms}"
    if not r.set(seen_key, "1", nx=True, ex=max(int(_POLL_SEC * 6), 60)):
        return None  # 이미 처리한 eval 결과

    # 2-b. symbol-level cooldown: consensus 판단 전 체크 (불필요한 처리 방지)
    cooldown_key = f"consensus:symbol_cooldown:{market}:{symbol}"
    if r.exists(cooldown_key):
        _log("runner.reject.cooldown", symbol=symbol, cooldown_sec=_SYMBOL_COOLDOWN_SEC)
        _record_reject(r, market, "reject_cooldown")
        return None

    # 2-c. 당일 stop_loss 이후 재진입 차단 (완화: 2시간 경과 + 손절가 대비 +3% 회복 시 허용)
    today = today_kst()
    daily_stop_key = f"claw:daily_stop:{market}:{symbol}:{today}"
    if r.exists(daily_stop_key):
        allow_reentry = False
        try:
            if r.type(daily_stop_key).decode() == "hash":
                ds = _hgetall_str(r, daily_stop_key)
                stop_ts = float(ds.get("stop_ts", 0))
                elapsed = time.time() - stop_ts
                if elapsed >= 7200 and ds.get("stop_price"):
                    ds_stop_price = float(ds["stop_price"])
                    live_check = _get_live_ret_5m(r, market, symbol)
                    if live_check is not None and ds_stop_price > 0:
                        recovery = (live_check[1] - ds_stop_price) / ds_stop_price
                        if recovery >= 0.03:
                            allow_reentry = True
                            _log("runner.daily_stop.reentry_allowed", symbol=symbol,
                                 elapsed_h=f"{elapsed/3600:.1f}h",
                                 recovery_pct=f"{recovery*100:.1f}%")
        except Exception:
            pass
        if not allow_reentry:
            _log("runner.reject.daily_stop", symbol=symbol, market=market)
            _record_reject(r, market, "reject_daily_stop")
            return None

    # 2-d. 종목별 일일 진입 상한 (max 3회)
    symbol_daily_key = f"consensus:symbol_daily:{market}:{symbol}:{today}"
    _symbol_daily_cap = int(os.getenv("CONSENSUS_SYMBOL_DAILY_CAP", "3"))
    symbol_count = int(r.get(symbol_daily_key) or 0)
    if symbol_count >= _symbol_daily_cap:
        _log("runner.reject.symbol_daily_cap", symbol=symbol, market=market, count=symbol_count)
        _record_reject(r, market, "reject_symbol_daily_cap")
        return None

    # 3. 데이터 무결성: features_json 파싱
    try:
        c_features = json.loads(claude.get("features_json") or "{}")
    except (json.JSONDecodeError, Exception) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"features_json parse error: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 4. Claude emit 확인 (bad news filter: emit=False → bad news detected)
    c_emit = claude.get("emit") == "1"
    if not c_emit:
        _log("runner.reject.bad_news", symbol=symbol, reason=claude.get("reason", "")[:60])
        _record_reject(r, market, "reject_bad_news")
        return None

    # 방향 확인
    c_dir = claude.get("direction", "")
    if not c_dir:
        _log("runner.reject.direction_missing", symbol=symbol)
        _record_reject(r, market, "reject_direction_missing")
        return None

    # Phase 10: LONG 방향만 처리
    if c_dir != "LONG":
        _log("runner.reject.direction_not_long", symbol=symbol, direction=c_dir)
        _record_reject(r, market, "reject_direction_not_long")
        return None

    # 5. prefilter: live ret_5m (mark_hist 직접 계산 — stale eval 방지)
    live = _get_live_ret_5m(r, market, symbol)
    if live is None:
        # mark_hist 데이터 없거나 stale → features_json fallback
        try:
            ret_5m_raw = c_features.get("ret_5m")
            if ret_5m_raw is None:
                raise ValueError("ret_5m missing")
            ret_5m = float(ret_5m_raw)
            live_price = None
        except (TypeError, ValueError) as e:
            _log("runner.reject.invalid_payload", symbol=symbol, reason=str(e))
            _record_reject(r, market, "reject_invalid_payload")
            return None
    else:
        ret_5m, live_price_float = live
        live_price = Decimal(str(live_price_float))

    try:
        range_5m_raw = c_features.get("range_5m")
        if range_5m_raw is None:
            raise ValueError("range_5m missing in features_json")
        range_5m = float(range_5m_raw)
    except (TypeError, ValueError) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=str(e))
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # momentum breakout: 지금 이 순간에도 5분 상승폭이 충분해야 함
    surge_threshold = _MIN_SURGE_5M_KR if market == "KR" else _MIN_SURGE_5M
    if ret_5m <= surge_threshold:
        _log("runner.reject.prefilter_ret_5m", symbol=symbol, ret_5m=ret_5m)
        _record_reject(r, market, "reject_prefilter_ret_5m")
        return None

    if range_5m <= _MIN_RANGE_5M:
        _log("runner.reject.prefilter_range_5m", symbol=symbol, range_5m=range_5m)
        _record_reject(r, market, "reject_prefilter_range_5m")
        return None

    # 5-a2. ret_1m 최소 기준 프리필터
    try:
        ret_1m_raw = c_features.get("ret_1m")
        if ret_1m_raw is not None:
            ret_1m = float(ret_1m_raw)
            if ret_1m < _MIN_RET_1M:
                _log("runner.reject.prefilter_ret_1m", symbol=symbol, ret_1m=ret_1m)
                _record_reject(r, market, "reject_prefilter_ret_1m")
                return None
    except (TypeError, ValueError):
        pass  # ret_1m 없거나 파싱 실패 → 통과 (permissive)

    # 5-b. Volume surge 필터 (KR + COIN — US는 데이터 없음)
    if market in ("KR", "COIN") and not _has_volume_surge(r, market, symbol):
        _log("runner.reject.volume_no_surge", symbol=symbol)
        _record_reject(r, market, "reject_volume_no_surge")
        return None

    # 6. current_price: live_price 우선, fallback features_json
    if live_price is not None:
        current_price = live_price
    else:
        try:
            price_raw = c_features.get("current_price")
            if not price_raw:
                raise ValueError("current_price missing in features_json")
            current_price = Decimal(str(price_raw))
            if current_price <= 0:
                raise ValueError(f"current_price must be positive: {current_price}")
        except (InvalidOperation, ValueError) as e:
            _log("runner.reject.invalid_payload", symbol=symbol, reason=f"current_price: {e}")
            _record_reject(r, market, "reject_invalid_payload")
            return None

    # 7. stop/take pct 동적 계산 + stop price 계산 (market-aware 정규화)
    stop_pct, take_pct = _dynamic_pcts(range_5m, market)
    stop_raw = current_price * (1 - stop_pct)
    stop_price = _normalize_price(market, stop_raw)
    if stop_price <= 0:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"stop_price={stop_price} <= 0")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 8. Signal 객체 생성 (Pydantic 검증 포함)
    signal_id = str(uuid.uuid4())
    ts = datetime.now(_KST).isoformat()

    # H1: 잔고 비율 기반 size_cash + confidence 가중
    base_size = _calc_size_cash(market, current_price)

    try:
        c_conf = float(claude.get("confidence") or "0.7")
    except (ValueError, TypeError):
        c_conf = 0.7
    if c_conf >= 0.8:
        conf_mult = Decimal("1.2")
    elif c_conf < 0.6:
        conf_mult = Decimal("0.8")
    else:
        conf_mult = Decimal("1.0")

    # COIN은 소수점 매수 → current_price(BTC=99.5M 등) floor 불필요
    if market == "COIN":
        size_cash = base_size * conf_mult
    else:
        size_cash = max(base_size * conf_mult, current_price)
    _log("size_cash_weighted", symbol=symbol, conf=f"{c_conf:.2f}",
         mult=str(conf_mult), size_cash=str(size_cash))

    try:
        signal = Signal(
            signal_id=signal_id,
            ts=ts,
            market=market,
            symbol=symbol,
            direction="LONG",
            entry=SignalEntry(
                price=current_price,
                size_cash=size_cash,
            ),
            stop=SignalStop(price=stop_price),
            stop_pct=stop_pct,
            take_pct=take_pct,
        )
    except Exception as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"Signal validation: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 9. claw:signal:queue enqueue
    payload = {
        "signal_id": signal.signal_id,
        "ts": signal.ts,
        "market": signal.market,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry": {
            "price": str(signal.entry.price),
            "size_cash": str(signal.entry.size_cash),
        },
        "stop": {"price": str(signal.stop.price)},
        "source": "consensus_signal_runner",
        "status": "candidate",
        "strategy": "momentum_breakout",
        "claude_emit": 1,
        "claude_conf": str(c_conf),
        "ret_5m": ret_5m,
        "range_5m": range_5m,
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
    }

    # cooldown SET: consensus 성공 + prefilter 통과 후, signal push 직전에만 설정
    r.set(cooldown_key, "1", ex=_SYMBOL_COOLDOWN_SEC)

    try:
        r.lpush("claw:signal:queue", json.dumps(payload))
    except Exception as e:
        _log("runner.error.publish_failed", symbol=symbol, signal_id=signal_id, error=str(e))
        r.delete(cooldown_key)  # lpush 실패 시 cooldown 롤백
        return None

    # 종목별 일일 진입 카운트 증가
    r.incr(symbol_daily_key)
    r.expire(symbol_daily_key, 86400)

    # stop_pct/take_pct를 exit runner가 읽을 수 있도록 저장 (TTL 24시간)
    r.hset(f"claw:signal_pct:{market}:{symbol}", mapping={
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
    })
    r.expire(f"claw:signal_pct:{market}:{symbol}", 86400)

    # 10. 감사 로그 / 통계
    _save_audit(r, market, signal, ret_5m, range_5m)

    _log(
        "runner.pass.candidate_created",
        signal_id=signal_id,
        symbol=symbol,
        market=market,
        entry_price=str(current_price),
        stop_price=str(stop_price),
        stop_pct=str(stop_pct),
        take_pct=str(take_pct),
        ret_5m=f"{ret_5m:.4f}",
        range_5m=f"{range_5m:.4f}",
    )

    return payload


# ---------------------------------------------------------------------------
# Type B: 추세 탑승 신호 (일간 서서히 오르는 종목 포착)
# ---------------------------------------------------------------------------

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic()
    return _anthropic_client


def _run_type_b_coin(symbol: str, r, today: str) -> Optional[dict]:
    """Type B 추세 탑승: 일간 +5% 이상 상승 중이며 현재도 고점 유지 중인 종목."""
    market = "COIN"

    # Type B 쿨다운 (4시간)
    tb_cooldown_key = f"consensus:type_b_cooldown:{market}:{symbol}"
    if r.exists(tb_cooldown_key):
        return None

    # daily_stop 체크 (완화 로직 동일 적용)
    daily_stop_key = f"claw:daily_stop:{market}:{symbol}:{today}"
    if r.exists(daily_stop_key):
        allow_reentry = False
        try:
            if r.type(daily_stop_key).decode() == "hash":
                ds = _hgetall_str(r, daily_stop_key)
                stop_ts = float(ds.get("stop_ts", 0))
                elapsed = time.time() - stop_ts
                if elapsed >= 7200 and ds.get("stop_price"):
                    ds_stop_price = float(ds["stop_price"])
                    live_check = _get_live_ret_5m(r, market, symbol)
                    if live_check is not None and ds_stop_price > 0:
                        if (live_check[1] - ds_stop_price) / ds_stop_price >= 0.03:
                            allow_reentry = True
        except Exception:
            pass
        if not allow_reentry:
            return None

    # Upbit ticker 조회
    client = _get_client(market)
    if client is None:
        return None
    try:
        ticker = client.get_ticker(symbol)
    except Exception as e:
        _log("type_b.ticker_error", symbol=symbol, error=str(e))
        return None

    change_rate = float(ticker.get("signed_change_rate", 0))
    trade_price = float(ticker.get("trade_price", 0))
    high_price = float(ticker.get("high_price", 0))
    vol_krw = float(ticker.get("acc_trade_price_24h", 0))

    if trade_price <= 0 or high_price <= 0:
        return None

    # 조건 ①: 전일대비 +5% 이상
    if change_rate < _TYPE_B_MIN_CHANGE_RATE:
        return None

    # 조건 ②: 당일 고점 -3% 이내 (추세 유지 확인)
    near_high = trade_price / high_price
    if near_high < _TYPE_B_NEAR_HIGH_RATIO:
        _log("type_b.reject.far_from_high", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", near_high=f"{near_high:.3f}")
        return None

    # 조건 ③: 거래대금 100억 이상
    if vol_krw < _TYPE_B_MIN_VOL_KRW:
        return None

    # 조건 ④: 현재도 5분 상승 중
    live = _get_live_ret_5m(r, market, symbol)
    if live is None:
        return None
    ret_5m, live_price_float = live
    if ret_5m < _TYPE_B_MIN_RET_5M:
        _log("type_b.reject.ret_5m_weak", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", ret_5m=f"{ret_5m:.4f}")
        return None

    current_price = Decimal(str(live_price_float))

    # Claude 평가 (Type B 전용 프롬프트)
    try:
        from ai.providers.base import build_type_b_prompt, parse_decision_response
        prompt = build_type_b_prompt(symbol, change_rate, trade_price, high_price, ret_5m, vol_krw)
        ai_client = _get_anthropic_client()
        resp = ai_client.messages.create(
            model=os.getenv("AI_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        emit, direction, confidence, reason = parse_decision_response(raw)
    except Exception as e:
        _log("type_b.claude_error", symbol=symbol, error=str(e))
        return None

    if not emit or direction != "LONG":
        _log("type_b.reject.claude_hold", symbol=symbol,
             change_rate=f"{change_rate*100:.1f}%", reason=reason[:60])
        return None

    # 신호 생성
    stop_pct = Decimal(os.getenv("COIN_EXIT_STOP_LOSS_PCT", "0.040"))
    take_pct = Decimal(os.getenv("COIN_EXIT_TAKE_PROFIT_PCT", "0.200"))
    stop_price = current_price * (1 - stop_pct)

    signal_id = str(uuid.uuid4())
    ts_now = datetime.now(_KST).isoformat()
    size_cash = _calc_size_cash(market, current_price)

    try:
        signal = Signal(
            signal_id=signal_id,
            ts=ts_now,
            market=market,
            symbol=symbol,
            direction="LONG",
            entry=SignalEntry(price=current_price, size_cash=size_cash),
            stop=SignalStop(price=stop_price),
            stop_pct=stop_pct,
            take_pct=take_pct,
        )
    except Exception as e:
        _log("type_b.signal_error", symbol=symbol, error=str(e))
        return None

    r.set(tb_cooldown_key, "1", ex=_TYPE_B_COOLDOWN_SEC)

    payload = {
        "signal_id": signal.signal_id,
        "ts": signal.ts,
        "market": market,
        "symbol": symbol,
        "direction": "LONG",
        "entry": {"price": str(current_price), "size_cash": str(size_cash)},
        "stop": {"price": str(stop_price)},
        "source": "consensus_signal_runner_type_b",
        "status": "candidate",
        "strategy": "trend_riding",
        "claude_emit": 1,
        "claude_conf": str(confidence),
        "ret_5m": ret_5m,
        "change_rate_daily": change_rate,
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
    }

    try:
        r.lpush("claw:signal:queue", json.dumps(payload))
    except Exception as e:
        _log("type_b.publish_failed", symbol=symbol, error=str(e))
        r.delete(tb_cooldown_key)
        return None

    r.hset(f"claw:signal_pct:{market}:{symbol}", mapping={
        "stop_pct": str(stop_pct), "take_pct": str(take_pct),
    })
    r.expire(f"claw:signal_pct:{market}:{symbol}", 86400)

    _log("type_b.pass.signal_created", symbol=symbol, signal_id=signal_id,
         change_rate=f"{change_rate*100:.1f}%", confidence=f"{confidence:.2f}",
         entry=str(current_price), reason=reason[:60])
    return payload


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("consensus: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 프로세스 락
    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("consensus: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("consensus: SIGTERM received, lock released", flush=True)
        sys.exit(0)

    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
    watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US")
    watchlist_coin = load_watchlist(r, "COIN", "GEN_WATCHLIST_COIN")

    if not watchlist_kr and not watchlist_us and not watchlist_coin:
        print("consensus: all watchlists empty — exiting", flush=True)
        r.delete(_LOCK_KEY)
        sys.exit(1)

    print(
        f"consensus: started poll_sec={_POLL_SEC} strategy=momentum_breakout "
        f"prefilter=ret_5m>{_MIN_SURGE_5M} range_5m>{_MIN_RANGE_5M} "
        f"stop_pct=0.025 take_pct=0.050 "
        f"watchlist_kr={watchlist_kr} "
        f"watchlist_us={watchlist_us} "
        f"watchlist_coin={watchlist_coin}",
        flush=True,
    )

    _last_type_b_ts = 0.0  # Type B 마지막 체크 시각

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)

            if is_paused(r):
                time.sleep(_POLL_SEC)
                continue

            # KR 처리 (장중일 때만)
            if is_market_hours("KR"):
                watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
                regime = _get_regime(r, "KR", watchlist_kr)
                _log("runner.regime", market="KR", regime=regime,
                     watchlist_size=len(watchlist_kr))
                for symbol in watchlist_kr:
                    if regime == "bearish":
                        continue  # 하락장에서 LONG 억제
                    try:
                        run_once("KR", symbol, r)
                    except Exception as e:
                        _log("runner.error.unexpected", market="KR",
                             symbol=symbol, error=str(e))

            # US 처리 (장중일 때만)
            if is_market_hours("US"):
                watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US")
                for symbol in watchlist_us:
                    try:
                        run_once("US", symbol, r)
                    except Exception as e:
                        _log("runner.error.unexpected", market="US",
                             symbol=symbol, error=str(e))

            # COIN 처리 (24/7)
            watchlist_coin = load_watchlist(r, "COIN", "GEN_WATCHLIST_COIN")
            if watchlist_coin:
                regime = _get_regime(r, "COIN", watchlist_coin)
                _log("runner.regime", market="COIN", regime=regime,
                     watchlist_size=len(watchlist_coin))
                for symbol in watchlist_coin:
                    if regime == "bearish":
                        continue
                    try:
                        run_once("COIN", symbol, r)
                    except Exception as e:
                        _log("runner.error.unexpected", market="COIN",
                             symbol=symbol, error=str(e))

                # Type B: 5분마다 추세 탑승 신호 체크
                if time.time() - _last_type_b_ts >= _TYPE_B_POLL_SEC and regime != "bearish":
                    today = today_kst()
                    for symbol in watchlist_coin:
                        try:
                            _run_type_b_coin(symbol, r, today)
                        except Exception as e:
                            _log("runner.error.type_b", market="COIN",
                                 symbol=symbol, error=str(e))
                    _last_type_b_ts = time.time()

            time.sleep(_POLL_SEC)

    finally:
        r.delete(_LOCK_KEY)
        print("consensus: lock released", flush=True)


if __name__ == "__main__":
    main()
