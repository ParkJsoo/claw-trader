from dotenv import load_dotenv
load_dotenv()

import json
import os
import random
import signal as _signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from ai.generator import AISignalGenerator
from ai.providers.claude_provider import ClaudeProvider
from ai.providers.qwen_provider import QwenProvider
from news.redis_writer import get_symbol_context
from utils.redis_helpers import parse_watchlist, load_watchlist, today_kst, is_market_hours

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_DUAL_LOCK_KEY = "dual:runner:lock"
_DUAL_LOCK_TTL = 300          # > DUAL_POLL_SEC + 최대 처리 시간

_DUAL_POLL_SEC = float(os.getenv("DUAL_POLL_SEC", "120"))
_DUAL_DAILY_CALL_CAP = int(os.getenv("DUAL_DAILY_CALL_CAP", "2000"))   # 시장별 라운드 캡
_DUAL_MIN_HIST = int(os.getenv("GEN_MIN_HIST", "20"))
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
# 합의 정책
# ---------------------------------------------------------------------------

def _compute_consensus(c_emit: bool, c_dir: str, q_emit: bool, q_dir: str) -> tuple[str, str]:
    """
    Returns (consensus, direction)

    Rules:
    - 둘 다 emit=true AND direction 동일 → EMIT
    - 둘 다 emit=true BUT direction 다름 → HOLD
    - 한쪽만 emit=true → HOLD
    - 둘 다 emit=false → SKIP
    """
    if c_emit and q_emit:
        if c_dir == q_dir:
            return "EMIT", c_dir
        return "HOLD", "HOLD"
    if not c_emit and not q_emit:
        return "SKIP", "HOLD"
    return "HOLD", "HOLD"


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
    r.lpush(log_key, json.dumps({**payload, "raw": result.raw_response[:300]}))
    r.ltrim(log_key, 0, _DUAL_LOG_MAX - 1)
    r.expire(log_key, 30 * 86400)

    # 통계
    stats_key = f"ai:dual_stats:{provider}:{market}:{today}"
    if result.error:
        r.hincrby(stats_key, f"error_{result.error.split(':')[0]}", 1)
    else:
        r.hincrby(stats_key, "emit" if result.emit else "no_emit", 1)
    r.expire(stats_key, _DUAL_TTL)


def _save_consensus(r, market: str, symbol: str, today: str,
                    consensus: str, direction: str,
                    c_result, q_result, features: dict) -> None:
    ts_ms = int(time.time() * 1000)
    payload = {
        "ts_ms": str(ts_ms),
        "market": market,
        "symbol": symbol,
        "consensus": consensus,
        "direction": direction,
        "claude_emit": "1" if c_result.emit else "0",
        "claude_dir": c_result.direction,
        "claude_conf": str(c_result.confidence),
        "qwen_emit": "1" if q_result.emit else "0",
        "qwen_dir": q_result.direction,
        "qwen_conf": str(q_result.confidence),
        "features_json": json.dumps(
            {k: str(v) if v is not None else None for k, v in features.items()}
        ),
    }

    last_key = f"ai:dual:last:consensus:{market}:{symbol}"
    r.hset(last_key, mapping=payload)
    r.expire(last_key, _DUAL_TTL)

    log_key = f"ai:dual_log:consensus:{market}:{today}"
    r.lpush(log_key, json.dumps(payload))
    r.ltrim(log_key, 0, _DUAL_LOG_MAX - 1)
    r.expire(log_key, 30 * 86400)

    # consensus 통계
    stats_key = f"ai:dual_stats:consensus:{market}:{today}"
    r.hincrby(stats_key, consensus.lower(), 1)
    r.expire(stats_key, _DUAL_TTL)

    # 비교 통계
    cmp_key = f"ai:dual_compare:{market}:{today}"
    if c_result.emit and q_result.emit:
        if c_result.direction == q_result.direction:
            r.hincrby(cmp_key, "both_emit_same_dir", 1)
            r.hincrby(cmp_key, "match_count", 1)
        else:
            r.hincrby(cmp_key, "both_emit_diff_dir", 1)
            r.hincrby(cmp_key, "mismatch_count", 1)
    elif not c_result.emit and not q_result.emit:
        r.hincrby(cmp_key, "both_no_emit", 1)
        r.hincrby(cmp_key, "match_count", 1)
    elif c_result.emit:
        r.hincrby(cmp_key, "claude_only_emit", 1)
        r.hincrby(cmp_key, "mismatch_count", 1)
    else:
        r.hincrby(cmp_key, "qwen_only_emit", 1)
        r.hincrby(cmp_key, "mismatch_count", 1)
    r.expire(cmp_key, _DUAL_TTL)


# ---------------------------------------------------------------------------
# 심볼 평가
# ---------------------------------------------------------------------------

def _eval_symbol(gen: AISignalGenerator, claude: ClaudeProvider, qwen: QwenProvider,
                 r, market: str, symbol: str, today: str) -> None:
    # 1. 히스토리 조회 + feature 계산 (AISignalGenerator 재사용)
    entries = gen._get_hist(market, symbol)
    if len(entries) < _DUAL_MIN_HIST:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_cold_start", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        return

    now_ms = int(time.time() * 1000)
    features = gen._compute_features(entries, now_ms)
    if not features:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_feature_error", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        return

    # 2. Phase 11 prefilter: AI 호출 전 기본 조건 확인 (call 절감)
    ret_1m = features.get("ret_1m")
    try:
        if ret_1m is not None and float(ret_1m) < -0.005:
            # 1분 수익률 -0.5% 이하면 하락 중 → AI 호출 건너뜀
            r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_prefilter_ret1m", 1)
            r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
            return
    except (TypeError, ValueError):
        pass

    # 2-b. 라운드 캡 체크 (하나의 increment = Claude + Qwen 한 세트)
    call_key = f"ai:dual_call_count:{market}:{today}"
    call_count = r.eval(_LUA_CAP_INCR, 1, call_key, _DUAL_DAILY_CALL_CAP, 3 * 86400)
    if call_count == -1:
        r.hincrby(f"ai:dual_stats:consensus:{market}:{today}", "skip_call_cap", 1)
        r.expire(f"ai:dual_stats:consensus:{market}:{today}", _DUAL_TTL)
        print(f"dual: call_cap_reached {market}:{symbol} cap={_DUAL_DAILY_CALL_CAP}", flush=True)
        return

    # 2-c. 뉴스 컨텍스트 추가 (있으면 AI 프롬프트에 포함)
    news_ctx = get_symbol_context(r, market, symbol, today, max_items=3)
    if not news_ctx:
        # 오늘 뉴스 없으면 어제 뉴스 확인
        from datetime import datetime, timedelta
        yesterday = (datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(days=1)).strftime("%Y%m%d")
        news_ctx = get_symbol_context(r, market, symbol, yesterday, max_items=3)
    if news_ctx:
        features["news_summary"] = news_ctx

    # 3. 두 provider 평가
    c_result = claude.evaluate(market, symbol, features)
    q_result = qwen.evaluate(market, symbol, features)

    # 4. 개별 결과 저장
    _save_provider(r, "claude", market, symbol, today, c_result, features)
    _save_provider(r, "qwen", market, symbol, today, q_result, features)

    # 5. 합의 계산 + 저장
    consensus, cons_dir = _compute_consensus(
        c_result.emit, c_result.direction,
        q_result.emit, q_result.direction,
    )
    _save_consensus(r, market, symbol, today, consensus, cons_dir,
                    c_result, q_result, features)

    c_err = f" c_err={c_result.error}" if c_result.error else ""
    q_err = f" q_err={q_result.error}" if q_result.error else ""
    print(
        f"dual: {market}:{symbol} "
        f"claude={c_result.direction}({c_result.confidence:.2f}) "
        f"qwen={q_result.direction}({q_result.confidence:.2f}) "
        f"consensus={consensus}/{cons_dir}{c_err}{q_err}",
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
    watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US")

    gen = AISignalGenerator(r)      # feature 계산 유틸로만 사용
    claude = ClaudeProvider()
    qwen = QwenProvider()

    print(
        f"dual: started poll_sec={_DUAL_POLL_SEC} "
        f"call_cap={_DUAL_DAILY_CALL_CAP}/market/day "
        f"claude={claude.model} qwen={qwen.model} "
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
                for market in ("KR", "US"):
                    call_val = r.get(f"ai:dual_call_count:{market}:{today}")
                    call_count = int(call_val) if call_val else 0
                    cmp = r.hgetall(f"ai:dual_compare:{market}:{today}") or {}
                    cmp_str = " ".join(
                        f"{(k.decode() if isinstance(k, bytes) else k)}={int(v)}"
                        for k, v in sorted(cmp.items())
                    )
                    print(
                        f"[DUAL STATUS] {market} calls={call_count}/{_DUAL_DAILY_CALL_CAP} "
                        f"compare=[{cmp_str or 'none'}]",
                        flush=True,
                    )

            # 동적 워치리스트 갱신 (매 폴링마다 Redis 확인)
            watchlist_kr = load_watchlist(r, "KR", "GEN_WATCHLIST_KR")
            watchlist_us = load_watchlist(r, "US", "GEN_WATCHLIST_US")

            # AI 평가 (pause 상태와 무관 — No-Trade 모드)
            for market, watchlist in [("KR", watchlist_kr), ("US", watchlist_us)]:
                if not watchlist:
                    continue
                if not is_market_hours(market):
                    print(f"dual: market_closed {market} skip", flush=True)
                    continue
                for symbol in watchlist:
                    r.expire(_DUAL_LOCK_KEY, _DUAL_LOCK_TTL)
                    time.sleep(random.uniform(0, _DUAL_JITTER_MAX_SEC))
                    try:
                        _eval_symbol(gen, claude, qwen, r, market, symbol, today)
                    except Exception as e:
                        print(f"dual: unexpected_error {market}:{symbol} {e}", flush=True)

            time.sleep(_DUAL_POLL_SEC)

    finally:
        r.delete(_DUAL_LOCK_KEY)
        print("dual: lock released", flush=True)


if __name__ == "__main__":
    main()
