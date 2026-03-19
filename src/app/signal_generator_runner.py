from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis

from guards.data_guard import DataGuard
from guards.notifier import send_telegram
from ai.generator import AISignalGenerator
from utils.redis_helpers import parse_watchlist, today_kst, is_market_hours, secs_until_kst_midnight as _secs_until_kst_midnight

_GEN_POLL_SEC = float(os.getenv("GEN_POLL_SEC", "60"))
_GEN_MAX_SIZE_CASH_KR = Decimal(os.getenv("GEN_MAX_SIZE_CASH_KR", "500000"))
_GEN_MAX_SIZE_CASH_US = Decimal(os.getenv("GEN_MAX_SIZE_CASH_US", "1000"))

_LOCK_KEY = "gen:runner:lock"
_LOCK_TTL = 120  # seconds — must be > GEN_POLL_SEC + max processing time

_STATUS_LOG_INTERVAL = float(os.getenv("GEN_STATUS_LOG_SEC", "600"))   # 10분마다 상태 로그
_MD_STALE_SEC = float(os.getenv("GEN_MD_STALE_SEC", "180"))            # md stale 임계값
_MD_ERROR_SPIKE = int(os.getenv("GEN_MD_ERROR_SPIKE", "50"))           # 인터벌당 md 오류 급증
_AI_ERROR_SPIKE = int(os.getenv("GEN_AI_ERROR_SPIKE", "10"))           # 인터벌당 AI 오류 급증

_KST = ZoneInfo("Asia/Seoul")



_parse_watchlist = parse_watchlist
_today_kst = today_kst


def _is_paused(r) -> bool:
    val = r.get("claw:pause:global")
    if not val:
        return False
    return (val.decode() if isinstance(val, bytes) else val).lower() == "true"


def _do_auto_pause(r, reason: str, market: str, detail: str) -> None:
    """전역 일시정지 설정 (NX: 첫 발동만 기록) + reason/meta 기록 + TG 알림."""
    ts_ms = str(int(time.time() * 1000))
    set_ok = r.set("claw:pause:global", "true", nx=True, ex=_secs_until_kst_midnight())
    if set_ok:
        ttl = _secs_until_kst_midnight()
        r.set("claw:pause:reason", reason, ex=ttl)
        r.hset("claw:pause:meta", mapping={
            "reason": reason, "market": market, "detail": detail,
            "ts_ms": ts_ms, "source": "signal_generator",
        })
        r.expire("claw:pause:meta", ttl)
        msg = f"[CLAW] AUTO-PAUSE: {reason}\nmarket={market}\n{detail}"
        sent = send_telegram(msg)
        print(f"signal_generator: auto_pause reason={reason} market={market} {detail} tg_sent={sent}", flush=True)
    else:
        print(f"signal_generator: auto_pause already active; skip telegram reason={reason} market={market}", flush=True)


def _md_age_sec(r, market: str) -> float:
    val = r.get(f"md:last_update:{market}")
    if not val:
        return float("inf")
    ts_ms = int(val.decode() if isinstance(val, bytes) else val)
    return (time.time() * 1000 - ts_ms) / 1000


def _get_md_error_total(r, market: str, today: str) -> int:
    data = r.hgetall(f"md:error:{market}:{today}") or {}
    return sum(int(v) for v in data.values())


def _get_ai_error_total(r, market: str, today: str) -> int:
    data = r.hgetall(f"ai:gen_stats:{market}:{today}") or {}
    total = 0
    for k, v in data.items():
        k_str = k.decode() if isinstance(k, bytes) else k
        if k_str.startswith("error_"):
            total += int(v)
    return total


def _get_ai_stats_str(r, market: str, today: str) -> str:
    data = r.hgetall(f"ai:gen_stats:{market}:{today}") or {}
    items = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in data.items()}
    return " ".join(f"{k}={v}" for k, v in sorted(items.items())) or "none"


def _health_check(r, watchlist_kr: list, watchlist_us: list, state: dict) -> list:
    """
    10분마다 1줄 상태 로그 출력 + 비정상 감지.
    반환값: [(market, reason, detail), ...] — 비어있으면 정상.
    """
    now = time.time()
    if now - state["last_log_ts"] < _STATUS_LOG_INTERVAL:
        return []
    state["last_log_ts"] = now

    today = _today_kst()
    anomalies = []

    for market, symbols in [("KR", watchlist_kr), ("US", watchlist_us)]:
        age = _md_age_sec(r, market)
        hist_str = " ".join(
            f"{s}={r.llen(f'mark_hist:{market}:{s}')}" for s in symbols
        )
        md_err_total = _get_md_error_total(r, market, today)
        ai_err_total = _get_ai_error_total(r, market, today)
        ai_stats = _get_ai_stats_str(r, market, today)
        lock_ttl = r.ttl(_LOCK_KEY)

        md_err_delta = md_err_total - state["md_err_prev"].get(market, md_err_total)
        ai_err_delta = ai_err_total - state["ai_err_prev"].get(market, ai_err_total)
        state["md_err_prev"][market] = md_err_total
        state["ai_err_prev"][market] = ai_err_total

        print(
            f"[STATUS] {market} md_age={age:.0f}s hist=[{hist_str}] "
            f"md_err_delta={md_err_delta} ai=[{ai_stats}] lock_ttl={lock_ttl}s",
            flush=True,
        )

        if age > _MD_STALE_SEC and is_market_hours(market):
            anomalies.append((market, "MD_STALE", f"md_age={age:.0f}s threshold={_MD_STALE_SEC}s"))
        if md_err_delta > _MD_ERROR_SPIKE:
            anomalies.append((market, "MD_ERROR_SPIKE", f"delta={md_err_delta} threshold={_MD_ERROR_SPIKE}"))
        if ai_err_delta > _AI_ERROR_SPIKE:
            anomalies.append((market, "AI_ERROR_SPIKE", f"delta={ai_err_delta} threshold={_AI_ERROR_SPIKE}"))

    return anomalies


def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("signal_generator: ANTHROPIC_API_KEY not set — exiting", flush=True)
        sys.exit(1)

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("signal_generator: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)

    # 프로세스 락 — 중복 실행 방지
    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("signal_generator: already running (lock exists) — exiting", flush=True)
        sys.exit(0)
    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("signal_generator: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    print("signal_generator: lock acquired", flush=True)

    # 시작 시 pause 상태 확인 (경고만, 강제 설정 안 함)
    if not _is_paused(r):
        print(
            "signal_generator: WARN — claw:pause:global is not true. "
            "Orders may go through if Risk Gate passes. "
            "Set claw:pause:global=true for safe unattended operation.",
            flush=True,
        )
    else:
        print("signal_generator: claw:pause:global=true confirmed — safe mode", flush=True)

    data_guard = DataGuard(r)
    generator = AISignalGenerator(r)

    watchlist_kr = _parse_watchlist("GEN_WATCHLIST_KR")
    watchlist_us = _parse_watchlist("GEN_WATCHLIST_US")

    print(
        f"signal_generator: started poll_sec={_GEN_POLL_SEC} "
        f"kr_watchlist={watchlist_kr} us_watchlist={watchlist_us}",
        flush=True,
    )

    today = _today_kst()
    _health_state = {
        "last_log_ts": 0.0,
        "md_err_prev": {m: _get_md_error_total(r, m, today) for m in ("KR", "US")},
        "ai_err_prev": {m: _get_ai_error_total(r, m, today) for m in ("KR", "US")},
    }

    try:
        while True:
            # 락 TTL 갱신 (루프마다 리셋)
            r.expire(_LOCK_KEY, _LOCK_TTL)

            # 상태 체크 + 비정상 감지 (10분마다)
            # 이미 pause 상태면 상태 로그만 출력하고 중복 TG 알림은 보내지 않음
            anomalies = _health_check(r, watchlist_kr, watchlist_us, _health_state)
            if anomalies and not _is_paused(r):
                for market, reason, detail in anomalies:
                    _do_auto_pause(r, reason, market, detail)

            # 전역 일시정지 확인 — pause 상태에서는 AI 호출 없이 짧은 sleep
            if _is_paused(r):
                time.sleep(60)
                continue

            # AI 신호 생성
            for market, watchlist, max_size in [
                ("KR", watchlist_kr, _GEN_MAX_SIZE_CASH_KR),
                ("US", watchlist_us, _GEN_MAX_SIZE_CASH_US),
            ]:
                # DataGuard stale 체크 (warn-only)
                guard = data_guard.check(market)
                if not guard.allow:
                    print(f"signal_generator: md_stale {market} {guard.reason} — proceeding with caution", flush=True)
                elif guard.severity == "WARN":
                    print(f"signal_generator: md_stale_warn {market} {guard.reason}", flush=True)

                # 심볼 결정: 보유 포지션 우선(EXIT 관리), 워치리스트 추가
                raw_members = r.smembers(f"position_index:{market}")
                position_symbols = {m.decode() if isinstance(m, bytes) else m for m in raw_members}
                symbols = list(position_symbols) + [s for s in watchlist if s not in position_symbols]

                if not symbols:
                    continue

                for symbol in symbols:
                    try:
                        signal = generator.generate(market, symbol, max_size)
                        if signal:
                            r.lpush("claw:signal:queue", json.dumps(signal))
                            print(
                                f"signal_generator: emitted {market}:{symbol} "
                                f"dir={signal['direction']} size={signal['entry']['size_cash']}",
                                flush=True,
                            )
                    except Exception as e:
                        print(f"signal_generator: error {market}:{symbol} {e}", flush=True)

            time.sleep(_GEN_POLL_SEC)
    finally:
        r.delete(_LOCK_KEY)
        print("signal_generator: lock released", flush=True)


if __name__ == "__main__":
    main()
