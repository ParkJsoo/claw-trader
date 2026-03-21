"""daily_report_runner — 장 마감 후 성과 리포트 자동 발송 + Daily cap 리셋 + 자동 튜닝.

KST 08:55 — strategy:daily_count 자동 리셋 (장 시작 전)
KST 15:40 — KR 성과 리포트 자동 발송 + 파라미터 자동 튜닝

기동:
    PYTHONPATH=src venv/bin/python -m scripts.daily_report_runner
"""
from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis

from app.performance_reporter import PerformanceReporter
from guards.notifier import send_telegram
from utils.redis_helpers import today_kst, get_config

_KST = ZoneInfo("Asia/Seoul")
_POLL_SEC = 60  # 1분마다 체크
_KR_REPORT_HOUR = 15
_KR_REPORT_MIN = 40
_REPORT_DONE_TTL = 20 * 3600  # 당일 중복 발송 방지

_DAILY_RESET_HOUR = 8
_DAILY_RESET_MIN = 55
_DAILY_RESET_TTL = 3600  # 1시간 TTL (중복 방지)

_AUTO_TUNE_LOOKBACK_DAYS = 5


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


def _reset_daily_cap(r, date_str: str) -> None:
    """strategy:daily_count KR/US 리셋 (장 시작 전 08:55)."""
    reset_key = f"claw:daily_reset:{date_str}"
    if r.exists(reset_key):
        return  # 이미 리셋함

    r.delete(f"strategy:daily_count:KR:{date_str}")
    r.delete(f"strategy:daily_count:US:{date_str}")
    r.set(reset_key, "1", ex=_DAILY_RESET_TTL)

    try:
        send_telegram("[CLAW] 🔄 Daily cap 리셋 완료 (KR/US)")
    except Exception:
        pass
    print(f"daily_report: daily_cap_reset {date_str}", flush=True)


def _auto_tune(r, market: str) -> None:
    """최근 5거래일 성과 기반 파라미터 자동 조정."""
    reporter = PerformanceReporter(r)
    now_kst = datetime.now(_KST)

    win_rates = []
    max_drawdowns = []

    for i in range(1, _AUTO_TUNE_LOOKBACK_DAYS + 1):
        day = now_kst - timedelta(days=i)
        date_str = day.strftime("%Y%m%d")
        stats = reporter.get_daily_stats(market, date_str)
        if not stats:
            continue
        try:
            wr = float(stats.get("win_rate", "0"))
            win_rates.append(wr / 100.0)  # % → 소수
        except (ValueError, TypeError):
            pass
        try:
            md = float(stats.get("max_drawdown", "0"))
            max_drawdowns.append(md)
        except (ValueError, TypeError):
            pass

    if not win_rates:
        return  # 데이터 없음

    avg_win_rate = sum(win_rates) / len(win_rates)
    avg_max_dd = sum(max_drawdowns) / len(max_drawdowns) if max_drawdowns else 0.0

    changes: list[str] = []
    config_key = f"claw:config:{market}"

    current_stop = get_config(r, market, "stop_pct", 0.015)
    current_take = get_config(r, market, "take_pct", 0.030)
    current_size = get_config(r, market, "size_cash_pct", 0.20)

    new_stop = current_stop
    new_take = current_take
    new_size = current_size

    if avg_win_rate < 0.40:
        # 승률 낮음: stop 축소(손실 줄이기), take 확대(수익 늘리기)
        new_stop = round(max(current_stop * 0.90, 0.005), 4)
        new_take = round(min(current_take * 1.10, 0.10), 4)
    elif avg_win_rate > 0.60:
        # 승률 높음: stop 확대(홀딩 여유), take 축소(빠른 수익 실현)
        new_stop = round(min(current_stop * 1.10, 0.05), 4)
        new_take = round(max(current_take * 0.90, 0.01), 4)

    _MAX_DD_THRESHOLD = {"KR": 50000, "US": 50}
    threshold = _MAX_DD_THRESHOLD.get(market, 50000)
    if avg_max_dd > threshold:
        # 최대 낙폭 큼: 포지션 크기 하향
        new_size = round(max(current_size - 0.05, 0.10), 4)

    if new_stop != current_stop:
        r.hset(config_key, "stop_pct", str(new_stop))
        changes.append(f"stop_pct: {current_stop} → {new_stop}")
    if new_take != current_take:
        r.hset(config_key, "take_pct", str(new_take))
        changes.append(f"take_pct: {current_take} → {new_take}")
    if new_size != current_size:
        r.hset(config_key, "size_cash_pct", str(new_size))
        changes.append(f"size_cash_pct: {current_size} → {new_size}")

    if changes:
        msg = f"[CLAW] 🔧 자동 튜닝 ({market})\n" + "\n".join(changes)
        try:
            send_telegram(msg)
        except Exception:
            pass
        print(f"daily_report: auto_tune {market} {changes}", flush=True)
    else:
        print(f"daily_report: auto_tune {market} no changes (win_rate={avg_win_rate:.2%} max_dd={avg_max_dd:.4f})", flush=True)


def main() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("daily_report: REDIS_URL not set — exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)
    print(
        f"daily_report: started poll_sec={_POLL_SEC} "
        f"KR_report={_KR_REPORT_HOUR}:{_KR_REPORT_MIN:02d} KST "
        f"daily_reset={_DAILY_RESET_HOUR}:{_DAILY_RESET_MIN:02d} KST",
        flush=True,
    )

    try:
        while True:
            now_kst = datetime.now(_KST)
            date_str = today_kst()

            # 08:55~08:59 KST — daily cap 리셋
            if (now_kst.hour == _DAILY_RESET_HOUR and
                    _DAILY_RESET_MIN <= now_kst.minute <= 59):
                try:
                    _reset_daily_cap(r, date_str)
                except Exception as e:
                    print(f"daily_report: daily_cap_reset error {e}", flush=True)

            # KR: 15:40 KST 이후이면 발송
            if (now_kst.hour > _KR_REPORT_HOUR or
                    (now_kst.hour == _KR_REPORT_HOUR and now_kst.minute >= _KR_REPORT_MIN)):
                if not _already_sent(r, "KR", date_str):
                    try:
                        _send_report(r, "KR")
                    except Exception as e:
                        print(f"daily_report: error KR {e}", flush=True)
                    # 리포트 발송 직후 자동 튜닝
                    try:
                        _auto_tune(r, "KR")
                    except Exception as e:
                        print(f"daily_report: auto_tune error KR {e}", flush=True)

            time.sleep(_POLL_SEC)
    except KeyboardInterrupt:
        print("daily_report: stopped", flush=True)


if __name__ == "__main__":
    main()
