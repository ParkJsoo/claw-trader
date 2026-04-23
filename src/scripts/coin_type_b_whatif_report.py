"""coin_type_b_whatif_report - COIN Type B gate what-if analysis."""
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

from app.coin_type_b_gate_profiles import default_scenarios, first_gate_fail_reason
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


def _decode(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return "" if value is None else str(value)


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _load_scan_samples(r: redis.Redis, *, date: str) -> list[dict[str, object]]:
    key = f"consensus:type_b:scan_samples:COIN:{date}"
    rows = r.lrange(key, 0, -1) or []
    parsed: list[dict[str, object]] = []
    for row in rows:
        try:
            raw = json.loads(_decode(row))
        except Exception:
            continue
        parsed.append(
            {
                "date": date,
                "symbol": _decode(raw.get("symbol")),
                "status": _decode(raw.get("status")),
                "reason_code": _decode(raw.get("reason_code")),
                "signal_mode": _decode(raw.get("signal_mode")),
                "change_rate": _to_float(raw.get("change_rate")),
                "near_high": _to_float(raw.get("near_high")),
                "ret_5m": _to_float(raw.get("ret_5m")),
                "trade_price": _to_float(raw.get("trade_price")),
                "high_price": _to_float(raw.get("high_price")),
                "vol_24h": _to_float(raw.get("vol_24h")),
                "ob_ratio": _to_float(raw.get("ob_ratio")),
            }
        )
    return parsed

def _evaluate_scenario(samples: list[dict[str, object]], *, name: str, thresholds: dict[str, object], baseline_fails: list[str] | None = None) -> dict[str, object]:
    fail_counts = Counter()
    newly_unblocked = Counter()
    pass_count = 0
    pass_symbols: list[str] = []

    for idx, sample in enumerate(samples):
        fail_reason = first_gate_fail_reason(sample, thresholds)
        fail_counts[fail_reason] += 1
        if fail_reason == "pass_pre_ai":
            pass_count += 1
            symbol = _decode(sample.get("symbol"))
            if symbol and symbol not in pass_symbols and len(pass_symbols) < 10:
                pass_symbols.append(symbol)
            if baseline_fails is not None and idx < len(baseline_fails) and baseline_fails[idx] != "pass_pre_ai":
                newly_unblocked[baseline_fails[idx]] += 1

    sample_count = len(samples)
    return {
        "name": name,
        "thresholds": thresholds,
        "sample_count": sample_count,
        "pre_ai_pass_count": pass_count,
        "pre_ai_pass_rate_pct": round((pass_count / sample_count) * 100, 2) if sample_count else 0.0,
        "pass_symbols": pass_symbols,
        "top_blockers": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                ((reason, count) for reason, count in fail_counts.items() if reason != "pass_pre_ai" and count > 0),
                key=lambda item: (-item[1], item[0]),
            )[:5]
        ],
        "newly_unblocked_from": [
            {"reason": reason, "count": count}
            for reason, count in sorted(newly_unblocked.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def build_report(r: redis.Redis, *, date_from: str, date_to: str) -> dict[str, object]:
    dates = _iter_dates(date_from, date_to)
    samples: list[dict[str, object]] = []
    for date in dates:
        samples.extend(_load_scan_samples(r, date=date))

    actual_status_counts = Counter(_decode(sample.get("status")) for sample in samples if _decode(sample.get("status")))
    actual_reason_counts = Counter(_decode(sample.get("reason_code")) for sample in samples if _decode(sample.get("reason_code")))

    scenarios = default_scenarios()
    baseline = _evaluate_scenario(samples, name=scenarios[0]["name"], thresholds=scenarios[0]["thresholds"])
    baseline_fails = [first_gate_fail_reason(sample, scenarios[0]["thresholds"]) for sample in samples]
    scenario_reports = [baseline]
    for scenario in scenarios[1:]:
        scenario_reports.append(
            _evaluate_scenario(
                samples,
                name=scenario["name"],
                thresholds=scenario["thresholds"],
                baseline_fails=baseline_fails,
            )
        )

    baseline_pass_count = baseline["pre_ai_pass_count"]
    for scenario in scenario_reports[1:]:
        scenario["delta_vs_baseline"] = scenario["pre_ai_pass_count"] - baseline_pass_count

    return {
        "date_from": date_from,
        "date_to": date_to,
        "summary": {
            "scan_sample_count": len(samples),
            "actual_status_counts": dict(actual_status_counts),
            "actual_reason_counts": dict(actual_reason_counts),
            "scenario_reports": scenario_reports,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="COIN Type B gate what-if report")
    parser.add_argument("--date-from", dest="date_from", default=today_kst(), help="KST start date YYYYMMDD")
    parser.add_argument("--date-to", dest="date_to", default=today_kst(), help="KST end date YYYYMMDD")
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_type_b_whatif_report: REDIS_URL not set", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)
    report = build_report(r, date_from=args.date_from, date_to=args.date_to)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
