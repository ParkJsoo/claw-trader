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

# KisClient / IbkrClient 싱글톤 캐시 (프로세스 재시작 시 초기화)
_client_cache: dict[str, object] = {}

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_KST = ZoneInfo("Asia/Seoul")

_LOCK_KEY = "consensus:runner:lock"
_LOCK_TTL = 120  # seconds

_POLL_SEC = float(os.getenv("CONSENSUS_POLL_SEC", "30"))

# Phase 10 prefilter 기준값 (Phase 11: ret_5m threshold 강화 0.0 → 0.001)
_MIN_RET_5M = float(os.getenv("CONSENSUS_MIN_RET_5M", "0.001"))
_MIN_RANGE_5M = 0.004

# Phase 11: symbol-level cooldown (같은 종목 N초 내 재emit 방지)
_SYMBOL_COOLDOWN_SEC = int(os.getenv("CONSENSUS_SYMBOL_COOLDOWN_SEC", "180"))

# Phase 17: 신호 품질 강화
_MIN_RET_15M = float(os.getenv("CONSENSUS_MIN_RET_15M", "0.0"))  # 15분 추세: 0 이상이어야 함
_VOLUME_SURGE_RATIO = float(os.getenv("CONSENSUS_VOLUME_SURGE_RATIO", "1.5"))  # 거래량 배수
_VOLUME_LOOKBACK_DAYS = int(os.getenv("VOLUME_LOOKBACK_DAYS", "7"))  # 5 → 7 (주말 포함)

# Phase 19: 하락장 대응
_INVERSE_ETF_KR = set(os.getenv("INVERSE_ETF_KR", "114800,251340").split(","))
_BULLISH_THRESHOLD = float(os.getenv("REGIME_BULLISH_THRESHOLD", "0.30"))  # bearish 비율 < 30% → bullish

_AUDIT_TTL = 7 * 86400   # 7일
_STATS_TTL = 30 * 86400


# ---------------------------------------------------------------------------
# 동적 stop/take pct 계산
# ---------------------------------------------------------------------------

def _dynamic_pcts(range_5m: float) -> tuple:
    """range_5m 기반 동적 stop/take pct 계산."""
    stop = max(0.015, range_5m * 1.2)
    take = max(0.020, range_5m * 2.0)
    return Decimal(str(round(stop, 4))), Decimal(str(round(take, 4)))


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
    """market에 따라 가격 정규화: KR=호가단위, US=소수점 2자리."""
    if market == "KR":
        return normalize_kr_price_tick(price)
    return price.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 로깅 헬퍼
# ---------------------------------------------------------------------------

def _get_client(market: str):
    """KisClient/IbkrClient 싱글톤 캐시."""
    if market not in _client_cache:
        try:
            if market == "KR":
                from exchange.kis.client import KisClient
                _client_cache[market] = KisClient()
            else:
                from exchange.ibkr.client import IbkrClient
                _client_cache[market] = IbkrClient()
        except Exception as e:
            _log("client_init_failed", market=market, error=str(e))
            return None
    return _client_cache.get(market)


def _calc_size_cash(market: str, current_price: Decimal) -> Decimal:
    """H1: 잔고 비율 기반 size_cash 계산. 실패 시 1주(current_price) fallback."""
    pct = _SIZE_CASH_PCT_KR if market == "KR" else _SIZE_CASH_PCT_US
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
        if size_cash < current_price:
            return current_price
        return size_cash
    except Exception as e:
        _log("size_cash_error", market=market, error=str(e), exc_type=type(e).__name__)
        # 최소 size_cash 보장 (KR: 10만원, US: $100)
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
    """워치리스트 ret_5m 기반 regime 판별.

    Returns: "bearish" | "bullish" | "neutral"
    """
    if not watchlist:
        return "neutral"
    bearish = 0
    total = 0
    for symbol in watchlist:
        raw = r.hget(f"ai:dual:last:claude:{market}:{symbol}", "features_json")
        if not raw:
            continue
        try:
            features = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            ret_5m = features.get("ret_5m")
            if ret_5m is not None:
                total += 1
                if float(ret_5m) < 0:
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


# ---------------------------------------------------------------------------
# 핵심 처리: 심볼 1개
# ---------------------------------------------------------------------------

def run_once(market: str, symbol: str, r) -> Optional[dict]:
    """
    dual eval Redis 결과를 읽어 candidate Signal을 생성하고 queue에 push.

    Returns:
        signal dict (push 성공 시) or None (reject / 오류 시)
    """
    # 1. dual eval 결과 읽기
    claude = _hgetall_str(r, f"ai:dual:last:claude:{market}:{symbol}")
    qwen   = _hgetall_str(r, f"ai:dual:last:qwen:{market}:{symbol}")

    if not claude or not qwen:
        return None  # 아직 평가 결과 없음 — 무시 (cold start)

    # 2. dedup: 이미 이 ts_ms로 처리한 결과면 스킵 (중복 push 방지)
    # dual eval runner가 새 결과를 쓰기 전까지 동일 hash 반복 읽힘
    c_ts_ms = claude.get("ts_ms", "")
    q_ts_ms = qwen.get("ts_ms", "")
    seen_key = f"consensus:seen:{market}:{symbol}:{c_ts_ms}:{q_ts_ms}"
    if not r.set(seen_key, "1", nx=True, ex=max(int(_POLL_SEC * 6), 60)):
        return None  # 이미 처리한 eval 결과

    # 2-b. symbol-level cooldown: consensus 판단 전 체크 (불필요한 처리 방지)
    cooldown_key = f"consensus:symbol_cooldown:{market}:{symbol}"
    if r.exists(cooldown_key):
        _log("runner.reject.cooldown", symbol=symbol, cooldown_sec=_SYMBOL_COOLDOWN_SEC)
        _record_reject(r, market, "reject_cooldown")
        return None

    # 3. 데이터 무결성: features_json 파싱
    try:
        c_features = json.loads(claude.get("features_json") or "{}")
        q_features = json.loads(qwen.get("features_json") or "{}")
    except (json.JSONDecodeError, Exception) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"features_json parse error: {e}")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 3. dual consensus 확인 (emit)
    c_emit = claude.get("emit") == "1"
    q_emit = qwen.get("emit") == "1"
    _partial_consensus = False  # True이면 50% size_cash 적용

    if not c_emit:
        # Claude가 HOLD → 무조건 reject
        _log(
            "runner.reject.consensus_failed",
            symbol=symbol,
            claude_emit=claude.get("emit"),
            qwen_emit=qwen.get("emit"),
        )
        _record_reject(r, market, "reject_consensus_failed")
        return None

    if not q_emit:
        # Claude EMIT + Qwen HOLD → 뉴스 positive이면 partial consensus 허용
        if _has_positive_news(r, market, symbol):
            _partial_consensus = True
            _log("runner.partial_consensus", symbol=symbol,
                 reason="claude_emit+positive_news")
            _record_reject(r, market, "partial_consensus_approved")
        else:
            _log(
                "runner.reject.consensus_failed",
                symbol=symbol,
                claude_emit=claude.get("emit"),
                qwen_emit=qwen.get("emit"),
            )
            _record_reject(r, market, "reject_consensus_failed")
            return None

    # 4. 방향 일치 확인 (partial consensus는 Claude 방향만 사용)
    c_dir = claude.get("direction", "")
    q_dir = qwen.get("direction", "")
    if not _partial_consensus:
        if not c_dir or c_dir != q_dir:
            _log("runner.reject.direction_mismatch", symbol=symbol, claude_dir=c_dir, qwen_dir=q_dir)
            _record_reject(r, market, "reject_direction_mismatch")
            return None
    else:
        if not c_dir:
            _log("runner.reject.direction_mismatch", symbol=symbol, claude_dir=c_dir, qwen_dir=q_dir)
            _record_reject(r, market, "reject_direction_mismatch")
            return None

    # Phase 10: LONG 방향만 처리
    if c_dir != "LONG":
        _log("runner.reject.direction_not_long", symbol=symbol, direction=c_dir)
        _record_reject(r, market, "reject_direction_not_long")
        return None

    # 5. prefilter: ret_5m, range_5m
    try:
        ret_5m_raw   = c_features.get("ret_5m")
        range_5m_raw = c_features.get("range_5m")
        if ret_5m_raw is None or range_5m_raw is None:
            raise ValueError("ret_5m or range_5m missing in features_json")
        ret_5m   = float(ret_5m_raw)
        range_5m = float(range_5m_raw)
    except (TypeError, ValueError) as e:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=str(e))
        _record_reject(r, market, "reject_invalid_payload")
        return None

    if ret_5m <= _MIN_RET_5M:
        _log("runner.reject.prefilter_ret_5m", symbol=symbol, ret_5m=ret_5m)
        _record_reject(r, market, "reject_prefilter_ret_5m")
        return None

    if range_5m <= _MIN_RANGE_5M:
        _log("runner.reject.prefilter_range_5m", symbol=symbol, range_5m=range_5m)
        _record_reject(r, market, "reject_prefilter_range_5m")
        return None

    # 5-b. ret_15m prefilter: 15분 추세 확인
    try:
        ret_15m_raw = c_features.get("ret_15m")
        if ret_15m_raw is not None:
            ret_15m = float(ret_15m_raw)
            if ret_15m < _MIN_RET_15M:
                _log("runner.reject.prefilter_ret_15m", symbol=symbol, ret_15m=ret_15m)
                _record_reject(r, market, "reject_prefilter_ret_15m")
                return None
    except (TypeError, ValueError):
        pass  # 데이터 없으면 통과 (mark_hist 부족 시 등)

    # 5-c. Volume surge 필터 (KR만 — US는 데이터 없음)
    if market == "KR" and not _has_volume_surge(r, market, symbol):
        _log("runner.reject.volume_no_surge", symbol=symbol)
        _record_reject(r, market, "reject_volume_no_surge")
        return None

    # 6. current_price 추출 및 검증
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
    stop_pct, take_pct = _dynamic_pcts(range_5m)
    stop_raw = current_price * (1 - stop_pct)
    stop_price = _normalize_price(market, stop_raw)
    if stop_price <= 0:
        _log("runner.reject.invalid_payload", symbol=symbol, reason=f"stop_price={stop_price} <= 0")
        _record_reject(r, market, "reject_invalid_payload")
        return None

    # 8. Signal 객체 생성 (Pydantic 검증 포함)
    signal_id = str(uuid.uuid4())
    ts = datetime.now(_KST).isoformat()

    # H1: 잔고 비율 기반 size_cash + 뉴스/confidence 가중
    base_size = _calc_size_cash(market, current_price)

    news_score = "partial"
    c_conf = 0.0
    if _partial_consensus:
        # partial consensus: 50% 고정
        size_cash = max(base_size / 2, current_price)
    else:
        # full consensus: 뉴스 스코어 기반 가중
        news_score = _get_news_score(r, market, symbol)
        if news_score == "high":
            news_mult = Decimal("1.0")
        elif news_score == "medium":
            news_mult = Decimal("0.9")
        else:
            news_mult = Decimal("0.8")  # 뉴스 없으면 80%

        # Claude confidence 가중 (confidence >= 0.8 → +20%, < 0.6 → -20%)
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

        size_cash = max(base_size * news_mult * conf_mult, current_price)
        _log("size_cash_weighted", symbol=symbol, news=news_score,
             conf=f"{c_conf:.2f}", mult=str(news_mult * conf_mult),
             size_cash=str(size_cash))

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
        "consensus": "EMIT",
        "partial_consensus": _partial_consensus,
        "claude_emit": 1,
        "qwen_emit": 0 if _partial_consensus else (1 if q_emit else 0),
        "ret_5m": ret_5m,
        "range_5m": range_5m,
        "stop_pct": str(stop_pct),
        "take_pct": str(take_pct),
        "news_score": news_score if not _partial_consensus else "partial",
        "claude_conf": str(c_conf) if not _partial_consensus else "0.0",
    }

    # cooldown SET: consensus 성공 + prefilter 통과 후, signal push 직전에만 설정
    r.set(cooldown_key, "1", ex=_SYMBOL_COOLDOWN_SEC)

    try:
        r.lpush("claw:signal:queue", json.dumps(payload))
    except Exception as e:
        _log("runner.error.publish_failed", symbol=symbol, signal_id=signal_id, error=str(e))
        r.delete(cooldown_key)  # lpush 실패 시 cooldown 롤백
        return None

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

    if not watchlist_kr and not watchlist_us:
        print("consensus: both KR/US watchlists empty — exiting", flush=True)
        r.delete(_LOCK_KEY)
        sys.exit(1)

    print(
        f"consensus: started poll_sec={_POLL_SEC} "
        f"prefilter=ret_5m>{_MIN_RET_5M} range_5m>{_MIN_RANGE_5M} "
        f"stop_pct=dynamic(range_5m*1.2,min=0.015) take_pct=dynamic(range_5m*2.0,min=0.020) "
        f"watchlist_kr={watchlist_kr} "
        f"watchlist_us={watchlist_us}",
        flush=True,
    )

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
                    is_inverse = symbol in _INVERSE_ETF_KR
                    if regime == "bearish" and not is_inverse:
                        continue  # 하락장에서 일반 LONG 억제
                    if regime != "bearish" and is_inverse:
                        continue  # 상승/횡보장에서 인버스 ETF 억제
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

            time.sleep(_POLL_SEC)

    finally:
        r.delete(_LOCK_KEY)
        print("consensus: lock released", flush=True)


if __name__ == "__main__":
    main()
