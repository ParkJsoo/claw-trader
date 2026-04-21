"""coin_runtime_checkpoint - COIN runtime/resume snapshot to JSON."""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import redis

from app.coin_research import choose_resume_summary, compute_trade_summary, evaluate_resume_readiness
from app.coin_shadow import (
    compute_combined_shadow_summary,
    compute_pre_consensus_shadow_summary,
    compute_shadow_summary,
)

_KST = ZoneInfo("Asia/Seoul")


def _age_sec(now_ms: int, ts_ms: int | None) -> float | None:
    if not ts_ms:
        return None
    return round((now_ms - ts_ms) / 1000, 1)


def _get_ts_ms(raw: dict[str, str]) -> int | None:
    try:
        ts_ms = int(raw.get("ts_ms", "0") or 0)
    except ValueError:
        ts_ms = 0
    return ts_ms or None


def build_snapshot(r: redis.Redis, *, date_from: str, date_to: str) -> dict:
    now = datetime.now(_KST)
    now_ms = int(time.time() * 1000)
    today = now.strftime("%Y%m%d")

    watchlist = sorted(r.smembers("dynamic:watchlist:COIN"))
    scan5 = 0
    eval5 = 0
    eval30 = 0
    evaluated_in_last_scan = 0
    rows: list[dict[str, object]] = []

    for symbol in watchlist:
        scan_raw = r.hgetall(f"ai:dual:scan:last:COIN:{symbol}") or {}
        eval_raw = r.hgetall(f"ai:dual:last:claude:COIN:{symbol}") or {}

        scan_ts = _get_ts_ms(scan_raw)
        eval_ts = _get_ts_ms(eval_raw)
        scan_age = _age_sec(now_ms, scan_ts)
        eval_age = _age_sec(now_ms, eval_ts)
        scan_status = scan_raw.get("status")

        if scan_age is not None and scan_age <= 300:
            scan5 += 1
        if eval_age is not None and eval_age <= 300:
            eval5 += 1
        if eval_age is not None and eval_age <= 1800:
            eval30 += 1
        if scan_status == "evaluated":
            evaluated_in_last_scan += 1

        rows.append(
            {
                "symbol": symbol,
                "scan_status": scan_status,
                "scan_age_sec": scan_age,
                "eval_age_sec": eval_age,
                "emit": eval_raw.get("emit"),
                "reason": (eval_raw.get("reason") or "")[:100] if eval_raw else None,
            }
        )

    trade_summary = compute_trade_summary(r, date_from, date_to)
    shadow_summary = compute_shadow_summary(r, date_from, date_to)
    shadow_pre_summary = compute_pre_consensus_shadow_summary(r, date_from, date_to)
    shadow_all_summary = compute_combined_shadow_summary(r, date_from, date_to)
    selected = choose_resume_summary(
        trade_summary,
        shadow_summary,
        "auto",
        shadow_pre_summary=shadow_pre_summary,
        shadow_all_summary=shadow_all_summary,
    )
    resume_check = evaluate_resume_readiness(selected["summary"])

    return {
        "captured_at_kst": now.isoformat(),
        "today": today,
        "pause_global": r.get("claw:pause:global"),
        "pause_coin": r.get("claw:pause:COIN"),
        "watchlist_count": len(watchlist),
        "scan_coverage_5m_pct": round((scan5 / len(watchlist)) * 100, 1) if watchlist else 0.0,
        "eval_coverage_5m_pct": round((eval5 / len(watchlist)) * 100, 1) if watchlist else 0.0,
        "eval_coverage_30m_pct": round((eval30 / len(watchlist)) * 100, 1) if watchlist else 0.0,
        "evaluated_in_last_scan": evaluated_in_last_scan,
        "scan_stats": r.hgetall(f"ai:dual_stats:consensus:COIN:{today}"),
        "consensus_stats": r.hgetall(f"consensus:stats:COIN:{today}"),
        "execution_funnel": r.hgetall(f"execution_funnel:COIN:{today}"),
        "perf_daily": r.hgetall(f"perf:daily:COIN:{today}"),
        "selected_ledger": selected["selected_ledger"],
        "resume_check": resume_check,
        "trade_summary": trade_summary,
        "shadow_summary": shadow_summary,
        "shadow_pre_summary": shadow_pre_summary,
        "shadow_all_summary": shadow_all_summary,
        "watchlist_rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="COIN runtime checkpoint snapshot")
    parser.add_argument("--date-from", default="20260421", help="resume window start YYYYMMDD")
    parser.add_argument("--date-to", default="20260421", help="resume window end YYYYMMDD")
    parser.add_argument("--tag", required=True, help="checkpoint tag, e.g. 1830")
    parser.add_argument("--out-dir", default=".codex/coin_checkpoints", help="output directory")
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_runtime_checkpoint: REDIS_URL not set", file=sys.stderr, flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)
    snapshot = build_snapshot(r, date_from=args.date_from, date_to=args.date_to)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now(_KST).strftime('%Y%m%d')}_{args.tag}_coin_checkpoint.json"
    out_path = out_dir / filename
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path), flush=True)


if __name__ == "__main__":
    main()
