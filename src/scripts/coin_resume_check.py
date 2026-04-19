"""coin_resume_check — COIN 재개 기준 평가."""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import os
import sys

import redis

from app.coin_research import (
    choose_resume_summary,
    compute_trade_summary,
    evaluate_resume_readiness,
)
from app.coin_shadow import compute_shadow_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="COIN resume readiness check")
    parser.add_argument("--date-from", dest="date_from", help="KST start date YYYYMMDD")
    parser.add_argument("--date-to", dest="date_to", help="KST end date YYYYMMDD")
    parser.add_argument(
        "--ledger",
        choices=("auto", "trade", "shadow"),
        default="auto",
        help="resume 판단에 사용할 evidence source",
    )
    args = parser.parse_args()

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("coin_resume_check: REDIS_URL not set", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url, decode_responses=True)
    trade_summary = compute_trade_summary(r, args.date_from, args.date_to)
    shadow_summary = compute_shadow_summary(r, args.date_from, args.date_to)
    selected = choose_resume_summary(trade_summary, shadow_summary, args.ledger)
    evaluation = evaluate_resume_readiness(selected["summary"])
    print(
        json.dumps(
            {
                "selected_ledger": selected["selected_ledger"],
                "trade_summary": trade_summary,
                "shadow_summary": shadow_summary,
                "summary": selected["summary"],
                "resume_check": evaluation,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
