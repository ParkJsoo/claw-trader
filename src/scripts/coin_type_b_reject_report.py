"""coin_type_b_reject_report - COIN Type B reject/bottleneck summary."""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

import redis

from utils.redis_helpers import today_kst


def _iter_dates(date_from: str, date_to: str) -> list[str]:
    start = datetime.strptime(date_from, "%Y%m%d")
    end = datetime.strptime(date_to, "%Y%m%d")
    if end < start:
        raise ValueError("date_to must be >= date_from")
    dates: list[str] = []
    cursor = start
    while cursor <= end:
        dates.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return dates


def _to_int(raw: str | int | None) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _decode(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return "" if value is None else str(value)


def _build_day_summary(date: str, raw: dict[str, str]) -> dict[str, object]:
    normalized = {_decode(key): _decode(value) for key, value in raw.items()}
    scanned = _to_int(normalized.get("scanned"))
    candidate = _to_int(normalized.get("candidate"))
    shadow_candidate = _to_int(normalized.get("shadow_candidate"))
    reject_counts = {
        key: _to_int(value)
        for key, value in normalized.items()
        if key.startswith("reject_") and _to_int(value) > 0
    }
    reject_total = sum(reject_counts.values())
    top_rejects = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reject_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    pass_total = candidate + shadow_candidate
    return {
        "date": date,
        "scanned": scanned,
        "candidate": candidate,
        "shadow_candidate": shadow_candidate,
        "pass_total": pass_total,
        "candidate_rate_pct": round((pass_total / scanned) * 100, 2) if scanned else 0.0,
        "reject_total": reject_total,
        "reject_counts": reject_counts,
        "top_rejects": top_rejects,
    }


def build_report(r: redis.Redis, *, date_from: str, date_to: str) -> dict[str, object]:
    dates = _iter_dates(date_from, date_to)
    aggregated = Counter()
    days: list[dict[str, object]] = []

    for date in dates:
        raw = r.hgetall(f"consensus:type_b:stats:COIN:{date}") or {}
        day = _build_day_summary(date, raw)
        days.append(day)
        aggregated["scanned"] += int(day["scanned"])
        aggregated["candidate"] += int(day["candidate"])
        aggregated["shadow_candidate"] += int(day["shadow_candidate"])
        for reason, count in day["reject_counts"].items():
            aggregated[reason] += count

    reject_counts = {
        key: value
        for key, value in aggregated.items()
        if key.startswith("reject_") and value > 0
    }
    reject_total = sum(reject_counts.values())
    pass_total = aggregated["candidate"] + aggregated["shadow_candidate"]
    top_rejects = [
        {
            "reason": reason,
            "count": count,
            "share_pct": round((count / reject_total) * 100, 2) if reject_total else 0.0,
        }
        for reason, count in sorted(reject_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]

    return {
        "date_from": date_from,
        "date_to": date_to,
        "summary": {
            "scanned": aggregated["scanned"],
            "candidate": aggregated["candidate"],
            "shadow_candidate": aggregated["shadow_candidate"],
            "pass_total": pass_total,
            "candidate_rate_pct": round((pass_total / aggregated["scanned"]) * 100, 2) if aggregated["scanned"] else 0.0,
            "reject_total": reject_total,
            "top_rejects": top_rejects,
            "reject_counts": reject_counts,
        },
        "days": days,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="COIN Type B reject/bottleneck report")
    parser.add_argument("--date-from", dest="date_from", default=today_kst(), help="KST start date YYYYMMDD")
    parser.add_argument("--date-to", dest="date_to", default=today_kst(), help="KST end date YYYYMMDD")
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_type_b_reject_report: REDIS_URL not set", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)
    report = build_report(r, date_from=args.date_from, date_to=args.date_to)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
