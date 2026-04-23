"""coin_alt_canary_check - validate/activate COIN alt pullback canary."""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from app.coin_shadow import compute_shadow_summary

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_PROFILE = os.getenv("COIN_ALT_CANARY_PROFILE", "alt_pullback_setup_allow_small_dip")
_DEFAULT_FAMILY = os.getenv("COIN_ALT_CANARY_SIGNAL_FAMILY", "type_b_alt_pullback")


def _origin(profile: str) -> str:
    return f"consensus_runner_type_b_alt_shadow:{profile}"


def _to_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_check(
    r: redis.Redis,
    *,
    date_from: str,
    date_to: str,
    profile: str,
    family: str,
    min_trades: int,
    min_pf: float,
) -> dict[str, object]:
    summary = compute_shadow_summary(r, date_from, date_to)
    profile_stats = (summary.get("by_shadow_origin", {}) or {}).get(_origin(profile), {}) or {}
    trade_count = _to_int(profile_stats.get("trade_count"))
    profit_factor = _to_float(profile_stats.get("profit_factor"))
    net_pnl = _to_float(profile_stats.get("net_pnl"))
    avg_pnl = _to_float(profile_stats.get("avg_pnl"))

    checks = {
        "min_trades": {"actual": trade_count, "required": min_trades, "pass": trade_count >= min_trades},
        "profit_factor": {"actual": profit_factor, "required": min_pf, "pass": profit_factor >= min_pf},
        "net_pnl": {"actual": net_pnl, "required": 0.0, "pass": net_pnl > 0},
        "avg_pnl": {"actual": avg_pnl, "required": 0.0, "pass": avg_pnl > 0},
    }
    ready = all(row["pass"] for row in checks.values())
    return {
        "captured_at_kst": datetime.now(_KST).isoformat(),
        "date_from": date_from,
        "date_to": date_to,
        "profile": profile,
        "family": family,
        "ready": ready,
        "checks": checks,
        "profile_stats": profile_stats,
        "runtime": {
            "pause_global": r.get("claw:pause:global"),
            "pause_coin": r.get("claw:pause:COIN"),
            "signal_mode_coin": r.hgetall("claw:signal_mode:COIN"),
            "pause_bypass_coin": r.hgetall("claw:pause_bypass:COIN"),
            "queue_len": r.llen("claw:signal:queue"),
        },
    }


def activate_canary(r: redis.Redis, *, family: str) -> None:
    r.hset("claw:signal_mode:COIN", mapping={family: "live"})
    r.hset("claw:pause_bypass:COIN", mapping={family: "true"})


def deactivate_canary(r: redis.Redis, *, family: str) -> None:
    r.hset("claw:signal_mode:COIN", mapping={family: "off"})
    r.hdel("claw:pause_bypass:COIN", family)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or activate COIN alt pullback canary")
    parser.add_argument("--date-from", default=datetime.now(_KST).strftime("%Y%m%d"))
    parser.add_argument("--date-to", default=datetime.now(_KST).strftime("%Y%m%d"))
    parser.add_argument("--profile", default=_DEFAULT_PROFILE)
    parser.add_argument("--family", default=_DEFAULT_FAMILY)
    parser.add_argument("--min-trades", type=int, default=int(os.getenv("COIN_ALT_CANARY_MIN_TRADES", "50")))
    parser.add_argument("--min-pf", type=float, default=float(os.getenv("COIN_ALT_CANARY_MIN_PF", "1.3")))
    parser.add_argument("--activate", action="store_true", help="activate canary if all checks pass")
    parser.add_argument("--deactivate", action="store_true", help="turn off canary family and pause bypass")
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_alt_canary_check: REDIS_URL not set", file=sys.stderr, flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)

    if args.activate and args.deactivate:
        print("coin_alt_canary_check: choose only one of --activate/--deactivate", file=sys.stderr, flush=True)
        sys.exit(2)

    if args.deactivate:
        deactivate_canary(r, family=args.family)

    report = build_check(
        r,
        date_from=args.date_from,
        date_to=args.date_to,
        profile=args.profile,
        family=args.family,
        min_trades=args.min_trades,
        min_pf=args.min_pf,
    )

    action = "dry_run"
    if args.activate:
        if not report["ready"]:
            action = "activation_blocked"
        else:
            activate_canary(r, family=args.family)
            report = build_check(
                r,
                date_from=args.date_from,
                date_to=args.date_to,
                profile=args.profile,
                family=args.family,
                min_trades=args.min_trades,
                min_pf=args.min_pf,
            )
            action = "activated"
    elif args.deactivate:
        action = "deactivated"

    report["action"] = action
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.activate and action != "activated":
        sys.exit(3)


if __name__ == "__main__":
    main()
