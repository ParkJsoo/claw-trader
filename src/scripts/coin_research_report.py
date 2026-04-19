"""coin_research_report — COIN 연구 레저 요약 출력."""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import os
import sys

import redis

from app.coin_research import compute_trade_summary
from app.coin_shadow import compute_shadow_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="COIN research ledger summary")
    parser.add_argument("--date-from", dest="date_from", help="KST start date YYYYMMDD")
    parser.add_argument("--date-to", dest="date_to", help="KST end date YYYYMMDD")
    parser.add_argument(
        "--ledger",
        choices=("trade", "shadow", "both"),
        default="trade",
        help="출력할 ledger 종류",
    )
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_research_report: REDIS_URL not set", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)
    if args.ledger == "trade":
        summary = compute_trade_summary(r, args.date_from, args.date_to)
    elif args.ledger == "shadow":
        summary = compute_shadow_summary(r, args.date_from, args.date_to)
    else:
        summary = {
            "trade": compute_trade_summary(r, args.date_from, args.date_to),
            "shadow": compute_shadow_summary(r, args.date_from, args.date_to),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
