"""
DLQ에 쌓인 Fill을 claw:fill:queue로 재처리.
운영 단계에서 장애 복구 후 DLQ 재주입에 사용.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
import redis

DLQ_KEY = "claw:fill:dlq"
QUEUE_KEY = "claw:fill:queue"


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "-n",
        "--limit",
        type=int,
        default=1,
        help="처리할 DLQ 건수 (기본 1)",
    )
    ap.add_argument(
        "--reset-retry",
        action="store_true",
        help="재처리 시 retry를 0으로 리셋",
    )
    ap.add_argument(
        "--peek",
        action="store_true",
        help="LPOP 없이 LRANGE로 상위 N개 출력만 (requeue 수행 안 함)",
    )
    ap.add_argument(
        "--show-meta",
        action="store_true",
        help="reason, failed_at_ms 출력",
    )
    args = ap.parse_args()

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)

    if args.peek:
        items = r.lrange(DLQ_KEY, 0, args.limit - 1)
        for i, raw in enumerate(items):
            try:
                d = json.loads(raw)
                sym = d.get("symbol", "?")
                side = d.get("side", "")
                qty = d.get("qty", "")
                msg = f"[retry_dlq] peek #{i+1}: {sym} {side} {qty}"
                if args.show_meta:
                    msg += f" reason={d.get('reason','')} failed_at_ms={d.get('failed_at_ms','')}"
                print(msg)
            except Exception:
                raw_text = raw.decode() if isinstance(raw, bytes) else str(raw)
                print(f"[retry_dlq] peek #{i+1} (parse fail): {raw_text[:80]}...")
        print(f"[retry_dlq] peek done: {len(items)} items")
        return

    count = 0
    for _ in range(args.limit):
        raw = r.lpop(DLQ_KEY)
        if not raw:
            break
        try:
            d = json.loads(raw)
            reason = d.get("reason", "")
            failed_at = d.get("failed_at_ms", "")
            if args.reset_retry:
                d["retry"] = 0
            if "reason" in d:
                del d["reason"]
            if "failed_at_ms" in d:
                del d["failed_at_ms"]
            r.lpush(QUEUE_KEY, json.dumps(d, ensure_ascii=False))
            count += 1
            msg = f"[retry_dlq] requeued: {d.get('symbol','?')} {d.get('side','')} {d.get('qty','')}"
            if args.show_meta:
                msg += f" reason={reason} failed_at_ms={failed_at}"
            print(msg)
        except Exception as e:
            print(f"[retry_dlq] error: {e}")
            r.lpush(DLQ_KEY, raw)
            break

    print(f"[retry_dlq] done: {count} items requeued")


if __name__ == "__main__":
    main()
