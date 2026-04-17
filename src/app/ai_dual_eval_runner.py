from dotenv import load_dotenv
load_dotenv()

import json
import os
import random
import signal as _signal
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis

from ai.generator import AISignalGenerator
from ai.providers.claude_provider import ClaudeProvider
from news.redis_writer import get_symbol_context
from utils.redis_helpers import parse_watchlist, load_watchlist, today_kst, is_market_hours

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_DUAL_LOCK_KEY = "dual:runner:lock"
_DUAL_LOCK_TTL = 300          # > DUAL_POLL_SEC + 최대 처리 시간

_DUAL_POLL_SEC = float(os.getenv("DUAL_POLL_SEC", "180"))
_DUAL_DAILY_CALL_CAP = int(os.getenv("DUAL_DAILY_CALL_CAP", "500"))    # 시장별 일일 캡
_DUAL_MIN_HIST = int(os.getenv("GEN_MIN_HIST", "20"))

# prefilter: momentum breakout — 5분 상승폭 최소 기준 (AI call 전 차단으로 call 절감)
_MB_SURGE_5M = float(os.getenv("MB_MIN_SURGE_5M", "0.020"))       # COIN/US 5분 급등 최소 2.0%
_MB_SURGE_5M_KR = float(os.getenv("MB_MIN_SURGE_5M_KR", "0.030"))  # KR 3.0% (consensus와 동일)
_DUAL_MIN_RANGE_5M = 0.004
_DUAL_LOG_MAX = 500           # 시장별 일일 로그 최대 보관 수
_DUAL_JITTER_MAX_SEC = 3.0    # 심볼 간 호출 분산 최대 지터(초)
_DUAL_TTL = 7 * 86400

_STATUS_LOG_INTERVAL = float(os.getenv("DUAL_STATUS_LOG_SEC", "600"))

_LUA_CAP_INCR = """
local v = redis.call('INCR', KEYS[1])
if v == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if v > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return -1
end
return v
"""

# ---------------------------------------------------------------------------
# Redis 저장
# ---------------------------------------------------------------------------

def _save_provider(r, provider: str, market: str, symbol: str, today: str,
                   result, features: dict) -> None:
    ts_ms = int(time.time() * 1000)
    payload = {
        "ts_ms": str(ts_ms),
        "market": market,
        "symbol": symbol,
        "provider": provider,
        "model": result.model,
        "direction": result.direction,
        "emit": "1" if result.emit else "0",
        "confidence": str(result.confidence),
        "reason": result.reason,
        "error": result.error,
        "features_json": json.dumps(
            {k: str(v) if v is not None else None for k, v in features.items()}
        ),
    }

    # 최신 결과 (hash, overwrite)
    last_key = f"ai:dual:last:{provider}:{market}:{symbol}"
    r.hset(last_key, mapping=payload)
    r.expire(last_key, _DUAL_TTL)

    # 일일 로그 (list)
    log_key = f"ai:dual_log:{provider}:{market}:{today}"
    r.lpush(log_key, json.dumps({**payload, "raw": (result.raw_response or "")[:300]}))
    r.ltrim(log_key, 0, _DUAL_LOG_MAX - 1)
    r.expire(log_key, 30 * 86400)

    # 통계
    stats_key = f"ai:dual_stats:{provider}:{market}:{today}"
    if result.error:
        r.hincrby(stats_key, f"error_{result.error.split(':')[0]}", 1)
    else:
        r.hincrby(stats_key, "emit" if result.emit else "no_emit", 1)
    r.expire(stats_key, _DUAL_TTL)


# ---------------------------------------------------------------------------
# 심볼 평가
# ---------------------------------------------------------------------------

def _eval_symbol(gen: AISignalGenerator, claude: ClaudeProvider,
                 r, market: str, symbol: str, today: str) -> None:
    # 1. 히스토리 조회 + feature 계산 (AISignalGenerator 재사용)
    entries = gen._get_hist(market, symbol)
    if len(entries) < _DUAL_MIN_HIST:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_cold_start", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        return

    now_ms = int(time.time() * 1000)

    # tick freshness 체크: 최신 tick이 5분 이상 오래됐으면 skip (거래 없는 stale 심볼)
    _TICK_FRESH_MS = 300_000
    try:
        latest_ts = int(entries[0].split(":")[0])
        if (now_ms - latest_ts) > _TICK_FRESH_MS:
            r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_stale_tick", 1)
            r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
            return
    except (IndexError, ValueError):
        pass

    features = gen._compute_features(entries, now_ms)
    if not features:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_feature_error", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        return

    # ret_5m None이면 tick 히스토리 부족 — Claude 호출해도 "Missing" HOLD만 반환
    if features.get("ret_5m") is None:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_no_momentum_data", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        return

    # 2. momentum breakout prefilter: 급등 + 변동성 확인 (AI call 절감)
    stats_key = f"ai:dual_stats:consensus:{market}:{today}"

    # 직전 eval이 emit=0이고 30분 이상 지났으면 prefilter 우회 (stale HOLD 재평가)
    _stale_reeval_sec = int(os.getenv("EVAL_STALE_SEC", "1800"))
    last_eval_raw = r.hgetall(f"ai:dual:last:claude:{market}:{symbol}")
    _force_reeval = False
    if last_eval_raw:
        last_emit = (last_eval_raw.get(b"emit") or b"1").decode()
        last_ts_ms = int((last_eval_raw.get(b"ts_ms") or b"0").decode())
        if last_emit == "0" and (now_ms - last_ts_ms) > _stale_reeval_sec * 1000:
            _force_reeval = True

    try:
        ret_5m_val = features.get("ret_5m")
        range_5m_val = features.get("range_5m")
        if _force_reeval:
            # stale HOLD 재평가: 폭락 중인 종목은 재평가 불필요 (call_cap 절감)
            if ret_5m_val is not None and float(ret_5m_val) < -0.005:
                r.hincrby(stats_key, "skip_prefilter_stale_falling", 1)
                r.expire(stats_key, _DUAL_TTL)
                return
        else:
            # 5분 상승폭이 충분하지 않으면 skip (momentum breakout 셋업 아님)
            _surge_threshold = _MB_SURGE_5M_KR if market == "KR" else _MB_SURGE_5M
            if ret_5m_val is not None and float(ret_5m_val) <= _surge_threshold:
                r.hincrby(stats_key, "skip_prefilter_ret5m", 1)
                r.expire(stats_key, _DUAL_TTL)
                return
            # 변동성 없으면 skip
            if range_5m_val is not None and float(range_5m_val) <= _DUAL_MIN_RANGE_5M:
                r.hincrby(stats_key, "skip_prefilter_range5m", 1)
                r.expire(stats_key, _DUAL_TTL)
                return
    except (TypeError, ValueError):
        pass

    # 2-b. 라운드 캡 체크
    call_key = f"ai:dual_call_count:{market}:{today}"
    call_count = r.eval(_LUA_CAP_INCR, 1, call_key, _DUAL_DAILY_CALL_CAP, 3 * 86400)
    if call_count == -1:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_call_cap", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        print(f"dual: call_cap_reached {market}:{symbol} cap={_DUAL_DAILY_CALL_CAP}", flush=True)
        return

    # 2-e. 뉴스 컨텍스트 추가 (있으면 AI 프롬프트에 포함)
    news_ctx = get_symbol_context(r, market, symbol, today, max_items=3)
    if not news_ctx:
        # 오늘 뉴스 없으면 어제 뉴스 확인
        yesterday = (datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(days=1)).strftime("%Y%m%d")
        news_ctx = get_symbol_context(r, market, symbol, yesterday, max_items=3)
    if news_ctx:
        features["news_summary"] = news_ctx

    # 2-e2. KR: 장 시각 컨텍스트 (장 초반/중반/말미 판단용)
    if market == "KR":
        features["market_time"] = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M KST")

    # 2-f. 오더북 데이터 추가 (COIN만, ws_exit_monitor가 갱신)
    if market == "COIN":
        ob_raw = r.hget(f"orderbook:COIN:{symbol}", "ob_ratio")
        if ob_raw:
            try:
                features["ob_ratio"] = float(ob_raw.decode() if isinstance(ob_raw, bytes) else ob_raw)
            except (TypeError, ValueError):
                pass

    # 3. Claude 평가 (bad news filter)
    c_result = claude.evaluate(market, symbol, features)
    _save_provider(r, "claude", market, symbol, today, c_result, features)

    c_err = f" err={c_result.error}" if c_result.error else ""
    print(
        f"eval: {market}:{symbol} "
        f"claude={c_result.direction}({c_result.confidence:.2f}) "
        f"emit={c_result.emit} reason={c_result.reason[:60]}{c_err}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("dual: ANTHROPIC_API_KEY not set — exiting", flush=True)
        sys.exit(1)

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("dual: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 프로세스 락
    if not r.set(_DUAL_LOCK_KEY, "1", nx=True, ex=_DUAL_LOCK_TTL):
        print("dual: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_DUAL_LOCK_KEY)
        print("dual: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    print("dual: lock acquired", flush=True)

    watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
    watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US") if os.getenv("IBKR_ACCOUNT_ID") else []

    gen = AISignalGenerator(r)      # feature 계산 유틸로만 사용
    claude = ClaudeProvider()

    print(
        f"eval: started poll_sec={_DUAL_POLL_SEC} "
        f"call_cap={_DUAL_DAILY_CALL_CAP}/market/day "
        f"model={claude.model} strategy=momentum_breakout surge_threshold={_MB_SURGE_5M} "
        f"kr={watchlist_kr} us={watchlist_us}",
        flush=True,
    )

    _last_status_ts = 0.0

    try:
        while True:
            r.expire(_DUAL_LOCK_KEY, _DUAL_LOCK_TTL)

            today = today_kst()
            now = time.time()

            # 주기적 상태 로그
            if now - _last_status_ts >= _STATUS_LOG_INTERVAL:
                _last_status_ts = now
                for market in ("KR", "US", "COIN"):
                    call_val = r.get(f"ai:dual_call_count:{market}:{today}")
                    call_count = int(call_val) if call_val else 0
                    print(
                        f"[EVAL STATUS] {market} calls={call_count}/{_DUAL_DAILY_CALL_CAP}",
                        flush=True,
                    )

            # 동적 워치리스트 갱신 (매 폴링마다 Redis 확인)
            watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
            watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US") if os.getenv("IBKR_ACCOUNT_ID") else []
            watchlist_coin = load_watchlist(r, "COIN", "GEN_WATCHLIST_COIN")

            # AI 평가 (pause 상태와 무관 — No-Trade 모드)
            for market, watchlist in [("KR", watchlist_kr), ("US", watchlist_us), ("COIN", watchlist_coin)]:
                if not watchlist:
                    continue
                if not is_market_hours(market):  # COIN은 항상 True
                    print(f"dual: market_closed {market} skip", flush=True)
                    continue
                for symbol in watchlist:
                    r.expire(_DUAL_LOCK_KEY, _DUAL_LOCK_TTL)
                    time.sleep(random.uniform(0, _DUAL_JITTER_MAX_SEC))
                    try:
                        _eval_symbol(gen, claude, r, market, symbol, today)
                    except Exception as e:
                        print(f"dual: unexpected_error {market}:{symbol} {e}", flush=True)

            time.sleep(_DUAL_POLL_SEC)

    finally:
        r.delete(_DUAL_LOCK_KEY)
        print("dual: lock released", flush=True)


if __name__ == "__main__":
    main()
