"""coin_type_b_reject_report - COIN Type B reject/bottleneck summary."""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import redis

from app.coin_type_b_reject_insights import summarize_reject_samples
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

def _load_reject_samples(r: redis.Redis, *, date: str, reason: str) -> list[dict[str, object]]:
    key = f"consensus:type_b:reject_samples:COIN:{date}:{reason}"
    rows = r.lrange(key, 0, -1) or []
    parsed: list[dict[str, object]] = []
    for row in rows:
        try:
            parsed.append(json.loads(_decode(row)))
        except Exception:
            continue
    return parsed


_TYPE_B_REJECT_LOG_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\]\s+consensus:\s+type_b\.reject\.(?P<reason>[a-z0-9_]+)\s+(?P<rest>.+)$"
)
_LOG_KV_RE = re.compile(r"(?P<key>[a-z0-9_]+)=(?P<value>[^\s]+)")


def _parse_float_token(raw: str) -> float | None:
    token = (raw or "").strip()
    if not token:
        return None
    if token.endswith("%"):
        token = token[:-1]
    try:
        return float(token)
    except ValueError:
        return None


def _load_log_reject_samples(*, log_path: str, dates: list[str]) -> dict[str, list[dict[str, object]]]:
    path = Path(log_path)
    if not path.exists():
        return {}

    date_set = {
        f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        for date in dates
    }
    grouped: dict[str, list[dict[str, object]]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = _TYPE_B_REJECT_LOG_RE.match(line.strip())
            if not match:
                continue
            if match.group("date") not in date_set:
                continue

            reason = f"reject_{match.group('reason')}"
            sample: dict[str, object] = {}
            for kv in _LOG_KV_RE.finditer(match.group("rest")):
                key = kv.group("key")
                raw_value = kv.group("value")
                if key == "symbol":
                    sample[key] = raw_value
                else:
                    parsed = _parse_float_token(raw_value)
                    if parsed is not None:
                        if key == "change_rate":
                            sample[key] = parsed / 100.0
                        else:
                            sample[key] = parsed
            if sample:
                grouped.setdefault(reason, []).append(sample)
    return grouped


def build_report(
    r: redis.Redis,
    *,
    date_from: str,
    date_to: str,
    log_path: str = "logs/consensus_signal.log",
) -> dict[str, object]:
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
    sample_insights: dict[str, dict[str, object]] = {}
    for reason in reject_counts:
        samples: list[dict[str, object]] = []
        for date in dates:
            samples.extend(_load_reject_samples(r, date=date, reason=reason))
        if samples:
            insight = summarize_reject_samples(reason, samples)
            insight["source"] = "redis_samples"
            sample_insights[reason] = insight

    log_samples = _load_log_reject_samples(log_path=log_path, dates=dates)
    for reason, samples in log_samples.items():
        if reason not in reject_counts or reason in sample_insights or not samples:
            continue
        insight = summarize_reject_samples(reason, samples)
        insight["source"] = "consensus_log"
        sample_insights[reason] = insight

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
            "sample_insights": sample_insights,
        },
        "days": days,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="COIN Type B reject/bottleneck report")
    parser.add_argument("--date-from", dest="date_from", default=today_kst(), help="KST start date YYYYMMDD")
    parser.add_argument("--date-to", dest="date_to", default=today_kst(), help="KST end date YYYYMMDD")
    parser.add_argument("--log-path", dest="log_path", default="logs/consensus_signal.log", help="consensus log path for fallback sampling")
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_type_b_reject_report: REDIS_URL not set", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)
    report = build_report(r, date_from=args.date_from, date_to=args.date_to, log_path=args.log_path)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
