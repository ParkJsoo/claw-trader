from dotenv import load_dotenv
load_dotenv()

import json
import os
import sys
import time
from decimal import Decimal

import redis

from guards.data_guard import DataGuard
from ai.generator import AISignalGenerator

_GEN_POLL_SEC = float(os.getenv("GEN_POLL_SEC", "60"))
_GEN_MAX_SIZE_CASH_KR = Decimal(os.getenv("GEN_MAX_SIZE_CASH_KR", "500000"))
_GEN_MAX_SIZE_CASH_US = Decimal(os.getenv("GEN_MAX_SIZE_CASH_US", "1000"))

_LOCK_KEY = "gen:runner:lock"
_LOCK_TTL = 120  # seconds — must be > GEN_POLL_SEC + max processing time


def _parse_watchlist(env_key: str) -> list[str]:
    raw = os.getenv(env_key, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


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
    print("signal_generator: lock acquired", flush=True)

    data_guard = DataGuard(r)
    generator = AISignalGenerator(r)

    watchlist_kr = _parse_watchlist("GEN_WATCHLIST_KR")
    watchlist_us = _parse_watchlist("GEN_WATCHLIST_US")

    print(
        f"signal_generator: started poll_sec={_GEN_POLL_SEC} "
        f"kr_watchlist={watchlist_kr} us_watchlist={watchlist_us}",
        flush=True,
    )

    try:
        while True:
            # 락 TTL 갱신 (루프마다 리셋)
            r.expire(_LOCK_KEY, _LOCK_TTL)

            for market, watchlist, max_size in [
                ("KR", watchlist_kr, _GEN_MAX_SIZE_CASH_KR),
                ("US", watchlist_us, _GEN_MAX_SIZE_CASH_US),
            ]:
                # DataGuard stale 체크 (warn-only — cold start 가드가 개별 심볼을 게이팅)
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
