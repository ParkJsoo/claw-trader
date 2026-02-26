"""
포지션 및 PnL 조회 스크립트.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
import redis

from portfolio.redis_repo import RedisPositionRepository


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--trades",
        type=int,
        default=0,
        help="각 포지션별 최근 N건 거래 출력 (0=미출력, 3~5 권장)",
    )
    args = ap.parse_args()

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)
    repo = RedisPositionRepository(r)

    for market in ("KR", "US"):
        positions = repo.get_all_positions(market)
        realized_total, unrealized = repo.get_pnl(market)
        currency = "KRW" if market == "KR" else "USD"
        print(f"\n=== {market} ({currency}) ===")
        print(f"[pnl:{market}] realized_pnl (계좌 요약): {realized_total}")
        print(f"[pnl:{market}] unrealized_pnl: {unrealized}")
        if positions:
            pos_realized_sum = sum(p.realized_pnl for p in positions)
            print(f"[position hash] realized_pnl 합계 (포지션별): {pos_realized_sum}")
            for p in positions:
                print(
                    f"  {p.symbol}: qty={p.qty} avg_price={p.avg_price} "
                    f"realized_pnl={p.realized_pnl}"
                )
                if args.trades > 0:
                    recent = repo.get_recent_trades(market, p.symbol, limit=args.trades)
                    for t in recent:
                        print(
                            f"    trade {t.get('trade_id','?')} "
                            f"{t.get('side','')} {t.get('qty','')}@{t.get('price','')} "
                            f"pnl={t.get('realized_pnl','')} ts={t.get('ts','')}"
                        )
        else:
            print("  (no positions)")


if __name__ == "__main__":
    main()
