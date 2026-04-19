"""COIN shadow runner.

pause 상태에서도 signal snapshot을 주기적으로 후행 평가해 shadow ledger를 채운다.
"""
from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
import sys
import time

import redis

from app.coin_shadow import evaluate_pending_signals

_LOCK_KEY = "coin:shadow:runner:lock"
_LOCK_TTL = 120
_POLL_SEC = float(os.getenv("COIN_SHADOW_POLL_SEC", "60"))


def _log(msg: str) -> None:
    print(f"coin_shadow: {msg}", flush=True)


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        _log("REDIS_URL not set — exiting")
        sys.exit(1)

    r = redis.from_url(redis_url)

    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        _log("already running (lock exists) — exiting")
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        _log("SIGTERM received, lock released")
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    _log(f"started poll_sec={_POLL_SEC}")

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            stats = evaluate_pending_signals(r)
            _log(
                "scan "
                f"scanned={stats['scanned']} "
                f"completed={stats['completed']} "
                f"pending={stats['pending']} "
                f"skipped_existing={stats['skipped_existing']} "
                f"skipped_invalid={stats['skipped_invalid']}"
            )
            time.sleep(_POLL_SEC)
    finally:
        r.delete(_LOCK_KEY)
        _log("lock released")
