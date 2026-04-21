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

from app.coin_research import evaluate_resume_readiness
from app.coin_shadow import compute_shadow_summary, evaluate_pending_pre_consensus_signals, evaluate_pending_signals
from guards.notifier import send_telegram
from utils.redis_helpers import today_kst

_LOCK_KEY = "coin:shadow:runner:lock"
_LOCK_TTL = 120
_POLL_SEC = float(os.getenv("COIN_SHADOW_POLL_SEC", "60"))
_TYPE_B_ALERT_TTL = 7 * 86400


def _log(msg: str) -> None:
    print(f"coin_shadow: {msg}", flush=True)


def _type_b_alert_thresholds() -> list[int]:
    raw = os.getenv("COIN_TYPE_B_ALERT_TRADE_COUNTS", "10,20")
    thresholds: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if value > 0:
            thresholds.append(value)
    return sorted(set(thresholds))


def _type_b_only_readiness_summary(summary: dict) -> dict:
    type_b_stats = (summary.get("by_signal_family", {}) or {}).get("type_b", {}) or {}
    return {
        "overall": type_b_stats,
        "by_signal_family": {
            "type_b": type_b_stats,
            "type_a": {},
        },
    }


def _build_type_b_shadow_alerts(summary: dict, *, today: str) -> dict[str, str]:
    type_b_stats = (summary.get("by_signal_family", {}) or {}).get("type_b", {}) or {}
    trade_count = int(type_b_stats.get("trade_count", 0) or 0)
    if trade_count <= 0:
        return {}

    alerts: dict[str, str] = {}
    win_rate = float(type_b_stats.get("win_rate", 0.0) or 0.0)
    net_pnl = float(type_b_stats.get("net_pnl", 0.0) or 0.0)
    profit_factor = float(type_b_stats.get("profit_factor", 0.0) or 0.0)
    avg_pnl = float(type_b_stats.get("avg_pnl", 0.0) or 0.0)

    for threshold in _type_b_alert_thresholds():
        if trade_count >= threshold:
            alerts[f"count:{threshold}"] = (
                f"[CLAW] COIN Type B shadow {threshold} trades reached ({today})\n"
                f"trade_count={trade_count}\n"
                f"win_rate={win_rate:.1f}% pf={profit_factor:.2f}\n"
                f"net_pnl={net_pnl:.2f} avg_pnl={avg_pnl:.2f}"
            )

    readiness = evaluate_resume_readiness(_type_b_only_readiness_summary(summary))
    type_b_eval = ((readiness.get("evaluations") or {}).get("type_b") or {})
    if type_b_eval.get("ready"):
        alerts["ready"] = (
            f"[CLAW] COIN Type B shadow ready candidate ({today})\n"
            f"trade_count={trade_count}\n"
            f"win_rate={win_rate:.1f}% pf={profit_factor:.2f}\n"
            f"net_pnl={net_pnl:.2f} avg_pnl={avg_pnl:.2f}\n"
            "next_step=review COIN pause release for type_b_only canary"
        )

    return alerts


def _maybe_notify_type_b_shadow_progress(r, *, today: str | None = None) -> list[str]:
    today = today or today_kst()
    summary = compute_shadow_summary(r, today, today)
    alerts = _build_type_b_shadow_alerts(summary, today=today)
    sent_tags: list[str] = []

    for tag, message in alerts.items():
        alert_key = f"coin:type_b_shadow_alert:{today}:{tag}"
        if r.set(alert_key, "1", nx=True, ex=_TYPE_B_ALERT_TTL):
            send_telegram(message)
            sent_tags.append(tag)

    return sent_tags


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
            pre_stats = evaluate_pending_pre_consensus_signals(r)
            _log(
                "scan "
                f"scanned={stats['scanned']} "
                f"completed={stats['completed']} "
                f"pending={stats['pending']} "
                f"skipped_existing={stats['skipped_existing']} "
                f"skipped_invalid={stats['skipped_invalid']} "
                f"pre_scanned={pre_stats['scanned']} "
                f"pre_completed={pre_stats['completed']} "
                f"pre_pending={pre_stats['pending']} "
                f"pre_skipped_existing={pre_stats['skipped_existing']} "
                f"pre_skipped_invalid={pre_stats['skipped_invalid']}"
            )
            alert_tags = _maybe_notify_type_b_shadow_progress(r)
            if alert_tags:
                _log(f"type_b_alerts sent={','.join(alert_tags)}")
            time.sleep(_POLL_SEC)
    finally:
        r.delete(_LOCK_KEY)
        _log("lock released")
