"""
Position Engine — Fill 큐 소비, 포지션 갱신.
독립 프로세스로 실행 시 claw:fill:queue의 Fill 이벤트를 처리.
예외 시 DLQ/Retry로 fill 유실 방지.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
import redis

from portfolio.engine import PositionEngine
from portfolio.redis_repo import RedisPositionRepository

MAX_RETRY = 5
STATUS_LOG_INTERVAL_SEC = 30
FILL_QUEUE_KEY = "claw:fill:queue"
FILL_DLQ_KEY = "claw:fill:dlq"


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)
    repo = RedisPositionRepository(r)
    engine = PositionEngine(repo)

    processed = 0
    requeued = 0
    dlqed = 0
    parse_failed = 0
    last_status_log = 0.0

    print("[position_engine] started, consuming claw:fill:queue")
    while True:
        fill = repo.pop_fill(timeout=5)
        now = time.time()

        if fill is None:
            parse_failed += 1
            if now - last_status_log >= STATUS_LOG_INTERVAL_SEC:
                queue_len = r.llen(FILL_QUEUE_KEY)
                dlq_len = r.llen(FILL_DLQ_KEY)
                print(
                    f"[position_engine] status processed={processed} "
                    f"requeued={requeued} dlqed={dlqed} parse_failed={parse_failed} "
                    f"queue={queue_len} dlq={dlq_len}"
                )
                last_status_log = now
            continue

        try:
            engine.apply_fill(fill)
            processed += 1
            print(
                f"[position_engine] applied fill: {fill.symbol} "
                f"{fill.side.value} {fill.qty}@{fill.price}"
            )
        except Exception as e:
            if fill.retry >= MAX_RETRY:
                repo.push_fill_dlq(fill, reason=str(e))
                dlqed += 1
                print(
                    f"[position_engine] DLQ: {fill.symbol} "
                    f"retry={fill.retry} err={e}"
                )
            else:
                repo.requeue_fill(fill)
                requeued += 1
                print(
                    f"[position_engine] requeue: {fill.symbol} "
                    f"retry={fill.retry} err={e}"
                )

        if now - last_status_log >= STATUS_LOG_INTERVAL_SEC:
            queue_len = r.llen(FILL_QUEUE_KEY)
            dlq_len = r.llen(FILL_DLQ_KEY)
            print(
                f"[position_engine] status processed={processed} "
                f"requeued={requeued} dlqed={dlqed} parse_failed={parse_failed} "
                f"queue={queue_len} dlq={dlq_len}"
            )
            last_status_log = now


if __name__ == "__main__":
    main()
