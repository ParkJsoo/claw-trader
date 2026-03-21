"""daily_report_runner — 장 마감 후 성과 리포트 자동 발송.

KST 15:40 (KR 장 마감 40분 후) 자동 실행.
하루 1회 발송 (Redis 키로 중복 방지).

기동:
    PYTHONPATH=src venv/bin/python -m scripts.daily_report_runner
"""
from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from app.performance_reporter import PerformanceReporter
from guards.notifier import send_telegram
from utils.redis_helpers import today_kst

_KST = ZoneInfo("Asia/Seoul")
_POLL_SEC = 60  # 1분마다 체크
_KR_REPORT_HOUR = 15
_KR_REPORT_MIN = 40
_REPORT_DONE_TTL = 20 * 3600  # 당일 중복 발송 방지


def _already_sent(r, market: str, date_str: str) -> bool:
    return bool(r.exists(f"perf:report_sent:{market}:{date_str}"))


def _mark_sent(r, market: str, date_str: str) -> None:
    r.set(f"perf:report_sent:{market}:{date_str}", "1", ex=_REPORT_DONE_TTL)


def _send_report(r, market: str) -> None:
    reporter = PerformanceReporter(r)
    date_str = today_kst()
    stats = reporter.compute_and_save(market, date_str)
    msg = reporter.format_report(market, stats)
    send_telegram(msg)
    _mark_sent(r, market, date_str)
    print(f"daily_report: sent {market} {date_str}", flush=True)


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("daily_report: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)
    print(f"daily_report: started poll_sec={_POLL_SEC} KR_report={_KR_REPORT_HOUR}:{_KR_REPORT_MIN:02d} KST", flush=True)

    try:
        while True:
            now_kst = datetime.now(_KST)
            date_str = today_kst()

            # KR: 15:40 KST 이후이면 발송
            if (now_kst.hour > _KR_REPORT_HOUR or
                    (now_kst.hour == _KR_REPORT_HOUR and now_kst.minute >= _KR_REPORT_MIN)):
                if not _already_sent(r, "KR", date_str):
                    try:
                        _send_report(r, "KR")
                    except Exception as e:
                        print(f"daily_report: error KR {e}", flush=True)

            time.sleep(_POLL_SEC)
    except KeyboardInterrupt:
        print("daily_report: stopped", flush=True)


if __name__ == "__main__":
    main()
