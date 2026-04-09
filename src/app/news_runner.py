"""뉴스 인텔리전스 러너 — 독립 프로세스.

30분 폴링으로 DART 공시 + Google News + Yahoo Finance RSS 수집,
Qwen으로 분류/요약 후 Redis 저장.

기동:
    PYTHONPATH=src ../venv/bin/python -m app.news_runner
"""
from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from news.collector import collect_all
from news.classifier import classify_batch
from news.redis_writer import write_batch
from utils.redis_helpers import today_kst, load_watchlist

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
_LOCK_KEY = "news:runner:lock"
_LOCK_TTL = 90             # 30초 갱신 주기 × 3 (크래시 시 90초 내 자동 해제)

_POLL_SEC = float(os.getenv("NEWS_POLL_SEC", "1800"))     # 기본 30분
_MAX_ITEMS = int(os.getenv("NEWS_MAX_ITEMS", "100"))
_QWEN_CLASSIFY = os.getenv("NEWS_QWEN_CLASSIFY", "true").lower() in ("true", "1", "yes")
_DART_API_KEY = os.getenv("DART_API_KEY", "")
_KST = ZoneInfo("Asia/Seoul")

# ---------------------------------------------------------------------------
# 런타임
# ---------------------------------------------------------------------------

def _get_watchlists(r) -> tuple[list[str], list[str]]:
    us = load_watchlist(r, "US", "GEN_WATCHLIST_US") if os.getenv("IBKR_ACCOUNT_ID") else []
    return load_watchlist(r, "KR", "GEN_WATCHLIST_KR"), us


def _run_once(r, today: str, kr_watchlist: list[str], us_watchlist: list[str]) -> None:
    """수집 → 분류 → 저장 한 사이클."""
    ts_start = time.time()

    # 1. 수집
    items = collect_all(
        dart_api_key=_DART_API_KEY,
        kr_watchlist=kr_watchlist,
        us_watchlist=us_watchlist,
        date_str=today,
        max_per_query=8,
    )

    if not items:
        print("news: no items collected", flush=True)
        return

    # 2. Qwen 분류
    if _QWEN_CLASSIFY:
        t_classify = time.time()
        print(f"news: classify_start items={len(items)}", flush=True)
        items = classify_batch(items, enabled=True)
        classify_elapsed = time.time() - t_classify
        classified = sum(1 for i in items if i.classified)
        print(f"news: classified={classified}/{len(items)} classify_elapsed={classify_elapsed:.1f}s", flush=True)
    else:
        print("news: qwen_classify=off, skipping classification", flush=True)

    # 3. Redis 저장
    saved, skipped = write_batch(r, items, today)
    elapsed = time.time() - ts_start
    print(
        f"news: cycle done saved={saved} skipped={skipped} "
        f"total={len(items)} elapsed={elapsed:.1f}s",
        flush=True,
    )

    # 4. 통계 로그
    stats_kr = r.hgetall(f"news:stats:KR:{today}") or {}
    stats_us = r.hgetall(f"news:stats:US:{today}") or {}
    def _fmt(d: dict) -> str:
        return " ".join(
            f"{(k.decode() if isinstance(k, bytes) else k)}="
            f"{(v.decode() if isinstance(v, bytes) else v)}"
            for k, v in sorted(d.items())
        )
    print(f"news: stats KR [{_fmt(stats_kr)}]", flush=True)
    print(f"news: stats US [{_fmt(stats_us)}]", flush=True)


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("news: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    if not _DART_API_KEY:
        print("news: WARNING — DART_API_KEY not set, DART collection disabled", flush=True)

    r = redis.from_url(redis_url)

    # 프로세스 락 (중복 방지)
    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("news: already running (lock exists) — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        print("news: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    kr_watchlist, us_watchlist = _get_watchlists(r)
    print(
        f"news: started poll_sec={_POLL_SEC} "
        f"qwen_classify={_QWEN_CLASSIFY} "
        f"dart={'on' if _DART_API_KEY else 'off'} "
        f"kr={kr_watchlist} us={us_watchlist}",
        flush=True,
    )

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            today = today_kst()
            # 매 폴링 시 동적 워치리스트 갱신 (watchlist_selector 변경 반영)
            kr_watchlist, us_watchlist = _get_watchlists(r)
            try:
                _run_once(r, today, kr_watchlist, us_watchlist)
            except Exception as e:
                print(f"news: run_once error {e}", flush=True)

            # 다음 폴링까지 대기 (30초 단위로 lock 갱신)
            remaining = _POLL_SEC
            while remaining > 0:
                sleep_chunk = min(30.0, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk
                r.expire(_LOCK_KEY, _LOCK_TTL)

    finally:
        r.delete(_LOCK_KEY)
        print("news: lock released", flush=True)


if __name__ == "__main__":
    main()
