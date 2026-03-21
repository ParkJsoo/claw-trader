"""daily_report_runner — 장 마감 후 성과 리포트 자동 발송 + Daily cap 리셋 + 자동 튜닝.

KST 08:55 — strategy:daily_count 자동 리셋 (장 시작 전)
KST 15:40 — KR 성과 리포트 자동 발송 + 파라미터 자동 튜닝

기동:
    PYTHONPATH=src venv/bin/python -m scripts.daily_report_runner
"""
from dotenv import load_dotenv
load_dotenv()

import os
import signal as _signal
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

_LOCK_KEY = "daily_report:runner:lock"
_LOCK_TTL = 120


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


def _reset_daily_cap(r, market: str) -> None:
    """strategy:daily_count {market} 리셋 (장 시작 전 08:55).

    오늘 + 어제 날짜 키를 모두 삭제하여 전날 잔여 카운트도 정리.
    """
    today = today_kst()
    yesterday = (datetime.strptime(today, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    reset_key = f"claw:daily_reset:{market}:{today}"
    if r.exists(reset_key):
        return

    deleted = 0
    for date_str in [today, yesterday]:
        key = f"strategy:daily_count:{market}:{date_str}"
        deleted += r.delete(key)

    from utils.redis_helpers import secs_until_kst_midnight
    ttl = secs_until_kst_midnight()
    r.set(reset_key, "1", ex=max(ttl, 3600))

    # pnl:{market} realized_pnl 일별 리셋 (킬스위치 오작동 방지)
    # US 시장은 장중일 수 있으므로 KR만 무조건 리셋, US는 장 외 시간만 리셋
    from utils.redis_helpers import is_market_hours
    if market != "US" or not is_market_hours("US"):
        r.hset(f"pnl:{market}", "realized_pnl", "0")

    print(f"daily_report: daily_cap_reset {market} (deleted={deleted})", flush=True)
    try:
        send_telegram(f"[CLAW] 🔄 Daily cap 리셋 완료 ({market})\n{today} + {yesterday} 키 삭제")
    except Exception:
        pass


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

    config_key = f"claw:config:{market}"

    current_stop = get_config(r, market, "stop_pct", 0.015)
    current_take = get_config(r, market, "take_pct", 0.030)
    current_size = get_config(r, market, "size_cash_pct", 0.20)

    changes: dict[str, str] = {}

    if avg_win_rate < 0.40:
        # 승률 낮음: stop 확대(더 많은 가격 변동 허용) + size 축소
        new_stop = round(min(float(current_stop) * 1.10, 0.05), 4)
        new_size = round(max(float(current_size) - 0.05, 0.10), 4)
        changes["stop_pct"] = str(new_stop)
        changes["size_cash_pct"] = str(new_size)
    elif avg_win_rate > 0.60:
        # 승률 높음: stop 소폭 축소(리스크 감소)
        new_stop = round(max(float(current_stop) * 0.95, 0.015), 4)  # KR 시장 최소 1.5%
        changes["stop_pct"] = str(new_stop)

    _MAX_DD_THRESHOLD = {"KR": 50000, "US": 50}
    threshold = _MAX_DD_THRESHOLD.get(market, 50000)
    if avg_max_dd > threshold:
        # 최대 낙폭 큼: 포지션 크기 하향 (win_rate 분기와 별개로 적용)
        new_size = round(max(float(current_size) - 0.05, 0.10), 4)
        changes["size_cash_pct"] = str(new_size)

    if changes:
        for param_key, param_val in changes.items():
            r.hset(config_key, param_key, param_val)
        msg = f"[CLAW] 🔧 자동 튜닝 ({market})\n" + "\n".join(f"{k}: → {v}" for k, v in changes.items())
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

    # 프로세스 락 (중복 실행 방지)
    if not r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        print("daily_report: already running — exiting", flush=True)
        sys.exit(0)

    def _handle_sigterm(signum, frame):
        r.delete(_LOCK_KEY)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    print(
        f"daily_report: started poll_sec={_POLL_SEC} "
        f"KR_report={_KR_REPORT_HOUR}:{_KR_REPORT_MIN:02d} KST "
        f"daily_reset={_DAILY_RESET_HOUR}:{_DAILY_RESET_MIN:02d} KST",
        flush=True,
    )

    try:
        while True:
            r.expire(_LOCK_KEY, _LOCK_TTL)
            now_kst = datetime.now(_KST)
            date_str = today_kst()

            # 08:55~08:59 KST — daily cap 리셋
            if (now_kst.hour == _DAILY_RESET_HOUR and
                    _DAILY_RESET_MIN <= now_kst.minute < _DAILY_RESET_MIN + 5):
                try:
                    _reset_daily_cap(r, "KR")
                    _reset_daily_cap(r, "US")
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
    finally:
        r.delete(_LOCK_KEY)
        print("daily_report: stopped, lock released", flush=True)


if __name__ == "__main__":
    main()
