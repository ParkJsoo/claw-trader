from dotenv import load_dotenv
load_dotenv()

import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis

from ai.generator import AISignalGenerator

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_EVAL_LOCK_KEY = "eval:runner:lock"
_EVAL_LOCK_TTL = 300  # > EVAL_POLL_SEC + 최대 처리 시간

_EVAL_POLL_SEC = float(os.getenv("EVAL_POLL_SEC", "120"))
_EVAL_DAILY_CALL_CAP = int(os.getenv("EVAL_DAILY_CALL_CAP", "2000"))  # 시장별 일일 cap
_EVAL_MIN_HIST = int(os.getenv("GEN_MIN_HIST", "20"))  # 기존 환경변수 재사용
_EVAL_LOG_MAX = 500  # 시장별 일일 eval 로그 최대 보관 수

_KST = ZoneInfo("Asia/Seoul")
_STATUS_LOG_INTERVAL = float(os.getenv("EVAL_STATUS_LOG_SEC", "600"))

# ---------------------------------------------------------------------------
# 목적: AI-First / No-Trade 모드
#   - claw:pause:global=true 여부와 무관하게 AI 평가 수행
#   - signal queue(claw:signal:queue) 절대 push 안 함
#   - executor / exchange / portfolio import 없음
# ---------------------------------------------------------------------------


def _parse_watchlist(env_key: str) -> list[str]:
    raw = os.getenv(env_key, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _today_kst() -> str:
    return datetime.now(_KST).strftime("%Y%m%d")


def _eval_symbol(gen: AISignalGenerator, r, market: str, symbol: str, today: str) -> None:
    """
    한 심볼에 대해 AI 평가 수행 후 Redis에 저장.
    주문/signal queue는 절대 건드리지 않음.
    """
    # 1. 히스토리 조회 (AISignalGenerator._get_hist 재사용)
    entries = gen._get_hist(market, symbol)
    if len(entries) < _EVAL_MIN_HIST:
        r.hincrby(f"ai:eval_stats:{market}:{today}", "skip_cold_start", 1)
        r.expire(f"ai:eval_stats:{market}:{today}", 7 * 86400)
        return

    # 2. 피처 계산 (AISignalGenerator._compute_features 재사용)
    now_ms = int(time.time() * 1000)
    features = gen._compute_features(entries, now_ms)
    if not features:
        r.hincrby(f"ai:eval_stats:{market}:{today}", "skip_feature_error", 1)
        r.expire(f"ai:eval_stats:{market}:{today}", 7 * 86400)
        return

    # 3. eval 전용 call count 체크 (기존 ai:call_count와 별도 키)
    call_key = f"ai:eval_call_count:{market}:{today}"
    call_count = r.incr(call_key)
    if call_count == 1:
        r.expire(call_key, 3 * 86400)
    if call_count > _EVAL_DAILY_CALL_CAP:
        r.decr(call_key)
        r.hincrby(f"ai:eval_stats:{market}:{today}", "skip_call_cap", 1)
        r.expire(f"ai:eval_stats:{market}:{today}", 7 * 86400)
        print(f"eval: call_cap_reached {market}:{symbol} cap={_EVAL_DAILY_CALL_CAP}", flush=True)
        return

    # 4. AI 호출 (기존 client/prompt/parse 재사용)
    #    generate() 는 호출하지 않음 — signal queue push 경로이므로
    try:
        client = gen._get_client()
        prompt = gen._build_prompt(market, symbol, features)
        response = client.messages.create(
            model=gen.model,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_response = response.content[0].text
        decision = gen._parse_response(raw_response, Decimal("999999"))
    except Exception as e:
        stats_key = f"ai:eval_stats:{market}:{today}"
        r.hincrby(stats_key, f"error_{type(e).__name__}", 1)
        r.expire(stats_key, 7 * 86400)
        print(f"eval: ai_error {market}:{symbol} {type(e).__name__}:{e}", flush=True)
        return

    # 5. 결과 저장 — 판단만 기록, 주문 없음
    ts_ms = int(time.time() * 1000)
    payload = {
        "ts_ms": str(ts_ms),
        "market": market,
        "symbol": symbol,
        "direction": decision.direction,
        "emit": "1" if decision.emit else "0",
        "reason": decision.reason,
        "features_json": json.dumps(
            {k: str(v) if v is not None else None for k, v in features.items()}
        ),
        "model": gen.model,
    }

    # 심볼별 최신 결과 (hash, overwrite) — /claw ai status 에서 읽음
    last_key = f"ai:eval:last:{market}:{symbol}"
    r.hset(last_key, mapping=payload)
    r.expire(last_key, 7 * 86400)

    # 일일 로그 (list, 최신 우선, 최대 _EVAL_LOG_MAX)
    log_key = f"ai:eval_log:{market}:{today}"
    r.lpush(log_key, json.dumps({**payload, "raw": raw_response[:500]}))
    r.ltrim(log_key, 0, _EVAL_LOG_MAX - 1)
    r.expire(log_key, 7 * 86400)

    # stats 카운터
    stats_key = f"ai:eval_stats:{market}:{today}"
    r.hincrby(stats_key, "emit" if decision.emit else "no_emit", 1)
    r.expire(stats_key, 7 * 86400)

    print(
        f"eval: {market}:{symbol} dir={decision.direction} emit={decision.emit} "
        f"reason={decision.reason[:60]}",
        flush=True,
    )


def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("eval: ANTHROPIC_API_KEY not set — exiting", flush=True)
        sys.exit(1)

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("eval: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 프로세스 락 (중복 실행 방지)
    if not r.set(_EVAL_LOCK_KEY, "1", nx=True, ex=_EVAL_LOCK_TTL):
        print("eval: already running (lock exists) — exiting", flush=True)
        sys.exit(0)
    print("eval: lock acquired", flush=True)

    watchlist_kr = _parse_watchlist("GEN_WATCHLIST_KR")
    watchlist_us = _parse_watchlist("GEN_WATCHLIST_US")

    # AISignalGenerator: AI 호출 유틸로만 사용
    # generate() 는 절대 호출하지 않음
    gen = AISignalGenerator(r)

    print(
        f"eval: started poll_sec={_EVAL_POLL_SEC} "
        f"call_cap={_EVAL_DAILY_CALL_CAP}/market/day "
        f"kr={watchlist_kr} us={watchlist_us}",
        flush=True,
    )

    _last_status_ts = 0.0

    try:
        while True:
            r.expire(_EVAL_LOCK_KEY, _EVAL_LOCK_TTL)

            today = _today_kst()
            now = time.time()

            # 주기적 상태 로그
            if now - _last_status_ts >= _STATUS_LOG_INTERVAL:
                _last_status_ts = now
                for market in ("KR", "US"):
                    call_val = r.get(f"ai:eval_call_count:{market}:{today}")
                    call_count = int(call_val) if call_val else 0
                    stats = r.hgetall(f"ai:eval_stats:{market}:{today}") or {}
                    stats_str = " ".join(
                        f"{(k.decode() if isinstance(k, bytes) else k)}={int(v)}"
                        for k, v in sorted(stats.items())
                    )
                    print(
                        f"[EVAL STATUS] {market} calls={call_count}/{_EVAL_DAILY_CALL_CAP} "
                        f"stats=[{stats_str or 'none'}]",
                        flush=True,
                    )

            # AI 평가 — pause 상태와 무관하게 실행 (AI-First 모드 핵심)
            for market, watchlist in [("KR", watchlist_kr), ("US", watchlist_us)]:
                if not watchlist:
                    continue
                for symbol in watchlist:
                    try:
                        _eval_symbol(gen, r, market, symbol, today)
                    except Exception as e:
                        print(f"eval: unexpected_error {market}:{symbol} {e}", flush=True)

            time.sleep(_EVAL_POLL_SEC)

    finally:
        r.delete(_EVAL_LOCK_KEY)
        print("eval: lock released", flush=True)


if __name__ == "__main__":
    main()
